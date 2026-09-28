# Exercises lib/load.nix on real flakes, which fetches their trees at
# evaluation time. Four shapes: a flake that ships a current lock (nh), one
# whose inputs the default policy leaves alone under `pinned` (sops-nix),
# one with a nested input whose nixpkgs the default policy has to reach
# (agenix -> home-manager -> nixpkgs), and one that `unified` has to reach
# past the foundations for (devenv -> nixd -> treefmt-nix).
#
# The policies are built over a fixture index at fixed revisions rather
# than over index.json. The assertions are about the shape of each
# subject's inputs, and the live index moves every subject to whatever its
# author pushed last: agenix dropping its home-manager input broke this
# test with no change to the loader.
{ self, system }:
let
  inherit (builtins) fromJSON readFile;

  ours = self.inputs.nixpkgs.rev;

  # The same policy code flake.nix uses, over the fixture. No fixture entry
  # has a stored lock, so nothing under the locks directory is read.
  policies = import ../lib/policies.nix {
    index = fromJSON (readFile ./fixtures/loader/index.json);
    unifyNames = fromJSON (readFile ./fixtures/loader/unify.json);
    locks = ./fixtures/loader/locks;
    inherit (self.lib) foundations;
  };

  nh = policies.flakes.nh;
  sops = policies.flakes.sops-nix;
  agenix = policies.flakes.agenix;
  pinnedSops = policies.pinned.sops-nix;

  # `unified` overrides on every name the index vouches for, and the flake
  # it substitutes is itself unified: devenv's nixd comes from the index,
  # and that nixd's treefmt-nix does too, rather than the one nixd locked.
  devenv = policies.unified.devenv;
in
# A flake evaluates to the shape Nix gives one.
assert nh._type == "flake";
assert nh ? packages.${system};
# Unification by name reaches a direct input...
assert nh.inputs.nixpkgs.rev == ours;
# ...and a nested one, without a follows line anywhere.
assert agenix.inputs.home-manager.inputs.nixpkgs.rev == ours;
# An input the policy does not name keeps the author's pin.
assert agenix.inputs.darwin.rev != ours;
# A flake evaluates from its own lock...
assert sops ? nixosModules.sops;
# ...and `pinned` leaves even nixpkgs on what that lock says.
assert pinnedSops.inputs.nixpkgs.rev != ours;
# `unified` substitutes a name the foundations do not cover...
assert devenv.inputs.cachix.rev == policies.unified.cachix.rev;
# ...and keeps substituting inside what it substituted.
assert devenv.inputs.nixd.inputs.treefmt-nix.rev == policies.unified.treefmt-nix.rev;
# A name the index does not vouch for is left alone at any depth: several
# repositories are named git-hooks and cachix means none of the others.
assert devenv.inputs.cachix.inputs.git-hooks.rev == policies.pinned.cachix.inputs.git-hooks.rev;
# A foundation still wins over the index entry of the same name, so
# `inputs.omniflake.inputs.nixpkgs.follows` reaches `unified` as well.
assert devenv.inputs.nixpkgs.rev == ours;
{
  nixpkgs = ours;
  checked = [
    "nh"
    "sops-nix"
    "agenix"
    "devenv"
    "nixd"
  ];
}
