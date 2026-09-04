#!/usr/bin/env python3
"""Generate index.json from the library and its pins.

flake.nix is static. What changes between releases is index.json: one line
per flake, mapping its attribute name to the `locked` attributes fetchTree
needs and a flag saying whether a computed lock is stored under locks/.
One entry per line keeps a diff readable when a flake is added, removed or
re-pinned.

A library row whose current revision has no pin falls back to the last
revision that did. An attribute name is API, and the reason a pin is
missing is usually that GitHub rate-limited the run that tried to make it,
so dropping the flake would take a working attribute away from consumers
over a transient failure upstream. The index keeps serving the last known
good revision until a new one pins. A flake only leaves the index by
leaving the library: blocklisted, reclassified, or gone from resolve.

Also writes unify.json, the names `unified` may substitute by input name,
and prunes what nothing references any more: pins and failures for
revisions no longer in the library, and stored locks no index entry uses.
"""

import argparse, collections, json, os, re, sys, time

from pin import flake_ref, lock_key, read_jsonl
from resolve import load_reserved, sanitize

# Markers around the status block in README.md.
STATUS_BEGIN = "<!-- BEGIN index-status -->"
STATUS_END = "<!-- END index-status -->"


def load_blocklist(path):
    if not os.path.exists(path):
        return set()
    return {l.strip() for l in open(path) if l.strip() and not l.startswith("#")}


def write_jsonl(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    os.replace(tmp, path)


def write_index(path, index):
    """One entry per line, sorted by name, so diffs stay per-flake."""
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("{\n")
        names = sorted(index)
        for i, name in enumerate(names):
            entry = json.dumps(index[name], sort_keys=True)
            sep = "," if i + 1 < len(names) else ""
            fh.write(f"  {json.dumps(name)}: {entry}{sep}\n")
        fh.write("}\n")
    os.replace(tmp, path)


def prune_locks(locks_dir, keys_in_use):
    removed = 0
    if not os.path.isdir(locks_dir):
        return removed
    for fname in os.listdir(locks_dir):
        if not fname.endswith(".json"):
            continue
        if fname[: -len(".json")] not in keys_in_use:
            os.remove(os.path.join(locks_dir, fname))
            removed += 1
    return removed


def unify_names(indexed, resolved, reserved):
    """The index names unification may substitute by input name.

    An index name used as an override key is a claim that an input called
    that means this flake. The claim holds when the index knows which
    repository the name means: one repository claims it, or a names.txt
    line hands it over. It does not hold for a name 26 repositories claim.
    "home" is one of those, 49 indexed flakes declare an input by that
    name, and substituting it replaced every one of them with a stranger's
    machine configuration.

    Contention is counted over the whole database rather than the index,
    because a repository classified personal still means the name does not
    identify one flake.

    A line counts only once the repository it names actually holds the
    name. resolve.py is what applies names.txt to a row, so between the
    commit that adds a line and the next pipeline run the index still
    holds the old assignment: a line handing "helix" to helix-editor/helix
    would otherwise vouch for an input name that izzqz/helix is still
    sitting on, which is the substitution the line exists to stop.
    """
    claims = collections.Counter(sanitize(row["repo"]) for row in resolved)
    names = [
        row["name"]
        for row in indexed
        if reserved.get((row["owner"].lower(), row["repo"].lower())) == row["name"]
        or claims[sanitize(row["repo"])] <= 1
    ]
    return sorted(names)


def write_unify(path, names):
    """One name per line, so a diff says which keys a run added or took."""
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("[\n")
        for i, name in enumerate(names):
            sep = "," if i + 1 < len(names) else ""
            fh.write(f"  {json.dumps(name)}{sep}\n")
        fh.write("]\n")
    os.replace(tmp, path)


def update_readme(path, stats):
    """Rewrite the status block between the markers, if the file has one."""
    if not os.path.exists(path):
        return
    text = open(path).read()
    pattern = re.compile(re.escape(STATUS_BEGIN) + ".*?" + re.escape(STATUS_END), re.S)
    if not pattern.search(text):
        return
    # The blank line after the opening marker is what prettier wants, and
    # `nix fmt` runs over this file in CI.
    lines = [
        STATUS_BEGIN,
        "",
        f"- **{stats['indexed']:,} flakes** in the index, from "
        f"**{stats['library']:,} in the library tier** "
        f"({stats['failed']:,} could not be pinned, {stats['unpinned']:,} not yet pinned)",
        f"- {stats['stored_locks']:,} ship no usable lock file and use one computed by Nix",
    ]
    # Only worth a line when it is not zero: a flake held back is a fault
    # upstream or a rate limit here, and either way it should be visible.
    if stats.get("stale"):
        lines.append(
            f"- {stats['stale']:,} held at an earlier revision, their newer one having failed to pin"
        )
    lines += [
        "- One `follows` line in your flake redirects `nixpkgs` in every one of them",
        f"- Last updated {time.strftime('%Y-%m-%d', time.gmtime())}",
        STATUS_END,
    ]
    block = "\n".join(lines)
    open(path, "w").write(pattern.sub(block, text))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", default="library.jsonl")
    ap.add_argument("--pins", default="pins.jsonl")
    ap.add_argument("--failures", default="failures.jsonl")
    ap.add_argument("--locks", default="locks")
    ap.add_argument("--blocklist", default="blocklist.txt")
    ap.add_argument("--index", default="index.json")
    ap.add_argument("--resolved", default="resolved.jsonl")
    ap.add_argument("--names", default="names.txt")
    ap.add_argument("--unify", default="unify.json")
    ap.add_argument("--readme", default="README.md")
    args = ap.parse_args()

    blocked = load_blocklist(args.blocklist)
    pins = {p["ref"]: p for p in read_jsonl(args.pins)}
    failures = {f["ref"]: f for f in read_jsonl(args.failures)}

    # The last pin recorded for each name, whatever revision it was for.
    # pins.jsonl is written in name order and holds one row per reference,
    # so this is what a flake falls back to when its current reference has
    # no pin of its own.
    last_good = {}
    for pin in pins.values():
        if "name" in pin:
            last_good[pin["name"]] = pin

    index = {}
    # The library rows that made it in, for the override map below.
    indexed = []
    # Every reference the output still needs: the library's own, plus the
    # older ones the fallback keeps alive so pruning does not collect them.
    keep_refs = {}
    stats = {
        "library": 0,
        "indexed": 0,
        "failed": 0,
        "unpinned": 0,
        "stored_locks": 0,
        "stale": 0,
    }
    for row in read_jsonl(args.library):
        name = row["name"]
        if name in blocked:
            continue
        ref = flake_ref(row)
        keep_refs[ref] = name
        stats["library"] += 1

        pin = pins.get(ref)
        if pin is None:
            stats["failed" if ref in failures else "unpinned"] += 1
            # Hold the flake at the last revision that pinned, rather than
            # letting a failure upstream remove the attribute.
            pin = last_good.get(name)
            if pin is None:
                continue
            keep_refs[pin["ref"]] = name
            stats["stale"] += 1

        entry = {"locked": pin["locked"]}
        if pin.get("lock"):
            entry["lock"] = True
            stats["stored_locks"] += 1
        index[name] = entry
        indexed.append(row)
        stats["indexed"] += 1

    write_index(args.index, index)

    # The names `unified` may substitute. Not every index name: a name
    # several repositories claim identifies none of them.
    unify = unify_names(
        indexed, list(read_jsonl(args.resolved)), load_reserved(args.names)
    )
    write_unify(args.unify, unify)
    stats["unify_keys"] = len(unify)

    # Keep the databases to what the library still references, and carry
    # the current name on each row so the files read well on their own.
    def current(rows):
        kept = []
        for ref, row in rows.items():
            if ref not in keep_refs:
                continue
            kept.append({**row, "name": keep_refs[ref]})
        return sorted(kept, key=lambda r: r["name"])

    write_jsonl(args.pins, current(pins))
    # A failure that was later retried successfully is superseded by its pin.
    write_jsonl(args.failures, [f for f in current(failures) if f["ref"] not in pins])
    keys_in_use = {lock_key(e["locked"]) for e in index.values() if e.get("lock")}
    stats["pruned_locks"] = prune_locks(args.locks, keys_in_use)

    update_readme(args.readme, stats)
    print("# " + json.dumps(stats), file=sys.stderr)


if __name__ == "__main__":
    main()
