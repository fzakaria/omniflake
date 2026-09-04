#!/usr/bin/env python3
"""Resolve harvested candidates into pinned, verified flake references.

For each repo this needs three things, all obtainable in one GraphQL round
trip per batch: the default branch commit (to pin a rev), whether a root
flake.nix exists (to reject non-flakes), and flake.lock (whose root node
lists the flake's declared input names, which is what `follows` needs).

This is incremental. resolved.jsonl is a database that is kept and added
to, not regenerated: pass --known to skip repos already in it, and
--refresh to additionally re-pin the ones already known. Rows that were
resolved outside the GitHub API (tools/manual.py writes them) come in via
--merge and win over any other row for the same repository. Their names are
decided here too, by the same rules as everything else: manual.py sees one
flake at a time and cannot know what the database already hands out.

A repository that is checked and cannot be used leaves no row in the
known set, so without --rejects nothing distinguishes "never checked"
from "checked and rejected" and the candidate is queried again on every
run, forever. rejects.jsonl is that record: one row per repository this
script looked at and could not use, keyed by (owner, repo).

The record is a timestamp rather than a verdict, which is the opposite of
what pin.py's failures.jsonl holds. A failed pin is keyed by an immutable
revision and can be skipped forever; a rejected repository is keyed by the
repository, and it can add a flake.nix tomorrow. So each run re-checks the
--recheck-oldest rows checked longest ago, and a row is cleared the moment
its repository resolves.

A bare attribute name is only assigned when one repository claims it.
4,006 of the names in use are claimed by more than one repository -- 61
are named home-manager, 110 are named flake -- and a bare name several
repositories could equally mean identifies none of them, so a contested
name goes to nobody and every claimant gets <repo>-<owner>. names.txt is
where a person overrides that and says which repository a name means. Every
row goes through this, merged rows included, so a name is unique across the
database no matter which path resolved the row.

Attribute names are otherwise sticky, which matters because they are API.
A name already assigned in the known set keeps its owner forever, so a
repo that later gains stars cannot take a bare name out from under a
consumer that already writes omniflake.flakes.<name>. A names.txt line is
the one thing that outranks stickiness. It is applied to the database
before anything is resolved, since a known row is carried over rather than
named again: the repository the line names takes the name, and whoever
held it is displaced to its qualified form. A line whose name is "-" asks
for no bare name at all.

The rolling refresh is bounded rather than time-based, so a flake comes
round about every eight days. always.txt names the repositories that must
not wait that long -- nixpkgs, home-manager and the rest of the pins people
watch. Those are re-resolved every run and do not spend the
--refresh-oldest budget, so the cadence for everything else is untouched.

Every row's star count comes from the same query that resolves it, so it
is current as of the run that wrote the row. harvest.py's count decides
nothing but the order candidates are processed in.

Output: JSON lines of {name, owner, repo, rev, inputs, stars}, sorted by
attribute name. Processing order still decides which repo wins a new name
(highest-starred first), but the file is written in name order so a run
that re-resolves 2,000 rows produces a 2,000-line diff instead of moving
every row that follows them. Keys are sorted too, so a row's bytes do not
depend on how the dict was built.
"""

import argparse, json, os, subprocess, sys, time, collections
import urllib.error, urllib.parse, urllib.request

BATCH = 40
# Skip absurd locks; they are almost always vendored monorepos.
MAX_LOCK_BYTES = 2_000_000

QUERY_HEAD = "query {"
QUERY_TAIL = "}"


def repo_fragment(alias, owner, repo):
    """One aliased repository selection: stars, HEAD oid, flake.nix, flake.lock.

    stargazerCount is a scalar on a repository already being fetched, so it
    costs no request and does not change the query's node count. It is the
    only thing that keeps a star count current: harvest.py records one when
    it first finds a repository and never looks at that repository again.
    """
    return f"""
  {alias}: repository(owner: "{owner}", name: "{repo}") {{
    stargazerCount
    defaultBranchRef {{ target {{ oid }} }}
    flakeNix: object(expression: "HEAD:flake.nix") {{ __typename }}
    flakeLock: object(expression: "HEAD:flake.lock") {{
      ... on Blob {{ text byteSize }}
    }}
  }}"""


def read_token():
    """Read the GitHub token once, in-process.

    Every `gh` invocation asks the system keyring to unlock, so shelling out
    per batch means one prompt per batch. Read it once instead.
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token.strip()
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=60
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


TOKEN = read_token()


def run_batch(batch):
    """Issue one GraphQL query for up to BATCH repos; return the data map."""
    parts = [repo_fragment(f"r{i}", b["owner"], b["repo"]) for i, b in enumerate(batch)]
    query = QUERY_HEAD + "\n".join(parts) + QUERY_TAIL
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request("https://api.github.com/graphql", data=body)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as fh:
                return (json.loads(fh.read().decode()) or {}).get("data") or {}
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 502):
                time.sleep(10 * (attempt + 1))
                continue
            return {}
        except Exception:
            time.sleep(3)
    return {}


def sanitize(repo):
    """GitHub repo name -> a legal, readable Nix attribute name."""
    name = repo.lower()
    out = "".join(c if (c.isalnum() or c in "-_") else "-" for c in name)
    # A Nix identifier may not start with a digit or dash.
    if not out or not out[0].isalpha():
        out = "f-" + out
    return out


# A names.txt line whose name is this asks for no bare name at all: the
# repository is named as though its repository name were contested.
DENY = "-"


def qualified_name(owner, repo):
    """The name a repository gets when it does not hold the bare one."""
    return f"{sanitize(repo)}-{sanitize(owner)}"


# The flake-reference prefixes that still spell a repository as
# owner/repo. A url-shaped reference (git+https://host/team/proj) does not:
# its owner comes out of the path, so it is written bare in these files.
FORGE_PREFIXES = ("github:", "gitlab:", "sourcehut:")


def repo_key(field):
    """The (owner, repo) a hand-written line names, or None.

    names.txt and always.txt both key on a repository, and a person
    reaching for either has usually just been reading manual.txt, where the
    same repository is spelled github:owner/repo or pinned to a ref. Every
    spelling that names a repository as owner/repo is read as that
    repository. A ref is dropped: neither file has anything to say about
    one, and a row is keyed on the repository whatever it is pinned to.

    None is what makes the caller warn, and it is the point of parsing this
    at all. Both parsers used to look for a "/" and split on the first one,
    so github:NixOS/nixpkgs passed as the repository "nixpkgs" owned by
    "github:NixOS", matched nothing, and did nothing on every run without
    ever saying so.

    Lowercased, because GitHub is case-insensitive about owners and
    repositories and both files are written by hand.
    """
    field = field.split("?", 1)[0].split("#", 1)[0]

    forge = next((p for p in FORGE_PREFIXES if field.startswith(p)), None)
    if forge:
        field = field[len(forge) :]

    parts = [p for p in field.split("/") if p]

    # owner/repo, and owner/repo/ref only from a reference that said which
    # forge it is on. A bare line of three segments is a typo, not a pin.
    if len(parts) != 2 and not (len(parts) == 3 and forge):
        return None
    # Anything still carrying a colon is a scheme this does not know, and
    # guessing at where its owner and repository are would be worse than
    # asking for the bare form.
    if ":" in parts[0] or ":" in parts[1]:
        return None
    return parts[0].lower(), parts[1].lower()


def load_reserved_entries(lines):
    """Parse names.txt lines into {(owner, repo): attribute name}.

    One entry per line, the repository then the name, whitespace separated;
    blank lines and # comments ignored. The repository is spelled as
    repo_key accepts it. A name of "-" is read as the repository's
    qualified name, which is how a line says that a bare name belongs to
    nobody -- akirak/git-hooks holds "git-hooks" and 319 indexed flakes
    mean cachix/git-hooks.nix by it.
    """
    reserved = {}
    for line in lines:
        entry = line.split("#", 1)[0].split()
        if not entry:
            continue
        key = repo_key(entry[0]) if len(entry) == 2 else None
        if key is None:
            print(f"# names: ignoring malformed line: {line!r}", file=sys.stderr)
            continue
        name = entry[1]
        if name == DENY:
            name = qualified_name(*key)
        reserved[key] = name
    return reserved


def load_reserved(path):
    """Read names.txt. See load_reserved_entries for the format."""
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return {}
    return load_reserved_entries(lines)


def load_always_entries(lines):
    """Parse always.txt lines into a set of (owner, repo).

    One repository per line, spelled as repo_key accepts it; blank lines
    and # comments ignored, and a malformed line is dropped with a warning
    rather than taken as fatal, since the file is written by hand and a typo
    in it must not stop the nightly run.
    """
    always = set()
    for line in lines:
        entry = line.split("#", 1)[0].split()
        if not entry:
            continue
        key = repo_key(entry[0]) if len(entry) == 1 else None
        if key is None:
            print(f"# always: ignoring malformed line: {line!r}", file=sys.stderr)
            continue
        always.add(key)
    return always


def load_always(path):
    """Read always.txt. See load_always_entries for the format."""
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return set()
    return load_always_entries(lines)


def apply_reserved(known, reserved):
    """Give every hand-assigned name to the repository a line names.

    This is the only thing that applies names.txt to a row the run carries
    over. A known row keeps the name it has and never goes through
    choose_name again, so without this a line would take effect only on
    whichever day the row came round for a refresh.

    Two directions, and assignment wins over displacement so a repository
    named by one line cannot be moved off it by another: the repository a
    line names takes the name, and any other repository holding that name
    is displaced to its qualified form. Returns (assigned, displaced).
    """
    wanted = {name: key for key, name in reserved.items()}
    assigned = displaced = 0
    for key, row in known.items():
        lowered = (key[0].lower(), key[1].lower())

        # The repository this line is about, whatever it was called before.
        hand_assigned = reserved.get(lowered)
        if hand_assigned is not None:
            if row["name"] != hand_assigned:
                row["name"] = hand_assigned
                assigned += 1
            continue

        # Somebody else sitting on a name a line hands over.
        if wanted.get(row["name"], lowered) == lowered:
            continue
        row["name"] = qualified_name(row["owner"], row["repo"])
        displaced += 1
    return assigned, displaced


def count_claims(*rowsets):
    """How many repositories claim each derived name.

    Counted over every row the run will write: the database, the candidates
    it is about to resolve, and the externally resolved rows. A bare name is
    only handed out when one repository claims it, so a set left out of this
    count is a set whose rows take a contested name uncontested -- which is
    how a manual.txt entry named "files" landed on the same attribute as an
    indexed flake of that name.

    Rows are folded to distinct (owner, repo) pairs first, because the same
    repository reaches this from more than one set: a known row that is also
    being refreshed, or an externally resolved row that also has a known row.
    """
    pairs = set()
    for rows in rowsets:
        pairs |= {(row["owner"], row["repo"]) for row in rows}
    return collections.Counter(sanitize(repo) for _, repo in pairs)


def name_merged(merged, known, reserved, claims, taken, used):
    """Name the externally resolved rows, in place.

    tools/manual.py resolves what the GitHub API cannot reach -- a pinned
    ref, a subdirectory, another forge -- and it sees one flake at a time.
    It cannot know which names the database already hands out, so a row it
    named after its own repository took a contested bare name uncontested,
    and names.txt could not reach the row at all.

    So the name is decided here instead, by the same choose_name every
    other row goes through: names.txt first, then the name the repository
    already holds, then the contention rule. Called before any candidate is
    named, because a merged row is authoritative for its repository.

    `taken` and `used` are updated as names are handed out, exactly as the
    resolve loop updates them.
    """
    for key, row in merged.items():
        prior = known.get(key)
        name = choose_name(
            row["owner"], row["repo"], prior, reserved, claims, taken, used
        )
        # used counts new assignments only; a row with a prior already
        # holds its name and is not competing for it.
        if prior is None:
            used[sanitize(row["repo"])] += 1
        taken[name] = key
        row["name"] = name


def choose_name(owner, repo, prior, reserved, claims, taken, used):
    """The attribute name a repository gets.

    `claims` counts how many repositories share each sanitized repository
    name, `taken` maps an assigned name to the repository holding it, and
    `used` counts the names this run has handed out.
    """
    # A hand-assigned name is authoritative: over the derived name, and
    # over the name the repository already holds.
    hand_assigned = reserved.get((owner.lower(), repo.lower()))
    if hand_assigned:
        return hand_assigned

    # Names are sticky: a repo keeps the name it was first given.
    if prior:
        return prior["name"]

    base = sanitize(repo)
    qualified = qualified_name(owner, repo)

    # A bare name is worth having only when it says which repository it
    # means, so a contested one goes to nobody.
    if claims[base] > 1:
        return qualified

    # Never take a name another repository already holds, or one this run
    # has already handed out.
    if taken.get(base) not in (None, (owner, repo)) or used[base]:
        return qualified

    return base


def load_known(path):
    """Read an existing resolved.jsonl into (by_repo, taken_names)."""
    by_repo, taken = {}, {}
    if not path:
        return by_repo, taken
    try:
        fh = open(path)
    except FileNotFoundError:
        return by_repo, taken
    with fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            e = json.loads(line)
            by_repo[(e["owner"], e["repo"])] = e
            # Remember which repo owns each name so it stays put.
            taken[e["name"]] = (e["owner"], e["repo"])
    return by_repo, taken


def load_rejects(path):
    """Read a rejects.jsonl into {(owner, repo): checked_at}."""
    rejects = {}
    if not path:
        return rejects
    try:
        fh = open(path)
    except FileNotFoundError:
        return rejects
    with fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            e = json.loads(line)
            rejects[(e["owner"], e["repo"])] = e.get("checked_at", 0)
    return rejects


def write_rejects(path, rejects):
    """Write the ledger, sorted by repository, through a temporary file.

    In place, unlike resolved.jsonl, which update.sh moves over the old
    copy on success. The rename is what keeps a killed run from leaving a
    half-written ledger behind.
    """
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        for (owner, repo), checked_at in sorted(rejects.items()):
            row = {"owner": owner, "repo": repo, "checked_at": checked_at}
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    os.replace(tmp, path)


def prune_rejects(rejects, known, merged):
    """Drop reject rows for repositories that resolved.

    Success is authoritative. A stale row would otherwise skip a candidate
    the database already holds a row for, and the two would disagree about
    whether the repository is usable.
    """
    for key in list(rejects):
        if key in known or key in merged:
            del rejects[key]


def oldest_rejects(rejects, count):
    """The `count` reject rows checked longest ago, as a set of keys.

    Bounded work rather than a fixed staleness, which is the trade
    --refresh-oldest already makes for the known set: the cost of a run
    stays put and the cadence stretches as the set grows. A fixed interval
    would do the reverse, and the set only grows — every harvested
    repository that is not a flake joins it permanently.

    The repository breaks a tie, so a ledger seeded within one second still
    selects the same rows on every run.
    """
    if count <= 0:
        return set()
    order = sorted(rejects.items(), key=lambda kv: (kv[1], kv[0]))
    return {key for key, _ in order[:count]}


def select_refresh(known, always, count):
    """The known rows a bounded run re-resolves, always rows first.

    Two selections joined. A repository named in always.txt is re-resolved
    on every run, and the rest of the budget goes to the `count` rows
    resolved longest ago, which is the rolling window that brings the whole
    database round on a fixed cadence.

    An always row does not spend that budget. What the count buys is the
    cadence for the other ~16,000 rows -- 2,000 a run is about eight days --
    and a few dozen pinned repositories must not shorten it. It costs one
    extra GraphQL batch at most, and a repository whose HEAD did not move
    costs nothing beyond the lookup.
    """
    pinned = [
        row for key, row in known.items() if (key[0].lower(), key[1].lower()) in always
    ]
    pinned_keys = {(r["owner"], r["repo"]) for r in pinned}

    # The age window, minus whatever the always set already took, so a
    # pinned row that is also stale is not resolved twice in one run.
    by_age = sorted(
        (r for k, r in known.items() if k not in pinned_keys),
        key=lambda r: r.get("resolved_at", 0),
    )
    return pinned + by_age[:count]


def select_candidates(cands, known, merged, rejects, recheck):
    """The candidates this run will query.

    A repository in the known or merged set is settled and is not asked
    about again here; --refresh-oldest is what brings those round. One
    with a reject row was checked and could not be used, and it stays
    skipped until its row is in `recheck`.
    """
    out = []
    for cand in cands:
        key = (cand["owner"], cand["repo"])
        if key in known or key in merged:
            continue
        if key in rejects and key not in recheck:
            continue
        out.append(cand)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--known", help="existing resolved.jsonl to extend")
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="re-resolve every repo already present in --known",
    )
    ap.add_argument(
        "--refresh-oldest",
        type=int,
        default=0,
        metavar="N",
        help="re-resolve the N known repos resolved longest ago",
    )
    ap.add_argument(
        "--merge",
        metavar="FILE",
        help="externally resolved rows to fold in; they win over known rows",
    )
    ap.add_argument(
        "--rejects",
        metavar="FILE",
        help="ledger of repositories checked and found unusable; read and rewritten",
    )
    ap.add_argument(
        "--names",
        default="names.txt",
        metavar="FILE",
        help="hand-assigned attribute names; they outrank the derived name",
    )
    ap.add_argument(
        "--always",
        default="always.txt",
        metavar="FILE",
        help="repos to re-resolve every run, outside the --refresh-oldest window",
    )
    ap.add_argument(
        "--recheck-oldest",
        type=int,
        default=1200,
        metavar="N",
        help="re-check the N rejected repos checked longest ago",
    )
    args = ap.parse_args()

    known, taken = load_known(args.known)

    # Hand-assigned names outrank stickiness, so a name a names.txt line
    # claims is freed before any of this run's rows are named. taken is
    # rebuilt from the moved rows rather than patched.
    reserved = load_reserved(args.names)
    assigned, displaced = apply_reserved(known, reserved)
    if assigned or displaced:
        print(
            f"# names: assigned {assigned} row(s) a reserved name, "
            f"displaced {displaced}",
            file=sys.stderr,
        )
        taken = {e["name"]: (e["owner"], e["repo"]) for e in known.values()}

    # Externally resolved rows (tools/manual.py) are authoritative for their
    # repository: the known row, a refresh, and any harvested candidate for
    # the same repo all yield to them. They are named below, once the
    # contention count exists, rather than keeping the provisional name
    # manual.py gave them.
    merged, _ = load_known(args.merge)

    # Repositories checked before and found unusable. A row that names a
    # repository the database now holds is dropped on sight: success is
    # authoritative, and the two must not disagree.
    rejects = load_rejects(args.rejects)
    prune_rejects(rejects, known, merged)

    # Which of them this run looks at anyway. --refresh means look at
    # everything, rejects included.
    recheck = (
        set(rejects) if args.refresh else oldest_rejects(rejects, args.recheck_oldest)
    )

    cands = [json.loads(l) for l in sys.stdin if l.strip() and not l.startswith("#")]
    # Highest-starred first, so the better-known flake wins any *new* name clash.
    cands.sort(key=lambda c: -c.get("stars", 0))
    cands = select_candidates(cands, known, merged, rejects, recheck)

    # Repositories that must never be a week behind: nixpkgs and the other
    # foundations, whose revision every indexed flake is unified against,
    # and the flakes people follow closely enough to notice a stale pin.
    always = load_always(args.always)

    # Known rows to look at again: all of them, or the always set plus the
    # ones resolved longest ago. A rolling refresh keeps each run's work
    # bounded while every row comes round on a fixed cadence.
    if args.refresh:
        refresh = list(known.values())
    else:
        refresh = select_refresh(known, always, args.refresh_oldest)
    refresh = [r for r in refresh if (r["owner"], r["repo"]) not in merged]
    refresh_keys = {(r["owner"], r["repo"]) for r in refresh}

    # Every row the run will write, collected rather than streamed: the
    # file is sorted by name at the end, and a sort needs all of it. The
    # run writes to a temporary file that update.sh moves into place only
    # on success, so nothing is lost by not streaming.
    out = []

    # Carry over what is not being refreshed, so the output is always the
    # full database.
    for entry in known.values():
        key = (entry["owner"], entry["repo"])
        if key in refresh_keys or key in merged:
            continue
        out.append(entry)
    rechecked = sum(1 for c in cands if (c["owner"], c["repo"]) in recheck)
    # How many of the refresh came from always.txt rather than from the age
    # window, so a line that matches no known repository is visible in the
    # log as a count that did not go up.
    pinned = sum(
        1 for r in refresh if (r["owner"].lower(), r["repo"].lower()) in always
    )
    print(
        f"# carried over {len(known) - len(refresh)} known, "
        f"refreshing {len(refresh)} ({pinned} always), "
        f"resolving {len(cands)} new "
        f"({rechecked} re-checked of {len(rejects)} rejected)",
        file=sys.stderr,
        flush=True,
    )

    # Refreshed rows are candidates like any other, resolved after the new
    # ones so a new repo's name is decided first by stars as before.
    cands += [
        {"owner": r["owner"], "repo": r["repo"], "stars": r.get("stars", 0)}
        for r in refresh
    ]

    # How many repositories claim each derived name. A name several
    # repositories claim is never handed out bare, so this has to be
    # counted before any of them is named.
    claims = count_claims(known.values(), cands, merged.values())

    now = int(time.time())
    used = collections.Counter()
    emitted = 0

    # The externally resolved rows take their names first: they are
    # authoritative for their repository, so a candidate resolved below
    # cannot take a name out from under one.
    name_merged(merged, known, reserved, claims, taken, used)

    for i in range(0, len(cands), BATCH):
        batch = cands[i : i + BATCH]
        data = run_batch(batch)
        # An empty map is a query that failed, not forty rejections. GraphQL
        # answers for a repository that is gone with a null beside the other
        # results, so a batch that came back at all can be trusted to say
        # which of its repositories are unusable.
        answered = bool(data)
        for j, cand in enumerate(batch):
            prior = known.get((cand["owner"], cand["repo"]))
            node = data.get(f"r{j}")
            ref = ((node or {}).get("defaultBranchRef") or {}).get("target") or {}
            rev = ref.get("oid")

            # Must be a real flake with a resolvable commit. A known row that
            # fails now, whether the repo is gone or GitHub did not answer,
            # is kept as it was: dropping it would release its name.
            if not node or not node.get("flakeNix") or not rev:
                if prior:
                    out.append(prior)
                elif answered:
                    # No prior row, so nothing else would remember that this
                    # repository was looked at. Write it down, or the next run
                    # asks about it again.
                    rejects[(cand["owner"], cand["repo"])] = now
                continue

            # It resolved, so any reject row for it is now wrong.
            rejects.pop((cand["owner"], cand["repo"]), None)

            # The lock's root node names the flake's declared direct inputs,
            # and its node count is the size of the transitive graph.
            inputs = []
            lock_nodes = None
            lock = node.get("flakeLock") or {}
            text = lock.get("text")
            if text and (lock.get("byteSize") or 0) <= MAX_LOCK_BYTES:
                try:
                    parsed = json.loads(text)
                    nodes = parsed.get("nodes", {})
                    inputs = sorted((nodes.get("root", {}).get("inputs", {})).keys())
                    lock_nodes = max(len(nodes) - 1, 0)
                except Exception:
                    inputs = []

            name = choose_name(
                cand["owner"], cand["repo"], prior, reserved, claims, taken, used
            )
            # used counts the names this run has handed out, so only a new
            # assignment adds to it; a known row already holds its name.
            if prior is None:
                used[sanitize(cand["repo"])] += 1
            taken[name] = (cand["owner"], cand["repo"])

            row = {
                "name": name,
                "owner": cand["owner"],
                "repo": cand["repo"],
                "rev": rev,
                "inputs": inputs,
                "stars": node.get("stargazerCount", 0),
                "resolved_at": now,
            }
            if lock_nodes is not None:
                row["lock_nodes"] = lock_nodes
            # Fields other tools fill in survive a refresh.
            if prior and "description" in prior:
                row["description"] = prior["description"]
            out.append(row)
            emitted += 1
        print(f"# resolved {emitted}/{i + len(batch)}", file=sys.stderr, flush=True)

    # The externally resolved rows themselves.
    out.extend(merged.values())

    # Name order, with owner and repo breaking any tie, so the file has one
    # canonical form: two runs that resolve the same facts produce the same
    # bytes regardless of the order the rows were built in.
    out.sort(key=lambda r: (r["name"], r["owner"], r["repo"]))
    for row in out:
        print(json.dumps(row, sort_keys=True))

    if args.rejects:
        write_rejects(args.rejects, rejects)
        print(f"# {len(rejects)} rejected repositories on record", file=sys.stderr)


if __name__ == "__main__":
    main()
