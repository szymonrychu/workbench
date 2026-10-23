"""The scm poll ops: resolve_branch, poll, failed_reasons, conflict_files, list_open.

Each op is checked against a fake gh/glab on PATH, with the value vocabularies
the pullrequests-watch and pullrequests-list scripts report on both hosts.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common  # noqa: E402
import scm  # noqa: E402

# One shim serves as both gh and glab. It records argv, then answers from the
# first rule in $SHIM_RULES whose "match" tokens all appear in argv (default:
# exit 0, no output).
SHIM = """#!/usr/bin/env python3
import json
import os
import sys

name = os.path.basename(sys.argv[0])
argv = sys.argv[1:]
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write(json.dumps({"cli": name, "argv": argv}) + "\\n")
rules = json.load(open(os.environ["SHIM_RULES"])) if os.environ.get("SHIM_RULES") else []
for rule in rules:
    if rule.get("cli", name) == name and all(t in argv for t in rule.get("match", [])):
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(rule.get("exit", 0))
"""

LOG_150 = "".join(f"line {i}\n" for i in range(1, 151))
TAIL_100 = "\n".join(f"line {i}" for i in range(51, 151))


@pytest.fixture
def shim(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("gh", "glab"):
        path = bin_dir / name
        path.write_text(SHIM)
        path.chmod(0o755)
    log = tmp_path / "shim.jsonl"
    rules = tmp_path / "rules.json"
    monkeypatch.setenv("SHIM_LOG", str(log))
    monkeypatch.setenv("SHIM_RULES", str(rules))
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    class Shim:
        def respond(self, *new_rules):
            rules.write_text(json.dumps(list(new_rules)))

        def calls(self):
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text().splitlines()]

    s = Shim()
    s.respond()
    return s


# Built after the shim is on PATH; the adapters read the process env per call.
@pytest.fixture
def hosts(shim):
    return (
        scm.GitHub("app", "github.com", "acme/app"),
        scm.GitLab("proj", "gitlab.com", "grp/sub/proj"),
    )


GL_MR = "projects/grp%2Fsub%2Fproj/merge_requests"


def _ok(conclusion="SUCCESS", **extra):
    return {"name": "ok", "status": "COMPLETED", "conclusion": conclusion, **extra}


GH = "repos/acme/app"


def _rest_check(conclusion="success", name="ok", status="completed", url=None):
    return {"name": name, "status": status, "conclusion": conclusion, "details_url": url}


def _rest_pull(num=12, sha="abc123", **extra):
    return {
        "number": num,
        "state": "open",
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "html_url": f"https://github.com/acme/app/pull/{num}",
        "head": {"sha": sha},
        "base": {"ref": "main"},
        **extra,
    }


def gh_pull_rules(pull=None, check_runs=(), statuses=(), num=12):
    """gh rules answering the three REST reads one GitHub snapshot of PR `num` makes."""
    pull = pull or _rest_pull(num)
    sha = pull["head"]["sha"]
    return [
        {"cli": "gh", "match": [f"{GH}/pulls/{num}"], "stdout": json.dumps(pull)},
        {
            "cli": "gh",
            "match": [f"{GH}/commits/{sha}/check-runs?per_page=100"],
            "stdout": json.dumps(
                [{"total_count": len(check_runs), "check_runs": list(check_runs)}]
            ),
        },
        {
            "cli": "gh",
            "match": [f"{GH}/commits/{sha}/status?per_page=100"],
            "stdout": json.dumps({"statuses": list(statuses)}),
        },
    ]


# --- resolve_branch ---------------------------------------------------------


def test_resolve_branch_both_hosts(shim, hosts):
    github, gitlab = hosts
    shim.respond(
        {
            "cli": "gh",
            "match": [
                f"{GH}/pulls?head=acme:feat/x&state=all&sort=created&direction=desc&per_page=1"
            ],
            "stdout": json.dumps([{"number": 12}]),
        },
        {"cli": "glab", "match": [], "stdout": json.dumps([{"iid": 7, "web_url": "u"}])},
    )
    assert github.resolve_branch("feat/x") == 12
    assert gitlab.resolve_branch("feat/x") == 7
    gh_call, gl_call = shim.calls()
    assert gh_call["argv"] == [
        "api",
        f"{GH}/pulls?head=acme:feat/x&state=all&sort=created&direction=desc&per_page=1",
    ]
    assert gl_call["argv"] == ["api", f"{GL_MR}?source_branch=feat%2Fx&state=opened"]


def test_resolve_branch_none_raises_naming_branch(shim, hosts):
    github, gitlab = hosts
    shim.respond(
        {"cli": "gh", "stdout": "[]"},
        {"cli": "glab", "stdout": "[]"},
    )
    with pytest.raises(common.RepoError, match=r"^branch feat/x: no PR found \("):
        github.resolve_branch("feat/x")
    with pytest.raises(common.RepoError, match=r"^branch feat/x: no MR found \("):
        gitlab.resolve_branch("feat/x")


def test_resolve_branch_cli_failure_raises_naming_branch(shim, hosts):
    github, _ = hosts
    shim.respond({"cli": "gh", "stderr": "gh: Not Found (HTTP 404)", "exit": 1})
    with pytest.raises(common.RepoError, match=r"^branch feat/x: no PR found \(.*404"):
        github.resolve_branch("feat/x")


def test_resolve_branch_malformed_answer_raises(shim, hosts):
    github, gitlab = hosts
    shim.respond({"cli": "gh", "stdout": "{}"}, {"cli": "glab", "stdout": "{}"})
    with pytest.raises(common.RepoError, match="branch b: no PR found"):
        github.resolve_branch("b")
    with pytest.raises(common.RepoError, match="branch b: no MR found"):
        gitlab.resolve_branch("b")


# --- poll: GitHub -----------------------------------------------------------


@pytest.mark.parametrize(
    "rollup,checks",
    [
        (None, "none"),
        ([], "none"),
        ([_ok()], "passed"),
        ([_ok("SKIPPED"), _ok("NEUTRAL")], "passed"),
        ([_ok(), _ok("FAILURE")], "failed"),
        ([_ok("CANCELLED")], "failed"),
        ([_ok("TIMED_OUT")], "failed"),
        ([_ok("ACTION_REQUIRED")], "failed"),
        ([_ok("STARTUP_FAILURE")], "failed"),
        ([{"name": "a", "status": "IN_PROGRESS", "conclusion": ""}], "pending"),
        ([{"name": "a", "status": "QUEUED"}], "pending"),
        ([{"context": "ci", "state": "SUCCESS"}], "passed"),
        ([{"context": "ci", "state": "PENDING"}], "pending"),
        ([{"context": "ci", "state": "FAILURE"}], "failed"),
        ([{"context": "ci", "state": "ERROR"}], "failed"),
        ([{"name": "a", "status": "IN_PROGRESS", "conclusion": "SUCCESS"}], "pending"),
        ([{"name": "a"}, _ok("FAILURE")], "failed"),
    ],
)
def test_reduce_checks_vocabulary(rollup, checks):
    assert scm.GitHub.reduce_checks(rollup) == checks


@pytest.mark.parametrize(
    "check_runs,statuses,checks",
    [
        ([], [], "none"),
        ([_rest_check()], [], "passed"),
        ([_rest_check("skipped"), _rest_check("neutral")], [], "passed"),
        ([_rest_check(), _rest_check("failure")], [], "failed"),
        ([_rest_check("timed_out")], [], "failed"),
        ([_rest_check("cancelled")], [], "failed"),
        ([_rest_check(None, status="in_progress")], [], "pending"),
        ([_rest_check(None, status="queued")], [], "pending"),
        ([], [{"context": "ci", "state": "success"}], "passed"),
        ([], [{"context": "ci", "state": "pending"}], "pending"),
        ([], [{"context": "ci", "state": "failure"}], "failed"),
        ([], [{"context": "ci", "state": "error"}], "failed"),
        ([_rest_check()], [{"context": "ci", "state": "pending"}], "pending"),
        ([_rest_check()], [{"context": "ci", "state": "error"}], "failed"),
    ],
)
def test_poll_github_reduces_rest_checks(shim, hosts, check_runs, statuses, checks):
    github, _ = hosts
    shim.respond(*gh_pull_rules(check_runs=check_runs, statuses=statuses))
    assert github.poll(12)["checks"] == checks


@pytest.mark.parametrize(
    "mergeable,state,expected,detail",
    [
        (True, "clean", "mergeable", "CLEAN"),
        (False, "clean", "conflict", "CLEAN"),
        (True, "dirty", "conflict", "DIRTY"),
        (False, "dirty", "conflict", "DIRTY"),
        (None, "unknown", "unknown", "UNKNOWN"),
        (None, None, "unknown", "UNKNOWN"),
        (True, "blocked", "blocked", "BLOCKED"),
        (True, "behind", "blocked", "BEHIND"),
        (True, "unstable", "blocked", "UNSTABLE"),
    ],
)
def test_poll_github_mergeability(shim, hosts, mergeable, state, expected, detail):
    github, _ = hosts
    pull = _rest_pull(mergeable=mergeable, mergeable_state=state)
    shim.respond(*gh_pull_rules(pull))
    poll = github.poll(12)
    assert poll["mergeable"] == expected
    assert poll["merge_detail"] == f"mergeStateStatus={detail}"


def test_poll_github_normalizes(shim, hosts):
    github, _ = hosts
    runs = [
        _rest_check(name="lint"),
        _rest_check(
            "failure", name="build", url="https://github.com/acme/app/actions/runs/5/job/1"
        ),
    ]
    statuses = [{"context": "ext/ci", "state": "success", "target_url": "https://ci.example/1"}]
    pull = _rest_pull(mergeable_state="blocked")
    threads = {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{"isResolved": True}],
    }}}}}  # fmt: skip
    shim.respond(
        *gh_pull_rules(pull, runs, statuses),
        {"cli": "gh", "match": ["api", "graphql"], "stdout": json.dumps(threads)},
    )
    rollup = [
        {
            "name": "lint",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "detailsUrl": None,
        },
        {
            "name": "build",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "detailsUrl": "https://github.com/acme/app/actions/runs/5/job/1",
        },
        {"name": "ext/ci", "state": "SUCCESS", "detailsUrl": "https://ci.example/1"},
    ]
    poll = github.poll(12)
    assert poll == {
        "state": "OPEN",
        "checks": "failed",
        "mergeable": "blocked",
        "merge_detail": "mergeStateStatus=BLOCKED",
        "url": "https://github.com/acme/app/pull/12",
        "head_sha": "abc123",
        "raw": poll["raw"],
        "reviews": 0,
    }
    assert poll["raw"]["statusCheckRollup"] == rollup
    assert [c["argv"] for c in shim.calls()[:3]] == [
        ["api", f"{GH}/pulls/12"],
        ["api", "--paginate", "--slurp", f"{GH}/commits/abc123/check-runs?per_page=100"],
        ["api", f"{GH}/commits/abc123/status?per_page=100"],
    ]


def test_poll_github_check_run_without_details_url_falls_back_to_html_url(shim, hosts):
    github, _ = hosts
    run = {"name": "a", "status": "completed", "conclusion": "failure", "html_url": "https://h/1"}
    shim.respond(*gh_pull_rules(check_runs=[run]))
    entry = github.poll(12)["raw"]["statusCheckRollup"][0]
    assert entry["detailsUrl"] == "https://h/1"


def test_poll_github_check_runs_paginate(shim, hosts):
    github, _ = hosts
    rules = gh_pull_rules()
    rules[1]["stdout"] = json.dumps(
        [
            {"check_runs": [_rest_check(name="a")]},
            {"check_runs": [_rest_check("failure", name="b")]},
        ]
    )
    shim.respond(*rules)
    poll = github.poll(12)
    assert [e["name"] for e in poll["raw"]["statusCheckRollup"]] == ["a", "b"]
    assert poll["checks"] == "failed"


@pytest.mark.parametrize(
    "pull,state",
    [
        ({"state": "open"}, "OPEN"),
        ({"state": "closed", "merged": True, "merged_at": "2026-10-01T00:00:00Z"}, "MERGED"),
        ({"state": "closed", "merged_at": "2026-10-01T00:00:00Z"}, "MERGED"),
        ({"state": "closed", "merged": False, "merged_at": None}, "CLOSED"),
    ],
)
def test_poll_github_state_from_rest(shim, hosts, pull, state):
    github, _ = hosts
    shim.respond({"cli": "gh", "stdout": json.dumps(pull)})
    poll = github.poll(1)
    assert poll["state"] == state
    assert (poll["url"], poll["head_sha"], poll["checks"]) == ("", "", "none")


def test_poll_github_missing_state_is_unknown(shim, hosts):
    github, _ = hosts
    shim.respond({"cli": "gh", "stdout": "{}"})
    assert github.poll(1)["state"] == "UNKNOWN"


def test_poll_github_cli_failure_raises(shim, hosts):
    github, _ = hosts
    shim.respond({"cli": "gh", "stderr": "not found", "exit": 1})
    with pytest.raises(common.RepoError, match="not found"):
        github.poll(1)


# --- poll: GitLab -----------------------------------------------------------


@pytest.mark.parametrize(
    "pipeline,checks",
    [
        (None, "none"),
        ({}, "none"),
        ({"id": 1, "status": "success"}, "passed"),
        ({"id": 1, "status": "skipped"}, "passed"),
        ({"id": 1, "status": "failed"}, "failed"),
        ({"id": 1, "status": "canceled"}, "failed"),
        ({"id": 1, "status": "running"}, "pending"),
        ({"id": 1, "status": "manual"}, "pending"),
        ({"id": 1, "status": "created"}, "pending"),
        ({"id": 1, "status": "something-new"}, "pending"),
    ],
)
def test_poll_gitlab_reduces_pipeline(shim, hosts, pipeline, checks):
    _, gitlab = hosts
    shim.respond(
        {"cli": "glab", "stdout": json.dumps({"state": "opened", "head_pipeline": pipeline})}
    )
    assert gitlab.poll(7)["checks"] == checks


@pytest.mark.parametrize(
    "detailed,conflicts,expected",
    [
        ("mergeable", False, "mergeable"),
        ("conflict", False, "conflict"),
        ("mergeable", True, "conflict"),
        ("checking", False, "unknown"),
        ("unchecked", False, "unknown"),
        ("not_approved", False, "blocked"),
        ("ci_must_pass", False, "blocked"),
        (None, None, "blocked"),
    ],
)
def test_poll_gitlab_mergeability(shim, hosts, detailed, conflicts, expected):
    _, gitlab = hosts
    data = {"state": "opened"}
    if detailed is not None:
        data["detailed_merge_status"] = detailed
    if conflicts is not None:
        data["has_conflicts"] = conflicts
    shim.respond({"cli": "glab", "stdout": json.dumps(data)})
    poll = gitlab.poll(7)
    assert poll["mergeable"] == expected
    assert poll["merge_detail"] == f"detailed_merge_status={detailed or 'unknown'}"


@pytest.mark.parametrize(
    "raw,state",
    [
        ("opened", "OPEN"),
        ("locked", "OPEN"),
        ("merged", "MERGED"),
        ("closed", "CLOSED"),
        ("x", "UNKNOWN"),
    ],
)
def test_poll_gitlab_state_map(shim, hosts, raw, state):
    _, gitlab = hosts
    shim.respond({"cli": "glab", "stdout": json.dumps({"state": raw})})
    assert gitlab.poll(7)["state"] == state


def test_poll_gitlab_normalizes(shim, hosts):
    _, gitlab = hosts
    data = {
        "iid": 7,
        "state": "opened",
        "detailed_merge_status": "not_approved",
        "has_conflicts": False,
        "web_url": "https://gitlab.com/grp/sub/proj/-/merge_requests/7",
        "sha": "def456",
        "head_pipeline": {"id": 77, "status": "running"},
    }
    shim.respond({"cli": "glab", "stdout": json.dumps(data)})
    assert gitlab.poll(7) == {
        "state": "OPEN",
        "checks": "pending",
        "mergeable": "blocked",
        "merge_detail": "detailed_merge_status=not_approved",
        "url": "https://gitlab.com/grp/sub/proj/-/merge_requests/7",
        "head_sha": "def456",
        "raw": data,
        "discussions": [],
        "reviews": 0,
    }
    assert shim.calls()[0]["argv"] == ["api", f"{GL_MR}/7"]


# --- failed_reasons ---------------------------------------------------------


def _rest_rollup(rollup):
    """(check_runs, statuses) REST lists for a flat check list: entries with a
    `state` are commit statuses, the rest check runs."""
    runs, statuses = [], []
    for e in rollup:
        if "state" in e:
            statuses.append(
                {
                    "context": e.get("context"),
                    "state": e["state"].lower(),
                    "target_url": e.get("detailsUrl"),
                }
            )
        else:
            runs.append(
                {
                    "name": e.get("name"),
                    "status": e["status"].lower(),
                    "conclusion": e["conclusion"].lower(),
                    "details_url": e.get("detailsUrl"),
                }
            )
    return runs, statuses


def _gh_failed_poll(github, shim, rollup):
    runs, statuses = _rest_rollup(rollup)
    shim.respond(
        {"cli": "gh", "match": ["run", "view", "555"], "stdout": LOG_150},
        {"cli": "gh", "match": ["run", "view", "666"], "stderr": "log gone", "exit": 1},
        *gh_pull_rules(check_runs=runs, statuses=statuses),
    )
    return github.poll(12)


def _gl_failed_poll(gitlab, shim, jobs, pipeline=None):
    pipeline = pipeline or {"id": 77, "status": "failed"}
    shim.respond(
        {"cli": "glab", "match": ["projects/grp%2Fsub%2Fproj/jobs/9/trace"], "stdout": LOG_150},
        {
            "cli": "glab",
            "match": ["projects/grp%2Fsub%2Fproj/jobs/10/trace"],
            "stderr": "403 Forbidden",
            "exit": 1,
        },
        {
            "cli": "glab",
            "match": ["projects/grp%2Fsub%2Fproj/pipelines/77/jobs"],
            "stdout": json.dumps(jobs),
        },
        {
            "cli": "glab",
            "match": [f"{GL_MR}/7"],
            "stdout": json.dumps({"state": "opened", "head_pipeline": pipeline}),
        },
    )
    return gitlab.poll(7)


def test_failed_reasons_log_tails_both_hosts(shim, hosts):
    github, gitlab = hosts
    rollup = [
        _ok(name="lint"),
        _ok(
            "FAILURE",
            name="build",
            detailsUrl="https://github.com/acme/app/actions/runs/555/job/1",
        ),
    ]
    poll = _gh_failed_poll(github, shim, rollup)
    assert github.failed_reasons(poll) == [f"check 'build' failed:\n{TAIL_100}"]
    assert shim.calls()[-1]["argv"] == [
        "run",
        "view",
        "555",
        "--log-failed",
        "-R",
        "acme/app",
    ]

    jobs = [
        {"id": 8, "name": "lint", "status": "success"},
        {"id": 9, "name": "test", "status": "failed"},
    ]
    poll = _gl_failed_poll(gitlab, shim, jobs)
    assert gitlab.failed_reasons(poll) == [f"job 'test' failed:\n{TAIL_100}"]
    assert [c["argv"] for c in shim.calls()[-2:]] == [
        ["api", "projects/grp%2Fsub%2Fproj/pipelines/77/jobs"],
        ["api", "projects/grp%2Fsub%2Fproj/jobs/9/trace"],
    ]


def test_failed_reasons_short_log_kept_whole(shim, hosts):
    github, _ = hosts
    runs, _ = _rest_rollup([_ok("FAILURE", name="b", detailsUrl="/actions/runs/1/job/2")])
    shim.respond(
        {"cli": "gh", "match": ["run", "view"], "stdout": "a\nb\n"},
        *gh_pull_rules(check_runs=runs),
    )
    assert github.failed_reasons(github.poll(12)) == ["check 'b' failed:\na\nb"]


def test_failed_reasons_github_degrades_per_check(shim, hosts):
    github, _ = hosts
    rollup = [
        _ok("FAILURE", name="no-url"),
        {"context": "ext/ci", "state": "ERROR", "detailsUrl": "https://ci.example/1"},
        _ok("CANCELLED", name="gone", detailsUrl="https://github.com/a/b/actions/runs/666"),
        {"status": "COMPLETED", "conclusion": "TIMED_OUT"},
    ]
    poll = _gh_failed_poll(github, shim, rollup)
    assert github.failed_reasons(poll) == [
        "check 'no-url' failed:\nno run id found (detailsUrl=none)",
        "check 'gone' failed:\ncould not fetch log for run 666: log gone",
        "check 'unknown check' failed:\nno run id found (detailsUrl=none)",
        # commit statuses follow the check runs in the synthesised rollup
        "check 'ext/ci' failed:\nno run id found (detailsUrl=https://ci.example/1)",
    ]


def test_failed_reasons_gitlab_degrades_per_job(shim, hosts):
    _, gitlab = hosts
    jobs = [{"id": 10, "status": "failed"}, {"id": 9, "name": "test", "status": "failed"}]
    poll = _gl_failed_poll(gitlab, shim, jobs)
    assert gitlab.failed_reasons(poll) == [
        "job 'unknown job' failed:\ncould not fetch log for job 10: 403 Forbidden",
        f"job 'test' failed:\n{TAIL_100}",
    ]


def test_failed_reasons_gitlab_jobs_fetch_error_raises(shim, hosts):
    _, gitlab = hosts
    poll = _gl_failed_poll(gitlab, shim, [])
    shim.respond({"cli": "glab", "stderr": "500", "exit": 1})
    with pytest.raises(common.RepoError, match="500"):
        gitlab.failed_reasons(poll)


def test_failed_reasons_empty_unless_checks_failed(shim, hosts):
    github, gitlab = hosts
    poll = _gh_failed_poll(github, shim, [_ok()])
    assert github.failed_reasons(poll) == []
    poll = _gl_failed_poll(gitlab, shim, [], pipeline={"id": 77, "status": "success"})
    before = len(shim.calls())
    assert gitlab.failed_reasons(poll) == []
    assert len(shim.calls()) == before


# --- conflict_files ---------------------------------------------------------


def test_conflict_files(shim, hosts):
    github, gitlab = hosts
    pages = [[{"filename": "a.py"}, {"filename": "b/c.md"}], [{"filename": "d.txt"}, {"sha": "x"}]]
    shim.respond({"cli": "gh", "stdout": json.dumps(pages)})
    assert github.conflict_files(12) == ["a.py", "b/c.md", "d.txt"]
    assert shim.calls()[-1]["argv"] == [
        "api",
        "--paginate",
        "--slurp",
        f"{GH}/pulls/12/files?per_page=100",
    ]
    before = len(shim.calls())
    assert gitlab.conflict_files(7) == []
    assert len(shim.calls()) == before


def test_conflict_files_github_error_is_empty(shim, hosts):
    github, _ = hosts
    shim.respond({"cli": "gh", "stderr": "boom", "exit": 1})
    assert github.conflict_files(12) == []


# --- list_open --------------------------------------------------------------


def _gh_list_rules(prs):
    """gh rules for list_open: the open list, then each PR's detail and checks.
    prs: [(number, created_at, extra pull fields, check_runs)]"""
    listing = [{"number": n, "created_at": created} for n, created, _, _ in prs]
    rules = []
    for n, created, extra, runs in prs:
        pull = _rest_pull(n, sha=f"sha{n}", created_at=created, **extra)
        rules += gh_pull_rules(pull, check_runs=runs, num=n)
    return [
        {
            "cli": "gh",
            "match": [f"{GH}/pulls?state=open&per_page=100"],
            "stdout": json.dumps([listing]),
        },
        *rules,
    ]


def test_list_open_github(shim, hosts):
    github, _ = hosts
    shim.respond(
        *_gh_list_rules(
            [
                (
                    3,
                    "2026-10-03T00:00:00Z",
                    {
                        "title": "newer",
                        "body": "fix it",
                        "mergeable": False,
                        "mergeable_state": "dirty",
                    },
                    [],
                ),
                (
                    1,
                    "2026-10-01T00:00:00Z",
                    {"title": "older", "body": None},
                    [_rest_check("failure")],
                ),
            ]
        )
    )
    items = github.list_open()
    assert [i.pop("raw")["number"] for i in items] == [1, 3]
    assert items == [
        {
            "n": 1,
            "url": "https://github.com/acme/app/pull/1",
            "title": "older",
            "body": "",
            "created": "2026-10-01T00:00:00Z",
            "checks": "failed",
            "mergeable": "mergeable",
        },
        {
            "n": 3,
            "url": "https://github.com/acme/app/pull/3",
            "title": "newer",
            "body": "fix it",
            "created": "2026-10-03T00:00:00Z",
            "checks": "none",
            "mergeable": "conflict",
        },
    ]
    argvs = [c["argv"] for c in shim.calls()]
    assert argvs[0] == ["api", "--paginate", "--slurp", f"{GH}/pulls?state=open&per_page=100"]
    assert ["api", f"{GH}/pulls/3"] in argvs and ["api", f"{GH}/pulls/1"] in argvs


def test_list_open_github_raw_is_the_synthesised_view(shim, hosts):
    github, _ = hosts
    shim.respond(*_gh_list_rules([(1, "2026-10-01T00:00:00Z", {}, [_rest_check()])]))
    raw = github.list_open()[0]["raw"]
    assert raw["mergeable"] == "MERGEABLE" and raw["mergeStateStatus"] == "CLEAN"
    assert raw["statusCheckRollup"][0]["conclusion"] == "SUCCESS"


def test_list_open_gitlab(shim, hosts):
    _, gitlab = hosts
    mrs = [
        {
            "iid": 4,
            "web_url": "https://gitlab.com/grp/sub/proj/-/merge_requests/4",
            "title": "first",
            "description": "the body",
            "created_at": "2026-09-01T00:00:00Z",
            "detailed_merge_status": "mergeable",
            "has_conflicts": False,
            "head_pipeline": {"id": 1, "status": "canceled"},
        },
        {
            "iid": 5,
            "web_url": "https://gitlab.com/grp/sub/proj/-/merge_requests/5",
            "title": "second",
            "description": None,
            "created_at": "2026-09-02T00:00:00Z",
            "detailed_merge_status": "conflict",
            "has_conflicts": True,
        },
    ]
    shim.respond({"cli": "glab", "stdout": json.dumps(mrs)})
    assert gitlab.list_open() == [
        {
            "n": 4,
            "url": "https://gitlab.com/grp/sub/proj/-/merge_requests/4",
            "title": "first",
            "body": "the body",
            "created": "2026-09-01T00:00:00Z",
            "checks": "failed",
            "mergeable": "mergeable",
            "raw": mrs[0],
        },
        {
            "n": 5,
            "url": "https://gitlab.com/grp/sub/proj/-/merge_requests/5",
            "title": "second",
            "body": "",
            "created": "2026-09-02T00:00:00Z",
            "checks": "none",
            "mergeable": "conflict",
            "raw": mrs[1],
        },
    ]
    assert shim.calls()[0]["argv"] == [
        "api",
        f"{GL_MR}?state=opened&order_by=created_at&sort=asc&per_page=100",
    ]


def test_list_open_empty_both_hosts(shim, hosts):
    github, gitlab = hosts
    shim.respond({"cli": "gh", "stdout": "[[]]"}, {"cli": "glab", "stdout": "null"})
    assert github.list_open() == []
    assert gitlab.list_open() == []


# --- no GraphQL-backed gh subcommand, cloud or local -------------------------------


def _gh_world_rules():
    pull = _rest_pull(12, merged=True, merge_commit_sha="m" * 40, state="closed")
    return [
        {"cli": "gh", "match": [f"{GH}/pulls/12/files?per_page=100"], "stdout": "[[]]"},
        {
            "cli": "gh",
            "match": [f"{GH}/pulls?state=open&per_page=100"],
            "stdout": json.dumps([[{"number": 12, "created_at": "x"}]]),
        },
        {
            "cli": "gh",
            "match": [f"{GH}/pulls?state=open&head=acme:b"],
            "stdout": json.dumps([{"number": 12, "html_url": "u"}]),
        },
        {"cli": "gh", "match": [f"{GH}/pulls?head=acme:b&state=all"], "stdout": "[]"},
        {"cli": "gh", "match": [f"{GH}/issues/12/labels?per_page=100"], "stdout": "[[]]"},
        {"cli": "gh", "match": [f"{GH}"], "stdout": json.dumps({"default_branch": "main"})},
        {"cli": "gh", "match": ["POST", f"{GH}/pulls"], "stdout": json.dumps({"html_url": "u"})},
        {"cli": "gh", "match": [f"{GH}/pulls/12/ccr/review_threads"], "stdout": "[]"},
        *gh_pull_rules(pull, num=12),
        {"cli": "gh", "match": ["--slurp"], "stdout": "[[]]"},
    ]


@pytest.mark.parametrize("remote", ["", "true"])
def test_github_never_issues_graphql_backed_subcommands(shim, remote):
    """Retirement pin: `gh pr ...`, `gh repo view` and `gh label ...` are GraphQL on the
    wire, which Claude Code cloud sessions answer with HTTP 403. Every GitHub op is a
    `gh api` REST call (or `gh run`); only the local review-thread count posts graphql."""
    shim.respond(*_gh_world_rules())
    env = {**os.environ, "CLAUDE_CODE_REMOTE": remote}
    gh = scm.GitHub("app", "github.com", "acme/app", env=env)
    ops = [
        lambda: gh.find_open("b"),
        lambda: gh.create("b", "t", "x"),
        lambda: gh.edit_body(12, "x"),
        lambda: gh.semver_label(12),
        lambda: gh.ensure_label("patch"),
        lambda: gh.apply_label(12, "patch"),
        lambda: gh.resolve_branch("b"),
        lambda: gh.poll(12),
        lambda: gh.unresolved_reviews(12),
        lambda: gh.resolve_deploy(12),
        lambda: gh.default_branch(),
        lambda: gh.conflict_files(12),
        lambda: gh.list_open(),
    ]
    for op in ops:
        try:
            op()
        except common.RepoError:
            pass  # resolve_branch has no PR in this world; only the argv matters here
    argvs = [c["argv"] for c in shim.calls()]
    assert len(argvs) >= len(ops)
    for argv in argvs:
        assert argv[0] in ("api", "run"), argv
        if remote:
            assert "graphql" not in argv, argv


# --- unresolved_reviews in a cloud session: the proxy's REST route --------------

CCR = f"{GH}/pulls/12/ccr/review_threads"


def _cloud_github(env=None):
    return scm.GitHub("app", "github.com", "acme/app", env=env)


@pytest.mark.parametrize(
    "answer,count",
    [
        ([], 0),
        ([{"isResolved": False}, {"isResolved": True}, {"isResolved": False}], 2),
        ([{"is_resolved": True}, {"is_resolved": False}], 1),
        ([{"resolved": True}, {"resolved": False}, {}], 2),
        ({"threads": [{"isResolved": False}, {"isResolved": True}]}, 1),
        ({"review_threads": [{"is_resolved": False}]}, 1),
        ({"reviewThreads": {"nodes": [{"isResolved": False}, {"isResolved": False}]}}, 2),
        ({"nodes": [{"isResolved": True}]}, 0),
    ],
)
def test_unresolved_reviews_cloud_counts_unresolved_threads(shim, monkeypatch, answer, count):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")  # the adapter reads os.environ by default
    shim.respond({"cli": "gh", "match": [CCR], "stdout": json.dumps(answer)})
    assert _cloud_github().unresolved_reviews(12) == count
    assert [c["argv"] for c in shim.calls()] == [["api", CCR]]


def test_unresolved_reviews_cloud_flag_comes_from_the_adapter_env(shim):
    shim.respond({"cli": "gh", "match": [CCR], "stdout": json.dumps([{"isResolved": False}])})
    env = {**os.environ, "CLAUDE_CODE_REMOTE": "true"}
    assert _cloud_github(env).unresolved_reviews(12) == 1
    # an explicit env without the flag stays on graphql, whatever os.environ says
    local = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_REMOTE"}
    shim.respond({"cli": "gh", "match": ["graphql"], "stdout": json.dumps({"data": None})})
    with pytest.raises(common.RepoError, match="unexpected reviewThreads"):
        _cloud_github(local).unresolved_reviews(12)
    assert "graphql" in shim.calls()[-1]["argv"]


@pytest.mark.parametrize("answer", ['{"error": "x"}', '"text"', "[1, 2]", "null"])
def test_unresolved_reviews_cloud_unexpected_shape_raises(shim, monkeypatch, answer):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    shim.respond({"cli": "gh", "match": [CCR], "stdout": answer})
    with pytest.raises(common.RepoError, match="unexpected review_threads response"):
        _cloud_github().unresolved_reviews(12)


def test_unresolved_reviews_cloud_http_error_raises(shim, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    shim.respond({"cli": "gh", "stderr": "gh: Not Found (HTTP 404)", "exit": 1})
    with pytest.raises(common.RepoError, match="404"):
        _cloud_github().unresolved_reviews(12)


def test_poll_cloud_carries_reviews_without_graphql(shim, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    shim.respond(
        {"cli": "gh", "match": [CCR], "stdout": json.dumps([{"isResolved": False}])},
        *gh_pull_rules(),
    )
    assert _cloud_github().poll(12)["reviews"] == 1
    assert all("graphql" not in c["argv"] for c in shim.calls())
