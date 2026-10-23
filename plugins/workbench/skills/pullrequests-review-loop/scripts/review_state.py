#!/usr/bin/env python3
"""Report an automated PR reviewer's verdict on code/ submodule PRs.

Emits one JSON object per line to stdout - structured data for the calling
agent to judge, never a verdict of its own. The reviewer can be flaky (can
flip CHANGES_REQUESTED -> APPROVED on the same unchanged commit, or never
review at all), so this script surfaces the full review history rather than
collapsing it to a single boolean.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_lib"))

import common  # noqa: E402
import scm  # noqa: E402

REVIEWER_ENV = "WORKBENCH_REVIEWER"
NO_REVIEWER_MSG = "review_state: no reviewer; pass --reviewer <login> or set WORKBENCH_REVIEWER"

PR_PAIR_RE = re.compile(r"^(?P<repo>[^#]+)#(?P<num>\d+)$")


class Skip(Exception):
    """A repo the review loop does not poll: GitLab (fails the run) or local-only."""

    def __init__(self, message: str, fails: bool):
        super().__init__(message)
        self.fails = fails


def github_adapter(repo: str) -> scm.GitHub:
    """The repo's GitHub adapter; Skip for a GitLab or local-only repo."""
    adapter = scm.adapter_for(repo)
    if adapter is None:
        raise Skip(
            f"pullrequests-review-loop: {repo}: skipped "
            f"(local-only: no PR host for {scm.origin_url(repo) or '(no origin)'})",
            fails=False,
        )
    if not isinstance(adapter, scm.GitHub):
        raise Skip(
            f"pullrequests-review-loop: {repo}: unsupported on GitLab (review-loop is GitHub-only)",
            fails=True,
        )
    return adapter


def parse_pr_pairs(value) -> list:
    pairs = []
    for item in common.parse_repos(value):
        m = PR_PAIR_RE.match(item)
        if not m:
            raise common.RepoError(f"invalid --prs entry: {item}")
        pairs.append((m.group("repo"), int(m.group("num"))))
    return pairs


def _substantive(body) -> bool:
    return bool(body and body.strip())


def reviews_by_reviewer(reviews: list, reviewer: str, head_sha: str) -> list:
    """Reviews authored by `reviewer`, newest first, tagged onHead."""
    mine = [r for r in reviews if (r.get("user") or {}).get("login") == reviewer]
    mine.sort(key=lambda r: r.get("submitted_at") or "", reverse=True)
    out = []
    for r in mine:
        commit_id = r.get("commit_id") or ""
        out.append(
            {
                "state": r.get("state", "UNKNOWN"),
                "submittedAt": r.get("submitted_at") or "",
                "commitId": commit_id,
                "onHead": commit_id == head_sha,
                "body": r.get("body", ""),
            }
        )
    return out


def comments_by_reviewer(comments: list, reviewer: str, head_sha: str) -> list:
    """Inline review comments authored by `reviewer`, newest first, tagged onHead."""
    mine = [c for c in comments if (c.get("user") or {}).get("login") == reviewer]
    mine.sort(key=lambda c: c.get("created_at") or "", reverse=True)
    out = []
    for c in mine:
        commit_id = c.get("commit_id") or ""
        out.append(
            {
                "path": c.get("path", ""),
                "line": c.get("line"),
                "commitId": commit_id,
                "onHead": commit_id == head_sha,
                "createdAt": c.get("created_at", ""),
                "body": c.get("body", ""),
            }
        )
    return out


def decision_on_head(reviews_all: list) -> str:
    """Latest (newest-first) review by the reviewer whose commit is the head."""
    for r in reviews_all:
        if r["onHead"]:
            return r["state"]
    return "NONE"


def is_actionable(decision: str, reviews_all: list, inline_comments: list) -> bool:
    if decision == "CHANGES_REQUESTED":
        return True
    if any(r["onHead"] and _substantive(r["body"]) for r in reviews_all):
        return True
    if any(c["onHead"] and _substantive(c["body"]) for c in inline_comments):
        return True
    return False


def fetch_pr_state(adapter: scm.GitHub, num: int, reviewer: str) -> dict:
    # REST only: `gh pr view` is GraphQL, which Claude Code cloud sessions block.
    pull = adapter.pull_view(adapter.json(["api", f"repos/{adapter.slug}/pulls/{num}"]))
    head_sha = pull["headRefOid"]
    reviews = adapter.pages(f"repos/{adapter.slug}/pulls/{num}/reviews?per_page=100")
    comments = adapter.pages(f"repos/{adapter.slug}/pulls/{num}/comments?per_page=100")

    reviews_all = reviews_by_reviewer(reviews, reviewer, head_sha)
    inline_comments = comments_by_reviewer(comments, reviewer, head_sha)
    decision = decision_on_head(reviews_all)

    return {
        "headSha": head_sha,
        "prState": pull["state"],
        "mergeable": pull["mergeable"],
        "url": pull["url"],
        "reviewerDecisionOnHead": decision,
        "reviewsAll": reviews_all,
        "inlineComments": inline_comments,
        "hasStaleReviews": any(not r["onHead"] for r in reviews_all),
        "actionable": is_actionable(decision, reviews_all, inline_comments),
    }


def process_pr(repo: str, num: int, reviewer: str) -> dict:
    entry = {"repo": repo, "pr": num}
    try:
        adapter = github_adapter(repo)
        entry.update(fetch_pr_state(adapter, num, reviewer))
    except common.RepoError as exc:
        entry["error"] = str(exc)
    return entry


def process_branch(repo: str, branch: str, reviewer: str) -> dict:
    entry = {"repo": repo, "pr": None}
    try:
        adapter = github_adapter(repo)
        entry["pr"] = adapter.resolve_branch(branch)
        entry.update(fetch_pr_state(adapter, entry["pr"], reviewer))
    except common.RepoError as exc:
        entry["error"] = str(exc)
    return entry


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prs", help="comma-separated repo#pr pairs, e.g. repo1#123,repo2#45")
    parser.add_argument("--branch", help="branch name to resolve a PR for, per repo")
    parser.add_argument("--repos", help="comma-separated submodule names")
    parser.add_argument(
        "--reviewer",
        help=f"automated reviewer login to filter on (default: env {REVIEWER_ENV})",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    reviewer = args.reviewer or os.environ.get(REVIEWER_ENV)
    if not reviewer:
        print(NO_REVIEWER_MSG, file=sys.stderr)
        sys.exit(2)

    if args.prs and (args.branch or args.repos):
        parser.error("use --prs, or --branch/--repos, not both")
    if not args.prs and not (args.branch and args.repos):
        parser.error("provide --prs OR --branch and --repos")

    if args.prs:
        try:
            targets = [(repo, num) for repo, num in parse_pr_pairs(args.prs)]
        except common.RepoError as exc:
            parser.error(str(exc))
            return
        work = [(process_pr, repo, num) for repo, num in targets]
    else:
        work = [(process_branch, repo, args.branch) for repo in common.parse_repos(args.repos)]

    results = []
    unsupported = False
    for fn, repo, target in work:
        try:
            results.append(fn(repo, target, reviewer))
        except Skip as skip:
            print(skip, file=sys.stderr)
            unsupported = unsupported or skip.fails

    for entry in results:
        print(json.dumps(entry))

    all_errored = bool(results) and all("error" in r for r in results)
    sys.exit(1 if unsupported or all_errored else 0)


if __name__ == "__main__":
    main()
