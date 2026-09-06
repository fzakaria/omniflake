#!/usr/bin/env python3
"""Evaluate a random sample of indexed flakes and report how many work.

Informational: the index holds third-party flakes, and some do not evaluate
even from their own lock (an eager reference to a system their nixpkgs
dropped, say), so a random sample cannot be a pass or fail signal. Each
flake is tried as `pinned` and as `flakes`, and the summary line says how
many evaluate both ways, only as pinned, or not at all.

Usage: sample.py [--size N] [--flake REF]
Appends the summary to $GITHUB_STEP_SUMMARY when that is set.
"""

import argparse, json, os, random, subprocess

# The attribute forced per flake. Reaching sourceInfo through the loader
# evaluates the flake's outputs to an attribute set, which is the test.
ATTR = "sourceInfo.narHash"
STDERR_TAIL = 200


def evaluate(flake, attr, name):
    expr = f"{flake}#{attr}.{name}.{ATTR}"
    # An evaluation error quotes the offending flake's own files, which are
    # arbitrary bytes, so a strict decode raises UnicodeDecodeError instead
    # of reporting the failure. Undecodable bytes are replaced.
    proc = subprocess.run(
        ["nix", "eval", "--raw", expr],
        capture_output=True,
        text=True,
        errors="replace",
    )
    ok = proc.returncode == 0
    print(("ok " if ok else "FAIL ") + expr, flush=True)
    if not ok:
        lines = [l for l in proc.stderr.splitlines() if l.strip()]
        print(
            "     " + (lines[-1][:STDERR_TAIL] if lines else "(no output)"), flush=True
        )
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=25)
    ap.add_argument("--flake", default=".")
    args = ap.parse_args()

    names = json.loads(
        subprocess.check_output(
            ["nix", "eval", "--json", f"{args.flake}#lib.names"], text=True
        )
    )
    sample = random.sample(names, min(args.size, len(names)))

    both = pinned_only = neither = 0
    for name in sample:
        as_pinned = evaluate(args.flake, "pinned", name)
        as_flakes = evaluate(args.flake, "flakes", name)
        if as_pinned and as_flakes:
            both += 1
        elif as_pinned:
            pinned_only += 1
        else:
            neither += 1

    line = (
        f"sample of {len(sample)}: {both} evaluate, "
        f"{pinned_only} only as pinned, {neither} not at all"
    )
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(line + "\n")


if __name__ == "__main__":
    main()
