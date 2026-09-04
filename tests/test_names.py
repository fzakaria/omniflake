"""Tests for the attribute name resolve.py gives a repository.

Everything under test is a pure function over dicts and counters, so no
test here issues a GraphQL query. load_reserved is the one exception and
reads a file, so it gets a temporary one.
"""

import collections
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import resolve


class TestLoadReserved(unittest.TestCase):
    """Tests that names.txt parses, over a file holding a comment, a blank
    line, an inline comment, mixed case and a flake reference."""

    FILE = """\
# a comment
nix-community/home-manager   home-manager

NixOS/nixpkgs                nixpkgs   # an inline comment
github:roman/nixDir/v3       nixdir
"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        self.tmp.write(self.FILE)
        self.tmp.close()
        self.addCleanup(os.unlink, self.tmp.name)

    def test_entries_are_read_and_keyed_lowercased(self):
        # The key is lowercased because GitHub is case-insensitive about
        # owners and repositories and the file is written by hand.
        self.assertEqual(
            resolve.load_reserved(self.tmp.name),
            {
                ("nix-community", "home-manager"): "home-manager",
                ("nixos", "nixpkgs"): "nixpkgs",
                # A line copied out of manual.txt names the repository it
                # names there, ref and all. See resolve.repo_key.
                ("roman", "nixdir"): "nixdir",
            },
        )

    def test_a_missing_file_reserves_nothing(self):
        self.assertEqual(resolve.load_reserved("/nonexistent/names.txt"), {})

    def test_a_line_that_names_no_repository_is_dropped_not_fatal(self):
        # A typo in a hand-written file must not take the nightly run down,
        # and a line the parser cannot key would otherwise sit there doing
        # nothing without ever saying so.
        self.assertEqual(
            resolve.load_reserved_entries(
                ["nixpkgs nixpkgs", "a/b", "git+https://x.com/a/b n", "a/b n"]
            ),
            {("a", "b"): "n"},
        )


class TestChooseName(unittest.TestCase):
    """Tests which name a repository gets, over the four cases that decide
    it: a hand-assigned name, a name the repository already holds, a
    repository name only one repository claims, and one several claim."""

    RESERVED = {("nixified-ai", "flake"): "nixified-ai"}

    def choose(self, owner, repo, prior=None, claims=None, taken=None, used=None):
        return resolve.choose_name(
            owner,
            repo,
            prior,
            self.RESERVED,
            collections.Counter(claims or {}),
            taken or {},
            collections.Counter(used or {}),
        )

    def test_a_hand_assigned_name_wins_over_the_derived_one(self):
        self.assertEqual(self.choose("nixified-ai", "flake"), "nixified-ai")

    def test_a_hand_assigned_name_wins_over_the_name_already_held(self):
        # The migration case: names.txt is how a name that was already
        # assigned gets corrected, so it has to beat stickiness.
        prior = {"name": "flake"}
        self.assertEqual(
            self.choose("nixified-ai", "flake", prior=prior), "nixified-ai"
        )

    def test_a_repository_keeps_the_name_it_was_given(self):
        prior = {"name": "disko"}
        self.assertEqual(self.choose("nix-community", "disko", prior=prior), "disko")

    def test_a_name_only_one_repository_claims_is_taken_bare(self):
        self.assertEqual(
            self.choose("nix-community", "disko", claims={"disko": 1}), "disko"
        )

    def test_a_name_several_repositories_claim_is_qualified(self):
        # 110 repositories are named "flake" and 61 "home-manager". A bare
        # name that several repositories could equally mean identifies
        # none of them, so nobody gets it without a names.txt line.
        self.assertEqual(
            self.choose("someone", "home-manager", claims={"home-manager": 61}),
            "home-manager-someone",
        )

    def test_a_name_another_repository_holds_is_not_taken(self):
        taken = {"disko": ("nix-community", "disko")}
        self.assertEqual(
            self.choose("fork", "disko", claims={"disko": 1}, taken=taken),
            "disko-fork",
        )

    def test_two_repositories_in_one_run_do_not_both_take_the_name(self):
        self.assertEqual(
            self.choose("second", "nh", claims={"nh": 1}, used={"nh": 1}), "nh-second"
        )


class TestApplyReserved(unittest.TestCase):
    """Tests that names.txt reaches rows nothing else in the run touches,
    over a database holding the repository a line names, a different
    repository sitting on the name it asks for, and one already correct.

    This is the only thing that applies a hand-assigned name to a row the
    run carries over: a known row keeps the name it has and is never put
    through choose_name again.
    """

    def row(self, owner, repo, name):
        return {"name": name, "owner": owner, "repo": repo}

    def test_the_repository_a_line_names_takes_the_name(self):
        known = {("nixified-ai", "flake"): self.row("nixified-ai", "flake", "flake")}
        counts = resolve.apply_reserved(
            known, {("nixified-ai", "flake"): "nixified-ai"}
        )
        self.assertEqual(known[("nixified-ai", "flake")]["name"], "nixified-ai")
        self.assertEqual(counts, (1, 0))

    def test_the_incumbent_is_displaced_to_its_qualified_name(self):
        known = {("someone", "nixvim"): self.row("someone", "nixvim", "nixvim")}
        counts = resolve.apply_reserved(known, {("nix-community", "nixvim"): "nixvim"})
        self.assertEqual(known[("someone", "nixvim")]["name"], "nixvim-someone")
        self.assertEqual(counts, (0, 1))

    def test_the_repository_the_name_belongs_to_is_left_alone(self):
        known = {
            ("nix-community", "nixvim"): self.row("nix-community", "nixvim", "nixvim")
        }
        counts = resolve.apply_reserved(known, {("nix-community", "nixvim"): "nixvim"})
        self.assertEqual(known[("nix-community", "nixvim")]["name"], "nixvim")
        self.assertEqual(counts, (0, 0))

    def test_a_row_both_displaced_and_named_takes_its_own_name(self):
        # hyprwm/Hyprland is reserved "hyprland" while IceDOS/hyprland
        # holds it. Were the two repositories reversed in one line each,
        # the assignment has to win or the row loses the name given to it.
        known = {("a", "x"): self.row("a", "x", "y")}
        counts = resolve.apply_reserved(known, {("b", "y"): "y", ("a", "x"): "z"})
        self.assertEqual(known[("a", "x")]["name"], "z")
        self.assertEqual(counts, (1, 0))

    def test_a_denied_name_is_given_up(self):
        # A line whose name is "-" means the repository gets no bare name,
        # which load_reserved reads as its qualified one.
        reserved = resolve.load_reserved_entries(["akirak/git-hooks -"])
        known = {("akirak", "git-hooks"): self.row("akirak", "git-hooks", "git-hooks")}
        counts = resolve.apply_reserved(known, reserved)
        self.assertEqual(known[("akirak", "git-hooks")]["name"], "git-hooks-akirak")
        self.assertEqual(counts, (1, 0))


class TestCountClaims(unittest.TestCase):
    """Tests that the contention count covers every set a run writes from,
    over a name claimed once by the database and once by a merged row."""

    def row(self, owner, repo):
        return {"owner": owner, "repo": repo, "name": ""}

    def test_a_name_one_repository_claims_is_counted_once(self):
        claims = resolve.count_claims([self.row("nix-community", "disko")], [], [])
        self.assertEqual(claims["disko"], 1)

    def test_an_externally_resolved_row_contests_a_name(self):
        # The half of the collapse that a names.txt line cannot fix: a
        # merged row left out of this count takes a contested bare name
        # without anything noticing.
        claims = resolve.count_claims(
            [self.row("sini", "files")], [], [self.row("someone", "files")]
        )
        self.assertEqual(claims["files"], 2)

    def test_a_candidate_contests_a_name(self):
        claims = resolve.count_claims(
            [self.row("sini", "files")], [self.row("someone", "files")], []
        )
        self.assertEqual(claims["files"], 2)

    def test_one_repository_in_two_sets_is_counted_once(self):
        # A merged row usually has a known row for the same repository, and
        # a refreshed candidate always does.
        claims = resolve.count_claims(
            [self.row("roman", "nixDir")], [], [self.row("roman", "nixDir")]
        )
        self.assertEqual(claims["nixdir"], 1)


class TestNameMerged(unittest.TestCase):
    """Tests that an externally resolved row is named by the same rules as
    every other row, over the four cases that decide one: a names.txt line,
    the name the repository already holds, a contested repository name and
    an uncontested one.

    tools/manual.py gives such a row a provisional name derived from its
    repository. It sees one flake at a time, so that name is the one thing
    it cannot decide.
    """

    def row(self, owner, repo, name):
        return {"name": name, "owner": owner, "repo": repo}

    def name(self, merged, known=None, reserved=None, claims=None):
        taken = {e["name"]: k for k, e in (known or {}).items()}
        resolve.name_merged(
            merged,
            known or {},
            reserved or {},
            collections.Counter(claims or {}),
            taken,
            collections.Counter(),
        )
        return merged

    def test_a_names_txt_line_reaches_a_merged_row(self):
        merged = {("roman", "nixDir"): self.row("roman", "nixDir", "nixdir")}
        self.name(merged, reserved={("roman", "nixdir"): "nix-dir"})
        self.assertEqual(merged[("roman", "nixDir")]["name"], "nix-dir")

    def test_a_contested_repository_name_is_qualified(self):
        # github:sini/files as a merged row used to take "files" whatever
        # else claimed it, and generate.py wrote one entry for both.
        merged = {("sini", "files"): self.row("sini", "files", "files")}
        self.name(merged, claims={"files": 2})
        self.assertEqual(merged[("sini", "files")]["name"], "files-sini")

    def test_an_uncontested_repository_name_is_taken_bare(self):
        merged = {("roman", "nixDir"): self.row("roman", "nixDir", "nixdir")}
        self.name(merged, claims={"nixdir": 1})
        self.assertEqual(merged[("roman", "nixDir")]["name"], "nixdir")

    def test_a_row_keeps_the_name_its_repository_already_holds(self):
        # Names are API. A merged row is re-resolved on every run, so
        # without stickiness the contention rule could move it whenever
        # some other repository of the same name appeared.
        known = {("roman", "nixDir"): self.row("roman", "nixDir", "nixdir")}
        merged = {("roman", "nixDir"): self.row("roman", "nixDir", "nixdir")}
        self.name(merged, known=known, claims={"nixdir": 2})
        self.assertEqual(merged[("roman", "nixDir")]["name"], "nixdir")

    def test_a_name_another_repository_holds_is_not_taken(self):
        known = {("someone", "files"): self.row("someone", "files", "files")}
        merged = {("sini", "files"): self.row("sini", "files", "files")}
        self.name(merged, known=known, claims={"files": 1})
        self.assertEqual(merged[("sini", "files")]["name"], "files-sini")

    def test_a_row_with_no_owner_is_never_qualified_to_a_dangling_dash(self):
        # manual.py derives an owner for the url-only fetchers precisely so
        # this fallback reads as something. Guarded here because the name
        # is only wrong once a second repository contests it.
        merged = {("example.com", "proj"): self.row("example.com", "proj", "proj")}
        self.name(merged, claims={"proj": 2})
        self.assertEqual(merged[("example.com", "proj")]["name"], "proj-example-com")


if __name__ == "__main__":
    unittest.main()
