"""Tests for the override map generate.py writes for unification.

The function under test is pure over lists of rows, so no test here reads
a database, runs Nix or touches the network.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import generate


def row(owner, repo, name):
    return {"owner": owner, "repo": repo, "name": name}


class TestUnifyNames(unittest.TestCase):
    """Tests which index names unification may substitute by input name,
    over a database holding an uncontested name, a contested one, and a
    contested one that names.txt hands to a repository."""

    # 26 repositories are named "home" and one of them won the name. The
    # other rows are here to contest it.
    RESOLVED = [
        row("nix-community", "disko", "disko"),
        row("tiredofit", "home", "home"),
        row("someone", "home", "home-someone"),
        row("nix-community", "nixvim", "nixvim"),
        row("elsewhere", "nixvim", "nixvim-elsewhere"),
    ]
    INDEXED = [
        row("nix-community", "disko", "disko"),
        row("tiredofit", "home", "home"),
        row("nix-community", "nixvim", "nixvim"),
    ]
    RESERVED = {("nix-community", "nixvim"): "nixvim"}

    def test_a_name_one_repository_claims_is_an_override_key(self):
        self.assertIn("disko", generate.unify_names(self.INDEXED, self.RESOLVED, {}))

    def test_a_name_several_repositories_claim_is_not(self):
        # The bug this exists to stop: 49 indexed flakes declare an input
        # called "home", and substituting it replaced every one of them
        # with a stranger's machine configuration.
        self.assertNotIn("home", generate.unify_names(self.INDEXED, self.RESOLVED, {}))

    def test_a_contested_name_names_txt_hands_over_is_an_override_key(self):
        # 67 repositories are named nixvim, so without the line nobody
        # gets to claim the input name.
        self.assertNotIn(
            "nixvim", generate.unify_names(self.INDEXED, self.RESOLVED, {})
        )
        self.assertIn(
            "nixvim", generate.unify_names(self.INDEXED, self.RESOLVED, self.RESERVED)
        )

    def test_contention_is_counted_over_the_whole_database(self):
        # someone/home is classified personal and is not in the index, but
        # it still means "home" does not identify a repository.
        indexed_only = generate.unify_names(self.INDEXED, self.INDEXED, {})
        self.assertIn("home", indexed_only)
        self.assertNotIn("home", generate.unify_names(self.INDEXED, self.RESOLVED, {}))

    def test_a_line_the_index_has_not_applied_yet_is_not_an_override_key(self):
        # resolve.py applies names.txt, so a line committed today reaches
        # the index on the next pipeline run and not before. Until then
        # the name is still on the repository the line moves it off, and
        # vouching for it would substitute exactly that repository.
        pending = {
            ("nix-community", "nixvim"): "nixvim",
            ("elsewhere", "nixvim"): "nixvim",
        }
        self.assertNotIn(
            "nixvim-elsewhere",
            generate.unify_names(
                self.INDEXED + [row("elsewhere", "nixvim", "nixvim-elsewhere")],
                self.RESOLVED,
                pending,
            ),
        )

    def test_a_flake_outside_the_index_is_never_an_override_key(self):
        keys = generate.unify_names(self.INDEXED, self.RESOLVED, {})
        self.assertNotIn("nixvim-elsewhere", keys)
        self.assertNotIn("home-someone", keys)

    def test_the_result_is_sorted(self):
        keys = generate.unify_names(self.INDEXED, self.RESOLVED, self.RESERVED)
        self.assertEqual(keys, sorted(keys))


if __name__ == "__main__":
    unittest.main()
