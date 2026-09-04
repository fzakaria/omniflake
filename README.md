# omniflake

> Background: [One flake to rule them all](https://fzakaria.com/2026/08/28/one-flake-to-rule-them-all).

Thousands of Nix flakes behind one flake input. Add omniflake once and use
any indexed flake by name; a flake is fetched only when you evaluate
something from it.

```nix
{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.omniflake.url = "github:fzakaria/omniflake";
  inputs.omniflake.inputs.nixpkgs.follows = "nixpkgs";

  outputs = { self, nixpkgs, omniflake, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      modules = [
        omniflake.flakes.home-manager.nixosModules.home-manager
        omniflake.flakes.disko.nixosModules.disko
        omniflake.flakes.sops-nix.nixosModules.sops
      ];
    };
  };
}
```

**Documentation:** [How to use it](./docs/using.md) ·
[How it works](./docs/how-it-works.md) ·
[Unification](./docs/unification.md) ·
[Adding or removing a flake](./docs/adding-a-flake.md) ·
[Building the index](./docs/building-the-index.md) ·
[Caveats](./docs/caveats.md)

## Status

![check workflow](https://github.com/fzakaria/omniflake/actions/workflows/check.yml/badge.svg?branch=main)
![update workflow](https://github.com/fzakaria/omniflake/actions/workflows/update.yml/badge.svg?branch=main)

<!-- BEGIN index-status -->

- **16,132 flakes** in the index, from **16,348 in the library tier** (218 could not be pinned, 0 not yet pinned)
- 1,615 ship no usable lock file and use one computed by Nix
- 2 held at an earlier revision, their newer one having failed to pin
- One `follows` line in your flake redirects `nixpkgs` in every one of them
- Last updated 2026-09-04
<!-- END index-status -->

## Quickstart

```console
# number of indexed flakes
$ nix eval github:fzakaria/omniflake#lib.count

# run a package from one of them
$ nix run 'github:fzakaria/omniflake#flakes.nh.packages.x86_64-linux.default'
```

Adding omniflake as an input adds six nodes to your `flake.lock`: omniflake
and its five inputs. The indexed flakes are not inputs and do not appear in
your lock file.

```console
$ time nix flake lock
real    0m1.5s
```

## Attributes

| attribute                          | `nixpkgs` and the other four foundation inputs come from       |
| ---------------------------------- | -------------------------------------------------------------- |
| `omniflake.flakes.<name>`          | omniflake's inputs, substituted at every depth                 |
| `omniflake.pinned.<name>`          | the flake's own lock file                                      |
| `omniflake.unified.<name>`         | omniflake's inputs, and every other input name the index knows |
| `omniflake.lib.load "<name>" {…}`  | the attribute set you pass; `{ }` means the flake's own lock   |
| `omniflake.lib.withOverrides {…}`  | the attribute set you pass, for every flake                    |
| `omniflake.lib.unifyAll {…}`       | the index, then the foundations, then the set you pass         |
| `omniflake.lib.names`, `lib.count` | metadata; forces no fetch                                      |

`<name>` has three spellings, and they reach the same thunk:

```nix
omniflake.flakes.home-manager                          # bare
omniflake.flakes."github:nix-community/home-manager"   # qualified
omniflake.github.flakes.nix-community.home-manager     # nested
```

Every flake answers to the last two. A bare name is assigned only when one
repository claims it, so `home-manager`, claimed by 61 repositories, is a
name handed over by [`names.txt`](./names.txt) rather than won on stars.

See [Unification](./docs/unification.md) for what is substituted and why.

## Caveats

Using omniflake means trusting this repository's pinning of every indexed
flake. Attribute names are stable API. See [Caveats](./docs/caveats.md)
before depending on it.
