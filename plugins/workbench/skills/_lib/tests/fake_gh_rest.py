"""A fake GitHub REST backend for the `gh` shims in the pullrequests-* tests.

A shim script imports this module and calls `respond(argv, world)` for every
`gh api ...` call; it answers the REST routes scm.GitHub uses. The PRs in the
world are written in the flat shape the tests always used (state, mergeable,
mergeStateStatus, url, headRefOid, statusCheckRollup) and are converted to REST
objects here, so a test states "this PR has a failing check" once and the
adapter's REST-to-vocabulary mapping is what gets exercised.

world = {
    "slug": "acme/repo1",
    "default_branch": "main",
    "prs": {"201": {...flat PR...}},
    "branches": {"feature-x": "201"},      # head branch -> PR number
    "files": {"203": ["a.py", "b.py"]},    # PR number -> changed file names
    "labels": {"201": ["bug"]},            # PR number -> label names
    "reviews": {"201": [...]},             # PR number -> REST review objects
    "comments": {"201": [...]},            # PR number -> REST inline review comments
}

Unmatched requests return None so the shim can add its own routes.
"""

import json
import os
import re
from urllib.parse import parse_qs, unquote, urlsplit

LIB_DIR = os.path.dirname(os.path.abspath(__file__))


def shim(text):
    """A shim script whose `__FAKE_GH_DIR__` placeholder is this module's directory,
    so the script can `sys.path.insert(0, "__FAKE_GH_DIR__")` and import it."""
    return text.replace("__FAKE_GH_DIR__", LIB_DIR)


NOT_FOUND = (1, "gh: Not Found (HTTP 404)\n")


def parse(argv):
    """(method, path, query, fields, flags) of one `gh api` argv."""
    args = list(argv[1:])
    method, fields, flags, path = None, {}, set(), None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-X", "--method"):
            method = args[i + 1]
            i += 2
        elif a in ("-f", "-F", "--raw-field", "--field"):
            key, _, value = args[i + 1].partition("=")
            if key.endswith("[]"):
                fields.setdefault(key[:-2], []).append(value)
            else:
                fields[key] = value
            i += 2
        elif a == "--hostname":
            i += 2
        elif a.startswith("-"):
            flags.add(a)
            i += 1
        else:
            path = a
            i += 1
    parts = urlsplit(path or "")
    query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
    return method or ("POST" if fields else "GET"), unquote(parts.path), query, fields, flags


def _rest_pull(slug, num, pr):
    state = pr.get("state", "OPEN")
    mergeable = pr.get("mergeable")
    merge_commit = pr.get("mergeCommit")
    if isinstance(merge_commit, dict):
        merge_commit = merge_commit.get("oid")
    return {
        "number": int(num),
        "state": "open" if state == "OPEN" else "closed",
        "merged": state == "MERGED",
        "merged_at": "2026-10-01T00:00:00Z" if state == "MERGED" else None,
        "merge_commit_sha": merge_commit,
        "mergeable": {"MERGEABLE": True, "CONFLICTING": False}.get(mergeable),
        "mergeable_state": (pr.get("mergeStateStatus") or "unknown").lower(),
        "html_url": pr.get("url", f"https://github.com/{slug}/pull/{num}"),
        "title": pr.get("title", f"pr {num}"),
        "body": pr.get("body", ""),
        "created_at": pr.get("createdAt", f"2026-10-{int(num) % 28 + 1:02d}T00:00:00Z"),
        "head": {"sha": pr.get("headRefOid", f"sha{num}"), "ref": pr.get("headRef", "")},
        "base": {"ref": pr.get("baseRefName", "main")},
    }


def _is_status(entry):
    return "state" in entry and "status" not in entry


def _check_run(entry):
    return {
        "name": entry.get("name"),
        "status": (entry.get("status") or "completed").lower(),
        "conclusion": (entry.get("conclusion") or None) and entry["conclusion"].lower(),
        "details_url": entry.get("detailsUrl"),
        "html_url": entry.get("detailsUrl"),
    }


def _status(entry):
    return {
        "context": entry.get("context") or entry.get("name"),
        "state": entry["state"].lower(),
        "target_url": entry.get("detailsUrl"),
    }


def _by_sha(world, sha):
    for num, pr in world["prs"].items():
        if pr.get("headRefOid", f"sha{num}") == sha:
            return pr
    return None


def respond(argv, world):
    """(exit code, stdout-or-stderr text) for a GET route, None when not served."""
    method, path, query, fields, flags = parse(argv)
    if method != "GET":
        return None
    slug = world.get("slug", "acme/repo1")
    prefix = f"repos/{slug}"
    if path == prefix:
        return 0, json.dumps({"default_branch": world.get("default_branch", "main")})
    if path == f"{prefix}/pulls":
        head = query.get("head", "").partition(":")[2]
        wanted = world.get("branches", {}).get(head)
        nums = [wanted] if wanted else ([] if head else list(world["prs"]))
        pulls = [_rest_pull(slug, n, world["prs"][n]) for n in nums if n in world["prs"]]
        if query.get("state") == "open":
            pulls = [p for p in pulls if p["state"] == "open"]
        pulls.sort(key=lambda p: p["created_at"], reverse=True)
        return 0, json.dumps(pulls)
    m = re.fullmatch(rf"{re.escape(prefix)}/pulls/(\d+)", path)
    if m:
        pr = world["prs"].get(m.group(1))
        return (0, json.dumps(_rest_pull(slug, m.group(1), pr))) if pr else NOT_FOUND
    m = re.fullmatch(rf"{re.escape(prefix)}/pulls/(\d+)/files", path)
    if m:
        if m.group(1) not in world["prs"]:
            return NOT_FOUND
        return 0, json.dumps([{"filename": f} for f in world.get("files", {}).get(m.group(1), [])])
    m = re.fullmatch(rf"{re.escape(prefix)}/pulls/(\d+)/(reviews|comments)", path)
    if m:
        if m.group(1) not in world["prs"]:
            return NOT_FOUND
        return 0, json.dumps(world.get(m.group(2), {}).get(m.group(1), []))
    m = re.fullmatch(rf"{re.escape(prefix)}/commits/([^/]+)/check-runs", path)
    if m:
        pr = _by_sha(world, m.group(1))
        rollup = [e for e in (pr or {}).get("statusCheckRollup") or [] if not _is_status(e)]
        return 0, json.dumps(
            {"total_count": len(rollup), "check_runs": [_check_run(e) for e in rollup]}
        )
    m = re.fullmatch(rf"{re.escape(prefix)}/commits/([^/]+)/status", path)
    if m:
        pr = _by_sha(world, m.group(1))
        rollup = [e for e in (pr or {}).get("statusCheckRollup") or [] if _is_status(e)]
        return 0, json.dumps({"state": "pending", "statuses": [_status(e) for e in rollup]})
    m = re.fullmatch(rf"{re.escape(prefix)}/issues/(\d+)/labels", path)
    if m:
        names = world.get("labels", {}).get(m.group(1), [])
        return 0, json.dumps([{"name": n} for n in names])
    return None


def serve(argv, world):
    """Run respond() and print its answer the way gh would; returns the exit code,
    or None when the route is not served here. `--slurp` wraps the answer in a page list."""
    answer = respond(argv, world)
    if answer is None:
        return None
    code, text = answer
    if code:
        import sys

        sys.stderr.write(text)
        return code
    if "--slurp" in parse(argv)[4]:
        text = json.dumps([json.loads(text)])
    print(text)
    return 0
