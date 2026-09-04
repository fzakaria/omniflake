#!/usr/bin/env python3
"""Add flakes by hand, outside of GitHub search.

Search only finds what people remembered to tag, and it cannot see GitLab,
sourcehut, or a private host at all. manual.txt is the escape hatch and is
committed alongside the database. One entry per line, blank lines and #
comments ignored:

    nix-community/disko          a GitHub repo, pinned to its default branch
    github:nix-community/disko   the same thing, spelled as a flake ref
    github:owner/repo/v1.2.3     a GitHub repo pinned to a ref you choose
    gitlab:owner/repo            anything else Nix can fetch
    git+https://example.com/x    likewise

A GitHub repository's default branch is emitted as a *candidate*, however
it is spelled, so resolve.py pins it and names it like any harvested repo.
Anything else -- a pinned ref, a subdirectory, another forge -- cannot go
through the GitHub API, so it is resolved here with `nix flake metadata`
and emitted as a finished database entry. resolve.py names those too; this
script only says what they are.

Either way the row carries the repository's star count, which is asked for
here because nothing else in the pipeline will. harvest.py is what records
stars, and a flake listed by hand is usually one harvest cannot see:
hyprwm/Hyprland has 38,344 and was written down as having none. Worse, a
repository that search does find and this file also lists was written down
twice, and merge-candidates.py lets the later row win, so listing
catppuccin/nix replaced the 756 stars harvest knew about with zero.

Listing a flake here also exempts it from classify.py's guess at what is
somebody's machine configuration, so manual.txt reads as "index this,
whatever the pipeline concludes on its own" and blocklist.txt as its
opposite. The name heuristic cannot tell that catppuccin/nix is a theme
rather than a personal config, and a person writing the line down can.

    --candidates FILE   append bare owner/repo entries here
    --resolved FILE     append fully-resolved entries here
"""

import argparse, json, os, re, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request

BARE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# A github: reference to a plain repository, with no ref, subdirectory or
# query string. Those name the default branch, which is exactly what a
# bare owner/repo names.
PLAIN_GITHUB = re.compile(r"^github:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?$")

# One repository's metadata. A manual list is a handful of lines, so a
# request each is simpler than the GraphQL batching resolve.py needs and
# works without a token; GH_TOKEN is used when the environment has one.
REPO_URL = "https://api.github.com/repos/{owner}/{repo}"
REPO_TIMEOUT_SECONDS = 15


def as_bare(entry):
    """Rewrite github:owner/repo as owner/repo; leave anything else alone.

    The two forms name one thing, the default branch of a GitHub
    repository, so they must take one path through the pipeline. Without
    this the prefixed form went to resolve_ref below instead of the GraphQL
    path, which meant a different name, no names.txt, and no exemption from
    classify.py -- three surprises for a line a person wrote expecting the
    spelling not to matter.

    A ref, a subdirectory or a query string makes the reference something
    the GraphQL path cannot resolve, and those are left as they are.
    """
    match = PLAIN_GITHUB.match(entry)
    if not match:
        return entry
    return f"{match.group(1)}/{match.group(2)}"


def read_entries(path):
    """Yield the flake references a manual list names, comments stripped.

    Normalized on the way out, so every caller sees one spelling per
    repository. See as_bare.
    """
    try:
        lines = open(path).read().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        entry = line.split("#", 1)[0].strip()
        if entry:
            yield as_bare(entry)


def listed_repos(path):
    """The (owner, repo) pairs a manual list names, lowercased.

    Only bare owner/repo entries appear here, because those are the ones
    that go on to be harvested and named like any other candidate --
    github:owner/repo among them, which read_entries has already rewritten.
    A ref this script resolves itself carries a "manual" flag on its own row
    instead. classify.py reads both to tell which rows a person put in the
    index by hand.
    """
    pairs = set()
    for entry in read_entries(path):
        if BARE.match(entry):
            owner, repo = entry.split("/", 1)
            pairs.add((owner.lower(), repo.lower()))
    return pairs


def github_ref(entry):
    """The (owner, repo) a manual entry names on GitHub, or None.

    A bare owner/repo is GitHub by definition, and so is a github: flake
    reference, with or without a ref or a query string. Every other forge
    has a star count somewhere and it is not at the endpoint below, so
    those are left alone rather than guessed at.
    """
    if BARE.match(entry):
        owner, repo = entry.split("/", 1)
        return owner, repo
    if not entry.startswith("github:"):
        return None
    path = entry[len("github:") :].split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def fetch_stars(owner, repo):
    """A repository's star count, or None if GitHub did not say.

    None rather than zero: the two mean different things to a row that
    already records a count, and a failed request must not be written down
    as a repository nobody has starred.
    """
    request = urllib.request.Request(REPO_URL.format(owner=owner, repo=repo))
    request.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=REPO_TIMEOUT_SECONDS) as response:
            return json.load(response).get("stargazers_count")
    except Exception as e:
        print(f"# could not read stars for {owner}/{repo}: {e}", file=sys.stderr)
        return None


def star_counts(entries, fetch=fetch_stars):
    """{(owner, repo): stars} for the entries GitHub can be asked about.

    A repository named twice, once bare and once as a reference, is asked
    about once. A repository the lookup could not answer for is absent.
    """
    counts = {}
    for entry in entries:
        ref = github_ref(entry)
        if ref is None or ref in counts:
            continue
        stars = fetch(*ref)
        if stars is not None:
            counts[ref] = stars
    return counts


def sanitize(name):
    """Flake ref -> a legal Nix attribute name."""
    out = "".join(c if (c.isalnum() or c in "-_") else "-" for c in name.lower())
    if not out or not out[0].isalpha():
        out = "f-" + out
    return out


def locked_ref(locked, url):
    """The (owner, repo) a locked flake reference names.

    github, gitlab and sourcehut hand back both. Every other fetcher hands
    back a url and nothing else, and resolve.py needs the pair for two
    things: it keys a row on it, and it falls back to <repo>-<owner> when
    the repository name is contested. An empty owner makes that fallback
    read "myrepo-f-". The last two path segments of the url stand in, and
    the host stands in for the owner when the path holds only one.
    """
    owner, repo = locked.get("owner") or "", locked.get("repo") or ""
    if owner and repo:
        return owner, repo

    parsed = urllib.parse.urlparse(locked.get("url") or url)
    parts = [p for p in parsed.path.split("/") if p]
    # A bare clone url ends in .git; the repository is not called that.
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][: -len(".git")]

    repo = repo or (parts[-1] if parts else parsed.netloc)
    owner = owner or (parts[-2] if len(parts) > 1 else parsed.netloc)
    return owner, repo


def resolve_ref(url, stars=0):
    """Pin an arbitrary flake ref with `nix flake metadata`."""
    try:
        out = subprocess.run(
            ["nix", "flake", "metadata", "--json", url],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if out.returncode != 0:
            print(
                f"# could not resolve {url}: {out.stderr.strip()[:120]}",
                file=sys.stderr,
            )
            return None
        meta = json.loads(out.stdout)
    except Exception as e:
        print(f"# could not resolve {url}: {e}", file=sys.stderr)
        return None

    locked = meta.get("locked", {})
    # The root node of its own lock names the flake's declared inputs.
    inputs = sorted(
        (
            meta.get("locks", {}).get("nodes", {}).get("root", {}).get("inputs", {})
        ).keys()
    )
    owner, repo = locked_ref(locked, url)

    # Pin to the exact revision. An unpinned url would re-resolve on every
    # consumer lock, defeating the point of shipping a fixed graph.
    rev = locked.get("rev", "")
    kind = locked.get("type", "")
    if kind in ("github", "gitlab", "sourcehut") and locked.get("owner") and rev:
        pinned = f'{kind}:{locked["owner"]}/{locked["repo"]}/{rev}'
    elif locked.get("url") and rev:
        sep = "&" if "?" in locked["url"] else "?"
        pinned = f'{locked["url"]}{sep}rev={rev}'
    else:
        pinned = url

    return {
        # Provisional. resolve.py names every row it writes, this one
        # included, because only it knows which names the database already
        # hands out and what names.txt says about them.
        "name": sanitize(repo),
        "owner": owner,
        "repo": repo,
        "rev": locked.get("rev", ""),
        # An explicit url wins over the constructed github: ref.
        "url": pinned,
        "inputs": inputs,
        "stars": stars,
        "manual": True,
        # The site's "last checked" date; resolve.py stamps harvested rows
        # the same way. Refreshed on every run, since manual entries are
        # re-resolved each time.
        "resolved_at": int(time.time()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manual", nargs="?", default="manual.txt")
    ap.add_argument("--candidates")
    ap.add_argument("--resolved")
    args = ap.parse_args()

    if not os.path.exists(args.manual):
        print(f"# no {args.manual}; nothing to add", file=sys.stderr)
        return

    # Every entry's star count, before any row is written. Without this a
    # manual entry is recorded as having no stars, and merge-candidates
    # lets the later row win, so listing a repository that search already
    # found used to overwrite the count harvest.py had for it.
    entries = list(read_entries(args.manual))
    counts = star_counts(entries)

    candidates, resolved = [], []
    for entry in entries:
        ref = github_ref(entry)
        stars = counts.get(ref, 0) if ref else 0
        if BARE.match(entry):
            owner, repo = entry.split("/", 1)
            candidates.append({"owner": owner, "repo": repo, "stars": stars})
            continue
        got = resolve_ref(entry, stars)
        if got:
            resolved.append(got)

    if args.candidates and candidates:
        with open(args.candidates, "a") as fh:
            for c in candidates:
                fh.write(json.dumps(c) + "\n")
    if args.resolved and resolved:
        with open(args.resolved, "a") as fh:
            for r in resolved:
                fh.write(json.dumps(r) + "\n")

    print(
        f"# manual: {len(candidates)} candidate(s), {len(resolved)} resolved",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
