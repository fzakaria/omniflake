# Adding or removing a flake

## Adding

`manual.txt` lists flakes the pipeline does not index on its own. It is read
on every run, one entry per line:

```
nix-community/disko
github:nix-community/disko
github:owner/repo/v1.2.3
gitlab:owner/repo
```

A GitHub repository's default branch is resolved like a harvested one
however it is spelled: the first two lines above are the same entry.
Anything else — a tag, a subdirectory, another forge — is beyond the GitHub
API, so `nix flake metadata` pins it instead. Either way the row
is named by `tools/resolve.py`, so every entry goes through the contention
rule, `names.txt` and the stickiness rule below.

`always.txt` and `names.txt` accept the same spellings, since both key on a
repository: a line copied out of `manual.txt` reaches the row it names, and
a ref on it is ignored. The exception is a `git+https://` reference, whose
owner comes out of the url path — write that one as `owner/repo` in those
two files. A line neither parser can key is warned about on the next run
rather than sitting there doing nothing.

A flake listed here is also exempt from `tools/classify.py`, which guesses
from the repository name which flakes are somebody's own machine
configuration. `catppuccin/nix` is a theme, but its repository is named
`nix`, which is exactly the shape of the personal configs the rule exists
to drop, and a hand-written line is better evidence than the name. So
`manual.txt` means "index this, whatever the pipeline concludes on its
own", and `blocklist.txt` below is its opposite: reach for `manual.txt`
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
is the exemption: one repository per line, re-resolved on every run
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
named `home-manager` and 110 are named `flake`; a name several of them
could equally mean identifies none of them, so a contested name goes to
nobody and every claimant gets the owner appended: `home-manager-rc-14`.

`names.txt` is where a person says which repository a name means. One entry
per line, the repository then the name:

```
nix-community/home-manager  home-manager
nixified-ai/flake           nixified-ai
```

That does two jobs. It hands a contested name to the project people mean
when they type it, which is the only way `home-manager` or `nixpkgs` gets a
bare name at all. And it replaces a derived name that says nothing:
`nixified-ai/flake` derives `flake` and `catppuccin/nix` derives `nix`.

A name of `-` is the third job, and it takes a bare name away:

```
akirak/git-hooks  -
```

`akirak/git-hooks` is then named `git-hooks-akirak` and nothing holds
`git-hooks`. Reach for that when one repository holds a name people
overwhelmingly use for a different project, which matters beyond the
attribute: an index name is what `unified` substitutes on, so a bare name
on the wrong repository rewrites that input for every flake declaring it.
319 indexed flakes declare `git-hooks` and mean `cachix/git-hooks.nix`.

Leaving a contested repository out is the normal case. It stays reachable
by its qualified name, and a bare name is reserved only when it carries
real information: `postgres` does not obviously mean `supabase/postgres`.

A name never changes once assigned, because consumers refer to flakes by
name — a repository that later gains stars does not take a bare name from
the one holding it. A `names.txt` line is the only thing that outranks
that, and it applies on the next run rather than whenever the row next
comes round for a refresh: the repository the line names takes the name,
and whoever was holding it is displaced to its qualified form.

## Sorted lists

`manual.txt`, `blocklist.txt`, `always.txt` and `names.txt` are all sorted
by [keep-sorted](https://github.com/google/keep-sorted). Each group of
entries is its own block, between a `# keep-sorted start case=no` line and
a `# keep-sorted end` line, so a group keeps the comment that says why its
lines are there and is still sorted within itself:

```
# The five foundations. Their revision is substituted into every indexed
# flake, so these are the pins with the widest reach.
# keep-sorted start case=no
hercules-ci/flake-parts
nix-systems/default
NixOS/flake-compat
NixOS/nixpkgs
numtide/flake-utils
# keep-sorted end
```

Add a line anywhere inside the block that fits it and run `nix fmt`, which
sorts it into place. `nix flake check` fails on a block left unsorted. The
markers are comments, so every tool that reads these files ignores them.
`case=no` sorts the way a reader reads: GitHub is case-insensitive about
owners, and case-sensitive order would file every `NixOS/` line ahead of
every lowercase one.
