"""pullrequests-watch on GitHub: unresolved review threads via `gh api graphql`,
so reviews=<N> unresolved matches GitLab and the merge gate reviews=0 holds."""

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_lib"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_lib" / "tests"))

import common  # noqa: E402
import fake_gh_rest  # noqa: E402
import scm  # noqa: E402
import test_watch_github as base  # noqa: E402
import watch  # noqa: E402

# PR 201: open, 3 threads (1 unresolved, 1 resolved, 1 unresolved+outdated) -> 2.
# PR 202: open, 150 threads over two pages, every 3rd resolved.
# PR 203: merged.  PR 204: open, graphql errors.  PR 205: open, no threads.
GH_SHIM = fake_gh_rest.shim(
    textwrap.dedent(
        """\
    #!/usr/bin/env python3
    import json
    import pathlib
    import sys

    sys.path.insert(0, "__FAKE_GH_DIR__")
    import fake_gh_rest

    LOG = pathlib.Path(__file__).with_name("calls.jsonl")
    PRS = {
        "201": "OPEN", "202": "OPEN", "203": "MERGED", "204": "OPEN", "205": "OPEN",
    }
    THREADS = {
        "201": [
            {"isResolved": False, "isOutdated": False},
            {"isResolved": True, "isOutdated": False},
            {"isResolved": False, "isOutdated": True},
        ],
        "202": [{"isResolved": i % 3 == 0, "isOutdated": False} for i in range(150)],
        "203": [{"isResolved": False, "isOutdated": False}],
        "205": [],
    }


    def arg(argv, key):
        for i, a in enumerate(argv):
            if a in ("-F", "-f") and argv[i + 1].startswith(key + "="):
                return argv[i + 1].split("=", 1)[1]
        return None


    def main():
        argv = sys.argv[1:]
        with LOG.open("a") as fh:
            fh.write(json.dumps(argv) + "\\n")
        if argv[0] == "api" and "ccr/review_threads" in argv[1]:
            num = argv[1].split("/")[-3]
            if num == "204":
                sys.stderr.write("gh: HTTP 502\\n")
                sys.exit(1)
            print(json.dumps(THREADS.get(num, [])))
            return
        if argv[0] == "api" and argv[1] != "graphql":
            world = {
                "slug": "acme/repo1",
                "prs": {
                    num: {
                        "state": state,
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "statusCheckRollup": [
                            {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
                        ],
                    }
                    for num, state in PRS.items()
                },
            }
            code = fake_gh_rest.serve(argv, world)
            if code is not None:
                sys.exit(code)
        if argv[:2] == ["api", "graphql"]:
            num = arg(argv, "number")
            if num == "204":
                sys.stderr.write("HTTP 502\\n")
                sys.exit(1)
            assert arg(argv, "owner") == "acme" and arg(argv, "name") == "repo1"
            cursor = arg(argv, "cursor")
            start = int(cursor) if cursor else 0
            threads = THREADS[num]
            nodes = threads[start:start + 100]
            more = start + 100 < len(threads)
            print(json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": more, "endCursor": str(start + 100)},
                "nodes": nodes,
            }}}}}))
            return
        sys.stderr.write(f"unhandled gh args: {argv}\\n")
        sys.exit(1)


    if __name__ == "__main__":
        main()
    """
    )
)


@pytest.fixture(autouse=True)
def _no_host_map(monkeypatch):
    monkeypatch.delenv(scm.HOSTS_ENV, raising=False)
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)


@pytest.fixture
def gh(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path = bin_dir / "gh"
    path.write_text(GH_SHIM)
    path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    log = bin_dir / "calls.jsonl"

    def calls(kind=None):
        rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        return [r for r in rows if kind is None or r[:2] == kind]

    return calls


def _adapter():
    return scm.GitHub("repo1", "github.com", "acme/repo1")


def test_github_counts_only_unresolved_threads(gh):
    assert _adapter().unresolved_reviews(201) == 2


def test_unresolved_reviews_zero_without_threads(gh):
    assert _adapter().unresolved_reviews(205) == 0


def test_github_reviews_one_graphql_call_per_poll(gh):
    _adapter().unresolved_reviews(201)
    assert len(gh(["api", "graphql"])) == 1


def test_github_review_threads_paginate(gh):
    # 150 threads, indexes divisible by 3 resolved: 50 resolved, 100 unresolved.
    assert _adapter().unresolved_reviews(202) == 100
    assert len(gh(["api", "graphql"])) == 2


def test_poll_open_pr_carries_reviews(gh):
    assert _adapter().poll(201)["reviews"] == 2


def test_poll_merged_pr_has_no_reviews_and_no_graphql_call(gh):
    poll = _adapter().poll(203)
    assert "reviews" not in poll
    assert gh(["api", "graphql"]) == []


def test_poll_degrades_when_graphql_fails(gh):
    poll = _adapter().poll(204)
    assert poll["checks"] == "passed"
    assert "reviews" not in poll


def test_github_review_reason_matches_gitlab(gh):
    adapter = _adapter()
    ok, detail = watch.build_detail(adapter, 201, adapter.poll(201))
    assert ok is True
    assert (
        "mergeable=mergeable reviews=2 unresolved url=https://github.com/acme/repo1/pull/201"
        in detail
    )
    assert "2 unresolved review discussions" in detail


def test_github_detail_reviews_zero(gh):
    adapter = _adapter()
    ok, detail = watch.build_detail(adapter, 205, adapter.poll(205))
    assert "reviews=0 unresolved" in detail
    assert "unresolved review discussion" not in detail


def test_detail_merged_pr_has_no_reviews_field(gh):
    adapter = _adapter()
    ok, detail = watch.build_detail(adapter, 203, adapter.poll(203))
    assert "reviews=" not in detail


def test_reviews_do_not_flip_ok(gh):
    adapter = _adapter()
    ok, _ = watch.build_detail(adapter, 202, adapter.poll(202))
    assert ok is True


def test_no_wait_snapshot_emits_reviews(gh, monkeypatch):
    monkeypatch.setattr(watch, "_adapter", lambda repo, report: _adapter())
    report = common.Report()
    watch.process_pr("repo1", 201, report)
    assert "reviews=2 unresolved" in report.render()


def test_github_reviews_open_only_and_outdated_counted(gh, monkeypatch, capsys):
    # outdated unresolved threads count (PR 201 has one of its two)
    assert _adapter().poll(201)["reviews"] == 2
    # merged PR: no reviews= and no graphql call, in detail and in --no-wait
    assert "reviews=" not in watch.build_detail(_adapter(), 203, _adapter().poll(203))[1]
    monkeypatch.setattr(watch, "_adapter", lambda repo, report: _adapter())
    report = common.Report()
    watch.process_pr("repo1", 203, report)
    assert "reviews=" not in report.render()
    assert not [c for c in gh(["api", "graphql"]) if "number=203" in c]


def test_mixed_hosts_both_carry_reviews(tmp_path, monkeypatch, gh, capsys):
    shim_dir = tmp_path / "mixbin"
    base._install(shim_dir, "gh", GH_SHIM)
    base._install(shim_dir, "glab", base.GLAB_MIXED_SHIM)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    root = base._make_workbench(
        tmp_path / "wb",
        {
            "repo1": "git@github.com:acme/repo1.git",
            "glrepo": "https://gitlab.com/acme/glrepo.git",
        },
    )
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit) as exc_info:
        watch.main(["--no-wait", "--prs", "repo1#201,glrepo#201"])
    assert exc_info.value.code == 0
    rows = capsys.readouterr().out.splitlines()[1:]
    assert "pr #201:" in rows[0] and "reviews=2 unresolved" in rows[0]
    assert "mr !201:" in rows[1] and "reviews=0 unresolved" in rows[1]


# -- in a Claude Code cloud session graphql is blocked: the proxy's REST route --


def _review_thread_calls(gh):
    return [c for c in gh() if c[0] == "api" and "ccr/review_threads" in c[1]]


def test_cloud_counts_unresolved_threads_over_rest(gh, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert _adapter().unresolved_reviews(201) == 2
    assert _review_thread_calls(gh) == [["api", "repos/acme/repo1/pulls/201/ccr/review_threads"]]
    assert gh(["api", "graphql"]) == []


def test_cloud_threads_past_the_first_hundred_come_in_one_call(gh, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert _adapter().unresolved_reviews(202) == 100
    assert len(_review_thread_calls(gh)) == 1


def test_cloud_poll_and_detail_carry_reviews_without_graphql(gh, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    adapter = _adapter()
    ok, detail = watch.build_detail(adapter, 201, adapter.poll(201))
    assert ok is True
    assert "reviews=2 unresolved" in detail
    assert gh(["api", "graphql"]) == []


def test_cloud_merged_pr_makes_no_review_call(gh, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert "reviews" not in _adapter().poll(203)
    assert _review_thread_calls(gh) == []


def test_cloud_poll_degrades_when_the_route_fails(gh, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    poll = _adapter().poll(204)
    assert poll["checks"] == "passed"
    assert "reviews" not in poll
