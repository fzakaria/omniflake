# The three policies over one index: `flakes`, `pinned` and `unified`, and
# the functions they are built from.
#
# flake.nix calls this with index.json. tests/loader.nix calls it with a
# fixture index at fixed revisions, so the loader test runs this exact code
# without depending on what upstream authors pushed since the last refresh.
{
  # Attribute name to index entry: the entry's `locked` attributes and
  # whether a computed lock is stored under `locks`.
  index,
  # The names `unified` substitutes by input name. See unifyAll.
  unifyNames,
  # The directory of stored lock files, read only for an entry with
  # `lock = true`.
  locks,
  # Inputs replaced by name in every subflake, at every depth.
  foundations,
}:
let
  inherit (builtins)
    attrNames
    fromJSON
    listToAttrs
    readFile
    replaceStrings
    ;

  names = attrNames index;

  load = import ./load.nix;

  # Mirrors lock_key in tools/pin.py: stored locks are named after the
  # revision, or the narHash for the rare input type without one.
  lockKey =
    locked: if locked ? rev then locked.rev else replaceStrings [ "/" "=" ] [ "_" "" ] locked.narHash;

  storedLock =
    entry:
    if entry.lock or false then fromJSON (readFile (locks + "/${lockKey entry.locked}.json")) else null;

  loadWith =
    overrides: name:
    let
      entry = index.${name};
    in
    load {
      inherit (entry) locked;
      lock = storedLock entry;
      inherit overrides;
    };

  # Every flake under one policy, as a lazy attribute set.
  withOverrides =
    overrides:
    listToAttrs (
      map (name: {
        inherit name;
        value = loadWith overrides name;
      }) names
    );

  # Every flake under one policy, plus every flake whose name the index
  # is sure of overriding that name: a graph reaches one home-manager,
  # one disko, one treefmt-nix, rather than the revision each author
  # happened to lock.
  #
  # The overrides are the set being defined, so a substituted flake's
  # own graph is unified too, at any depth, rather than stopping at the
  # five foundations.
  #
  # The foundations win over the index, and the caller's `extra` wins
  # over both. All five foundation names are indexed flakes as well, and
  # taking them from the index would quietly break the one thing a
  # consumer controls: `inputs.omniflake.inputs.nixpkgs.follows` reaches
  # a declared input and nothing else.
  unifyAll =
    extra:
    let
      fromIndex = listToAttrs (
        map (name: {
          inherit name;
          value = all.${name};
        }) unifyNames
      );
      all = withOverrides (fromIndex // foundations // extra);
    in
    all;
in
{
  inherit loadWith withOverrides unifyAll;

  # The three policies, each keyed by attribute name.
  flakes = withOverrides foundations;
  pinned = withOverrides { };
  unified = unifyAll { };
}
