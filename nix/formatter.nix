# `nix fmt`, through treefmt-nix, taken from the index itself: the
# formatter is `omniflake.flakes.treefmt-nix`, so the repository is its own
# first consumer and no development-only input reaches anyone's lock file.
{ treefmt-nix, pkgs }:
treefmt-nix.lib.evalModule pkgs {
  projectRootFile = "flake.nix";

  programs.nixfmt.enable = true;

  # The scripts in tools/. black rather than a linter: the point is that
  # nobody argues about where a call wraps.
  programs.black.enable = true;

  # The hand-written lists: manual.txt, always.txt, names.txt and
  # blocklist.txt. Each group of entries in those files is a keep-sorted
  # block, so a group keeps the comment explaining why its lines are there
  # and is still sorted. Only .txt: no other file in the tree carries a
  # marker, and keep-sorted's own default is every file.
  programs.keep-sorted.enable = true;
  programs.keep-sorted.includes = [ "*.txt" ];

  # Markdown, the workflow files and the site, at the width the docs are
  # written to. proseWrap stays at its default of preserving line breaks: the
  # docs are hand-wrapped prose, and reflowing them makes every diff a
  # whole-file diff.
  programs.prettier.enable = true;
  settings.formatter.prettier = {
    options = [
      "--print-width"
      "80"
    ];
    includes = [
      "*.md"
      "*.yml"
      "*.js"
      "*.css"
      "*.html"
    ];
  };

  # Generated data, and stored lock files written the way Nix writes them.
  # data-pins.json is rewritten by tools/bump-data-pin.sh on every release
  # cut, and the update workflow commits it without running the formatter.
  settings.global.excludes = [
    "index.json"
    "data-pins.json"
    "locks/*"
    "*.jsonl"
    "flake.lock"
  ];
}
