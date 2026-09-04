"""Tests for the reject ledger resolve.py keeps.

Everything under test is a pure function over dicts and lists, so no test
here issues a GraphQL query or reads a file.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import resolve


class TestOldestRejects(unittest.TestCase):
    """Tests which reject rows a run looks at anyway, over a ledger whose
    rows were checked at different times."""

    LEDGER = {
        ("a", "one"): 100,
        ("b", "two"): 300,
        ("c", "three"): 200,
    }

    def test_the_oldest_rows_come_first(self):
        self.assertEqual(
            resolve.oldest_rejects(self.LEDGER, 2), {("a", "one"), ("c", "three")}
        )

    def test_zero_disables_the_recheck(self):
        # What --refresh-oldest 0 already does for the known set, for a
        # smoke run that should query nothing it does not have to.
        self.assertEqual(resolve.oldest_rejects(self.LEDGER, 0), set())

    def test_asking_for_more_than_there_is_takes_everything(self):
        self.assertEqual(resolve.oldest_rejects(self.LEDGER, 99), set(self.LEDGER))

    def test_ties_break_on_the_repository(self):
        # Two rows seeded in the same second must not make the selection
        # depend on dict order, or a run's query count stops being stable.
        ledger = {("b", "two"): 5, ("a", "one"): 5}
        self.assertEqual(resolve.oldest_rejects(ledger, 1), {("a", "one")})


class TestRepoFragment(unittest.TestCase):
    """Tests what one repository's GraphQL selection asks for, since every
    field a row records has to be in it and nothing else fetches them."""

    FRAGMENT = resolve.repo_fragment("r0", "NixOS", "nixpkgs")

    def test_it_names_the_repository(self):
        self.assertIn('repository(owner: "NixOS", name: "nixpkgs")', self.FRAGMENT)

    def test_it_asks_for_everything_a_row_records(self):
        # The star count included: harvest.py is the only other thing that
        # knows one, and it never sees a repository again after the run
        # that found it.
        for field in ["stargazerCount", "defaultBranchRef", "flakeNix", "flakeLock"]:
            self.assertIn(field, self.FRAGMENT)


class TestSelectCandidates(unittest.TestCase):
    """Tests which candidates a run queries, over a pool holding one
    repository of every kind: new, known, merged and rejected."""

    POOL = [
        {"owner": "a", "repo": "new", "stars": 1},
        {"owner": "b", "repo": "known", "stars": 2},
        {"owner": "c", "repo": "merged", "stars": 3},
        {"owner": "d", "repo": "rejected", "stars": 4},
    ]
    KNOWN = {("b", "known"): {}}
    MERGED = {("c", "merged"): {}}
    REJECTS = {("d", "rejected"): 100}

    def select(self, recheck):
        return [
            (c["owner"], c["repo"])
            for c in resolve.select_candidates(
                self.POOL, self.KNOWN, self.MERGED, self.REJECTS, recheck
            )
        ]

    def test_a_rejected_repository_is_skipped(self):
        # The whole point: 8,357 repositories were re-queried every night
        # because nothing recorded that they had been checked.
        self.assertEqual(self.select(recheck=set()), [("a", "new")])

    def test_a_rejected_repository_comes_round_again(self):
        # A repository can add a flake.nix tomorrow, so the record is a
        # timestamp and not a verdict.
        self.assertEqual(
            self.select(recheck={("d", "rejected")}),
            [("a", "new"), ("d", "rejected")],
        )

    def test_known_and_merged_repositories_are_still_skipped(self):
        # Even a re-check does not reach them; success is authoritative.
        self.assertNotIn(("b", "known"), self.select(recheck=set(self.REJECTS)))
        self.assertNotIn(("c", "merged"), self.select(recheck=set(self.REJECTS)))


class TestRepoKey(unittest.TestCase):
    """Tests which hand-written spellings name a repository, over every
    form manual.txt uses and the shapes that must be refused rather than
    guessed at.

    names.txt and always.txt both key on a repository, so this is what
    decides whether a line reaches a row at all. A refusal is what makes
    the caller warn; a wrong guess would be a line that silently does
    nothing."""

    def test_a_bare_entry_names_its_repository(self):
        self.assertEqual(resolve.repo_key("NixOS/nixpkgs"), ("nixos", "nixpkgs"))

    def test_a_github_reference_names_the_same_repository(self):
        self.assertEqual(resolve.repo_key("github:NixOS/nixpkgs"), ("nixos", "nixpkgs"))

    def test_a_ref_is_dropped(self):
        # manual.txt spells a pinned flake this way, and a row is keyed on
        # the repository whatever revision it is pinned to.
        self.assertEqual(
            resolve.repo_key("github:roman/nixDir/v3"), ("roman", "nixdir")
        )

    def test_another_forge_names_its_repository_too(self):
        self.assertEqual(resolve.repo_key("gitlab:owner/repo"), ("owner", "repo"))
        self.assertEqual(resolve.repo_key("sourcehut:~user/repo"), ("~user", "repo"))

    def test_a_subdirectory_is_dropped(self):
        self.assertEqual(
            resolve.repo_key("github:owner/repo?dir=sub"), ("owner", "repo")
        )

    def test_a_url_names_nothing(self):
        # The owner of a url-shaped reference comes out of its path, which
        # is manual.py's job. Here it is refused so the line is warned
        # about and can be written bare.
        self.assertIsNone(resolve.repo_key("git+https://example.com/team/proj"))

    def test_an_unknown_scheme_names_nothing(self):
        self.assertIsNone(resolve.repo_key("weird:owner/repo"))

    def test_a_line_with_no_owner_names_nothing(self):
        self.assertIsNone(resolve.repo_key("nixpkgs"))

    def test_a_bare_line_of_three_segments_names_nothing(self):
        # Only a reference that said which forge it is on may carry a ref.
        self.assertIsNone(resolve.repo_key("owner/repo/v3"))


class TestLoadAlwaysEntries(unittest.TestCase):
    """Tests the always.txt parser, over lines carrying every shape the file
    permits: an entry, a comment, a trailing comment, blank space and junk."""

    def test_it_reads_one_repository_per_line(self):
        self.assertEqual(
            resolve.load_always_entries(
                ["NixOS/nixpkgs", "nix-community/home-manager"]
            ),
            {("nixos", "nixpkgs"), ("nix-community", "home-manager")},
        )

    def test_comments_and_blank_lines_are_ignored(self):
        self.assertEqual(
            resolve.load_always_entries(
                ["# the foundations", "", "  ", "NixOS/nixpkgs  # the big one"]
            ),
            {("nixos", "nixpkgs")},
        )

    def test_keys_are_lowercased(self):
        # GitHub is case-insensitive about owners and repositories and this
        # file is written by hand, so a line must match a resolved row whose
        # owner GitHub spelled differently.
        self.assertEqual(
            resolve.load_always_entries(["NIXOS/NixPkgs"]), {("nixos", "nixpkgs")}
        )

    def test_a_flake_reference_names_the_same_repository_as_a_bare_line(self):
        # The two spellings mean one repository, so a file may use either.
        self.assertEqual(
            resolve.load_always_entries(["github:NixOS/nixpkgs"]),
            resolve.load_always_entries(["NixOS/nixpkgs"]),
        )

    def test_a_malformed_line_is_dropped_not_fatal(self):
        # A typo in a hand-written file must not take the nightly run down.
        self.assertEqual(
            resolve.load_always_entries(
                ["nixpkgs", "a/b c", "git+https://x.com/a/b", "NixOS/nixpkgs"]
            ),
            {("nixos", "nixpkgs")},
        )


class TestSelectRefresh(unittest.TestCase):
    """Tests which known rows a bounded run re-resolves, over a database whose
    rows were resolved at different times and one of which is pinned to
    always refresh."""

    KNOWN = {
        ("NixOS", "nixpkgs"): {"owner": "NixOS", "repo": "nixpkgs", "resolved_at": 900},
        ("a", "one"): {"owner": "a", "repo": "one", "resolved_at": 100},
        ("b", "two"): {"owner": "b", "repo": "two", "resolved_at": 200},
        ("c", "three"): {"owner": "c", "repo": "three", "resolved_at": 300},
    }

    def keys(self, always, count):
        return [
            (r["owner"], r["repo"])
            for r in resolve.select_refresh(self.KNOWN, always, count)
        ]

    def test_without_an_always_set_it_is_the_oldest_n(self):
        self.assertEqual(self.keys(set(), 2), [("a", "one"), ("b", "two")])

    def test_an_always_row_is_refreshed_however_fresh_it_is(self):
        # nixpkgs was resolved most recently of the four, so the age window
        # would not reach it for another several runs.
        self.assertEqual(
            self.keys({("nixos", "nixpkgs")}, 2),
            [("NixOS", "nixpkgs"), ("a", "one"), ("b", "two")],
        )

    def test_an_always_row_does_not_spend_the_age_budget(self):
        # The rolling cadence for the other 16,000 rows is what the count
        # buys; a handful of pinned rows must not shorten it.
        self.assertEqual(len(self.keys({("nixos", "nixpkgs")}, 2)), 3)

    def test_a_row_is_never_selected_twice(self):
        # An always row old enough to also fall in the age window.
        selected = self.keys({("a", "one")}, 3)
        self.assertEqual(len(selected), len(set(selected)))

    def test_a_zero_count_still_refreshes_the_always_set(self):
        # A smoke run asks for no rolling refresh at all and should still
        # move the foundations.
        self.assertEqual(self.keys({("nixos", "nixpkgs")}, 0), [("NixOS", "nixpkgs")])

    def test_a_line_for_a_repository_that_is_not_known_is_inert(self):
        self.assertEqual(self.keys({("nobody", "nothing")}, 1), [("a", "one")])


class TestPruneRejects(unittest.TestCase):
    """Tests that a repository which resolved leaves the ledger, over a
    ledger holding a row for a repository that is in each database."""

    def test_a_resolved_repository_loses_its_row(self):
        # Success is authoritative: a stale reject row for a repository in
        # resolved.jsonl would skip a repository the index already has.
        rejects = {("a", "one"): 1, ("b", "two"): 2, ("c", "three"): 3}
        resolve.prune_rejects(rejects, {("a", "one"): {}}, {("b", "two"): {}})
        self.assertEqual(rejects, {("c", "three"): 3})

    def test_an_unrelated_ledger_is_untouched(self):
        rejects = {("c", "three"): 3}
        resolve.prune_rejects(rejects, {}, {})
        self.assertEqual(rejects, {("c", "three"): 3})


if __name__ == "__main__":
    unittest.main()
