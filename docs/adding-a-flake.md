# Adding or removing a flake

## Adding

`manual.txt` lists flakes the pipeline does not index on its own, including
flakes outside GitHub. It is read on every run. One entry per line:

```
nix-community/disko
github:owner/repo/v1.2.3
gitlab:owner/repo
```

- `owner/repo`: a GitHub repository, pinned to its default branch. It
  becomes a candidate and is resolved by `tools/resolve.py` like a harvested
  repository.
- Any other flake reference Nix can fetch, including a specific ref. It is
  resolved with `nix flake metadata` and pinned to an exact revision.

A flake listed here is also exempt from `tools/classify.py`, which guesses
from the repository name which flakes are somebody's own machine
configuration. That guess is wrong in both directions: `catppuccin/nix` is
a theme, but its repository is named `nix`, which is exactly the shape of
the personal configs the rule exists to drop. A hand-written line is
better evidence than the name.

So `manual.txt` means "index this, whatever the pipeline concludes on its
own", and `blocklist.txt` below is its opposite. Reach for `manual.txt`
when search cannot see a repository _or_ when the classifier is wrong
about one.

To add a flake, add a line to `manual.txt` and regenerate:

```console
$ nix run .#update -- --no-harvest
```

The new flake is fetched once by `nix flake metadata`, its `locked`
attributes are written to `pins.jsonl`, and `index.json` gains an entry.

`resolved.jsonl` and `pins.jsonl` are not committed. Cut a release for the
bytes they now hold, which repoints `data-pins.json`:

```console
$ nix run .#cut-data-release
```

Then commit `manual.txt`, `data-pins.json`, `index.json` and any new file
under `locks/`.

The `check` workflow regenerates the index on every pull request and fails
if the committed `index.json` differs from the generated one. It also
re-derives every pin the pull request adds or changes with `nix flake
metadata` and evaluates every new name, so the committed `locked`
attributes are checked against what the source really serves. The same
check runs locally with `nix run .#verify`.

## Removing

Add the attribute name to `blocklist.txt`, one per line, and regenerate:

```
some-flake
```

The row stays in `resolved.jsonl`, so the name stays reserved; the flake is
no longer indexed. Flakes that fail to pin do not need an entry: `tools/pin.py`
records them in `failures.jsonl` with the error.

## Refresh cadence

Each run re-resolves the 2,000 rows resolved longest ago, so with about
16,000 flakes a given one comes round roughly every eight days. `always.txt`
is the exemption: one `owner/repo` per line, re-resolved on every run
however recently it was last looked at.

```
NixOS/nixpkgs
nix-community/home-manager
```

Use it for a flake whose pin someone would notice going stale — the five
foundations, whose revision is substituted into every indexed flake, and the
handful people track daily. A line here does not consume the rolling
window's budget, so nothing else is refreshed less often, and `tools/pin.py`
is keyed by revision, so on a day the flake did not move it costs one
lookup. Keep the list short anyway: the rolling window already covers
anything that is not moving fast, and a line for a repository that is not in
`resolved.jsonl` does nothing.

## Names

An attribute name is derived from the repository name, and a bare name is
only assigned when one repository claims it. Sixty-one repositories are
named `home-manager` and 110 are named `flake`; a bare name several
repositories
could equally mean identifies none of them, so a contested name goes to
nobody and every claimant gets the owner appended: `home-manager-rc-14`.

`names.txt` is where a person says which repository a name means. One entry
per line, the repository then the name:

```
nix-community/home-manager  home-manager
nixified-ai/flake           nixified-ai
```

The file does two jobs. It hands a contested name to the project people
mean when they type it, which is the only way `home-manager` or `nixpkgs`
gets a bare name at all. And it corrects a derived name that says nothing:
`nixified-ai/flake` derives `flake`, `catppuccin/nix` derives `nix`, and
both are the repository name doing a poor job of naming the project.

A name of `-` is the third job, and it takes a bare name away:

```
akirak/git-hooks  -
```

The repository is then named `git-hooks-akirak` and nothing holds
`git-hooks`. Reach for it when one repository holds a name that people
overwhelmingly use for a different project, which matters beyond the
attribute: an index name is what `unified` substitutes on, so a bare name
on the wrong repository rewrites that input for every flake declaring it.
319 indexed flakes declare `git-hooks` and mean `cachix/git-hooks.nix`.

Leaving a contested repository out is the normal case. It stays reachable
by its qualified name, which is unambiguous, and a bare name is reserved
only when it carries real information. `postgres` does not obviously mean
`supabase/postgres`, so that one is absent on purpose.

A name never changes once assigned. A repository that later gains stars does
not take a bare name from the repository holding it, because consumers refer
to flakes by name. A `names.txt` line is the one thing that outranks that,
and it applies on the next run rather than whenever the row next comes round
for a refresh: the repository the line names takes the name, and whoever was
holding it is displaced to its qualified form.
