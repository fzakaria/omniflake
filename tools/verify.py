#!/usr/bin/env python3
"""Verify the pins a change added or modified, against the real source.

The check workflow proves index.json is consistent with pins.jsonl, but
takes the pins rows themselves on faith: pin.py never re-derives a
revision it has already seen, and tests/index.nix is deliberately
offline, so a wrong rev or narHash would pass every other gate. This
script closes that gap for a change under review:

- every pin added or modified relative to --base is re-derived with a
  fresh `nix flake metadata` run and compared field by field, computed
  lock file included;
- every attribute name the change adds to index.json is forced through
  the loader, which fetches the tree and so checks the narHash against
  real content.

Only *new* names are evaluated: a re-pinned existing flake may fail to
evaluate for reasons of its own (see tools/sample.py), but a flake being
added deliberately has no excuse.

Usage: verify.py [--base REV] [--jobs N] [--flake REF]
Exits non-zero on any disagreement. Appends a summary line to
$GITHUB_STEP_SUMMARY when that is set.
"""

import argparse, concurrent.futures, json, os, subprocess, sys, tempfile
import urllib.request

from pin import lock_key, pin_one, read_jsonl

DEFAULT_BASE = "origin/main"
DEFAULT_JOBS = 4
# The attribute forced per new name; mirrors tools/sample.py.
ATTR = "sourceInfo.narHash"
STDERR_TAIL = 200


def file_at(base, path):
    """The content of a committed file at the base revision."""
    proc = subprocess.run(
        ["git", "show", f"{base}:{path}"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        sys.exit(f"cannot read {path} at {base}: {proc.stderr.strip()}")
    return proc.stdout


def pins_at(base):
    """pins.jsonl as of the base revision, keyed by exact flake reference.

    pins.jsonl is not committed; data-pins.json is, and it names the release
    asset holding the bytes that went with that commit. So the base's pins
    are one indirection away: read the manifest at the base revision, then
    download the asset it pins. The narHash is not re-checked here — this
    is a comparison baseline, and a tampered baseline can only cause a pin
    to be re-derived needlessly, which is what this tool does anyway.
    """
    manifest = json.loads(file_at(base, "data-pins.json"))
    pin = manifest["files"].get("pins.jsonl")
    if not pin:
        sys.exit(f"data-pins.json at {base} has no pin for pins.jsonl")
    url = f"{manifest['baseUrl']}/{pin['tag']}/pins.jsonl"
    try:
        with urllib.request.urlopen(url, timeout=120) as fh:
            text = fh.read().decode()
    except Exception as e:
        sys.exit(f"cannot fetch {url}: {e}")

    rows = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        row = json.loads(line)
        rows[row["ref"]] = row
    return rows


def verify_pin(pin, scratch_locks):
    """Re-derive one committed pin. Returns an error string, or None."""
    status, fresh = pin_one(
        {"name": pin["name"], "url": pin["ref"]},
        use_token=False,
        locks_dir=scratch_locks,
    )
    if status != "ok":
        return f"{pin['ref']}: re-pin failed: {fresh['error']}"

    # The locked set and the lock flag must agree exactly. lock_nodes is
    # compared only where the committed row has one, since rows written
    # before the field existed lack it.
    fields = ["locked", "lock"] + (["lock_nodes"] if "lock_nodes" in pin else [])
    for field in fields:
        if fresh.get(field) != pin.get(field):
            return (
                f"{pin['ref']}: {field} mismatch: "
                f"committed {pin.get(field)!r}, derived {fresh.get(field)!r}"
            )

    # Where Nix had to compute a lock, the stored copy must match it too.
    if fresh["lock"]:
        key = lock_key(fresh["locked"])
        committed_path = os.path.join("locks", f"{key}.json")
        if not os.path.exists(committed_path):
            return f"{pin['ref']}: locks/{key}.json is missing"
        committed = json.load(open(committed_path))
        derived = json.load(open(os.path.join(scratch_locks, f"{key}.json")))
        if committed != derived:
            return f"{pin['ref']}: stored lock locks/{key}.json differs from the computed one"

    print(f"ok   {pin['ref']}", flush=True)
    return None


def evaluate_new(flake, name):
    """Force one new name through the loader. Returns an error string, or None."""
    expr = f"{flake}#flakes.{name}.{ATTR}"
    # An evaluation error quotes the flake's own files, which are arbitrary
    # bytes; undecodable ones are replaced rather than raising.
    proc = subprocess.run(
        ["nix", "eval", "--raw", expr],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if proc.returncode == 0:
        print(f"ok   {expr}", flush=True)
        return None
    lines = [l for l in proc.stderr.splitlines() if l.strip()]
    tail = lines[-1][:STDERR_TAIL] if lines else "(no output)"
    return f"{expr}: {tail}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--base", default=DEFAULT_BASE, help="revision to diff pins.jsonl against"
    )
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    ap.add_argument(
        "--flake", default=".", help="flake reference the new names are evaluated from"
    )
    args = ap.parse_args()

    # What the change did: pins added or modified, and names added to the
    # index. A removed pin needs no verification.
    base_pins = pins_at(args.base)
    head_pins = {p["ref"]: p for p in read_jsonl("pins.jsonl")}
    changed = [p for ref, p in sorted(head_pins.items()) if base_pins.get(ref) != p]

    base_names = set(json.loads(file_at(args.base, "index.json")))
    head_names = set(json.load(open("index.json")))
    new_names = sorted(head_names - base_names)

    if not changed and not new_names:
        print(f"no pins changed against {args.base}")
        return

    print(f"re-deriving {len(changed)} pin(s), evaluating {len(new_names)} new name(s)")

    errors = []

    # Each re-pin is one `nix flake metadata` run. Computed locks land in
    # a scratch directory, so verification never writes into locks/.
    with tempfile.TemporaryDirectory() as scratch_locks:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for error in pool.map(lambda p: verify_pin(p, scratch_locks), changed):
                if error:
                    errors.append(error)

    # New names are forced through the loader the way a consumer uses them.
    for name in new_names:
        error = evaluate_new(args.flake, name)
        if error:
            errors.append(error)

    line = (
        f"verified {len(changed)} changed pin(s) and {len(new_names)} new name(s): "
        + (f"{len(errors)} FAILED" if errors else "all agree")
    )
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(line + "\n")

    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
