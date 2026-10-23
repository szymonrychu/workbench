"""pullrequests-watch on GitHub: every test of the retired GitHub watch skill,
ported onto the host-agnostic watch over scm.GitHub, plus the mixed-host runs."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_lib" / "tests"))

import common  # noqa: E402
import fake_gh_rest  # noqa: E402
import scm  # noqa: E402
import watch  # noqa: E402


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def _gh(repo="repo1", slug="acme/repo1"):
    return scm.GitHub(repo, "github.com", slug)


GH_SHIM = fake_gh_rest.shim(
    textwrap.dedent(
        """\
    #!/usr/bin/env python3
    import json
    import sys

    sys.path.insert(0, "__FAKE_GH_DIR__")
    import fake_gh_rest

    BRANCHES = {"feature-x": "201"}

    PRS = {
        "201": {
            "number": 201,
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "url": "https://github.com/acme/repo1/pull/201",
            "statusCheckRollup": [
                {
                    "name": "ci/build",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "detailsUrl": "https://github.com/acme/repo1/actions/runs/9001/jobs/1",
                }
            ],
        },
        "202": {
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "UNSTABLE",
            "url": "https://github.com/acme/repo1/pull/202",
            "statusCheckRollup": [
                {
                    "name": "ci/lint",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                    "detailsUrl": "https://github.com/acme/repo1/actions/runs/9002/jobs/1",
                }
            ],
        },
        "203": {
            "state": "OPEN",
            "mergeable": "CONFLICTING",
            "mergeStateStatus": "DIRTY",
            "url": "https://github.com/acme/repo1/pull/203",
            "statusCheckRollup": [],
        },
        "204": {
            "state": "MERGED",
            "mergeable": "UNKNOWN",
            "mergeStateStatus": "UNKNOWN",
            "url": "https://github.com/acme/repo1/pull/204",
            "statusCheckRollup": [
                {
                    "name": "ci/build",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "detailsUrl": "https://github.com/acme/repo1/actions/runs/9004/jobs/1",
                }
            ],
        },
        "205": {
            "state": "CLOSED",
            "mergeable": "UNKNOWN",
            "mergeStateStatus": "UNKNOWN",
            "url": "https://github.com/acme/repo1/pull/205",
            "statusCheckRollup": [],
        },
    }

    FILES = {"203": ["a.py", "b.py"]}

    LOGS = {"9002": "\\n".join(f"log line {i}" for i in range(1, 151))}

    WORLD = {
        "slug": "acme/repo1",
        "default_branch": "main",
        "prs": PRS,
        "branches": BRANCHES,
        "files": FILES,
    }


    def main():
        argv = sys.argv[1:]
        if argv[0] == "api":
            code = fake_gh_rest.serve(argv, WORLD)
            if code is not None:
                sys.exit(code)
        if argv[0] == "run" and argv[1] == "view":
            run_id = argv[2]
            if "--log-failed" in argv:
                sys.stdout.write(LOGS.get(run_id, ""))
                return
            print(json.dumps({}))
            return
        sys.stderr.write(f"unhandled gh args: {argv}\\n")
        sys.exit(1)


    if __name__ == "__main__":
        main()
    """
    )
)

# The GitLab side of the mixed-host runs: branch feature-x has MR !201, green.
GLAB_MIXED_SHIM = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import re
    import sys

    MR = {
        "iid": 201,
        "state": "opened",
        "has_conflicts": False,
        "detailed_merge_status": "mergeable",
        "web_url": "https://gitlab.com/acme/glrepo/-/merge_requests/201",
        "head_pipeline": {"id": 9001, "status": "success"},
    }


    def main():
        argv = sys.argv[1:]
        if argv[0] != "api":
            sys.stderr.write(f"unhandled glab args: {argv}\\n")
            sys.exit(1)
        path = argv[1]
        m = re.match(r"^projects/[^/]+/merge_requests\\?source_branch=([^&]+)&state=opened$", path)
        if m:
            print(json.dumps([MR] if m.group(1) == "feature-x" else []))
            return
        if re.match(r"^projects/[^/]+/merge_requests/201$", path):
            print(json.dumps(MR))
            return
        sys.stderr.write(f"unhandled glab api path: {path}\\n")
        sys.exit(1)


    if __name__ == "__main__":
        main()
    """
)


def _install(shim_dir, name, text):
    shim_dir.mkdir(exist_ok=True)
    path = shim_dir / name
    path.write_text(text)
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def _no_host_map(monkeypatch):
    monkeypatch.delenv(scm.HOSTS_ENV, raising=False)


@pytest.fixture
def gh_shim(tmp_path, monkeypatch):
    gh_path = _install(tmp_path / "bin", "gh", GH_SHIM)
    monkeypatch.setenv("PATH", f"{gh_path.parent}:{os.environ['PATH']}")
    return gh_path


@pytest.fixture
def mixed_shims(tmp_path, monkeypatch):
    shim_dir = tmp_path / "mixbin"
    _install(shim_dir, "gh", GH_SHIM)
    _install(shim_dir, "glab", GLAB_MIXED_SHIM)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    return shim_dir


def _init_submodule(code_dir, name, origin_url):
    repo = code_dir / name
    repo.mkdir(parents=True)
    _run(["git", "init", "-q", "-b", "main"], repo)
    _run(["git", "remote", "add", "origin", origin_url], repo)
    return repo


def _make_workbench(root, repos):
    code_dir = root / "code"
    code_dir.mkdir(parents=True)
    gitmodules = ""
    for name, url in repos.items():
        _init_submodule(code_dir, name, url)
        gitmodules += f'[submodule "{name}"]\n\tpath = code/{name}\n\turl = {url}\n'
    (root / ".gitmodules").write_text(gitmodules)
    return root


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    root = _make_workbench(
        tmp_path / "workbench",
        {
            "repo1": "git@github.com:acme/repo1.git",
            "repo2": "https://github.com/acme/repo2.git",
        },
    )
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def mixed_workbench(tmp_path, monkeypatch):
    root = _make_workbench(
        tmp_path / "workbench",
        {
            "ghrepo": "git@github.com:acme/repo1.git",
            "glrepo": "https://gitlab.com/acme/glrepo.git",
        },
    )
    monkeypatch.chdir(root)
    return root


# -- owner/repo url parsing --


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/acme/repo1.git", "acme/repo1"),
        ("https://github.com/acme/repo1", "acme/repo1"),
        ("git@github.com:acme/repo1.git", "acme/repo1"),
        ("git@github.com:acme/repo1", "acme/repo1"),
        ("ssh://git@github.com/acme/repo1.git", "acme/repo1"),
        ("https://github.com/acme/repo1.git/", "acme/repo1"),
    ],
)
def test_owner_repo_from_url(url, expected):
    assert scm.slug_from_url(url) == expected


def test_owner_repo_from_url_garbage_raises():
    with pytest.raises(common.RepoError):
        scm.slug_from_url("not-a-url")


def test_repo_slug_reads_origin(workbench):
    assert scm.adapter_for("repo1").slug == "acme/repo1"
    assert scm.adapter_for("repo2").slug == "acme/repo2"


def test_repo_slug_unknown_repo_raises(workbench):
    with pytest.raises(common.RepoError):
        scm.adapter_for("nope")


# -- --prs parsing --


def test_parse_pr_pairs():
    assert watch.parse_pr_pairs("repo1#123,repo2#45") == [
        ("repo1", 123),
        ("repo2", 45),
    ]


def test_parse_pr_pairs_invalid_entry_raises():
    with pytest.raises(common.RepoError):
        watch.parse_pr_pairs("repo1-123")


# -- checks reduction --


def test_reduce_checks_none():
    assert scm.GitHub.reduce_checks([]) == "none"


def test_reduce_checks_passed():
    rollup = [{"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"}]
    assert scm.GitHub.reduce_checks(rollup) == "passed"


def test_reduce_checks_pending():
    rollup = [{"name": "a", "status": "IN_PROGRESS", "conclusion": None}]
    assert scm.GitHub.reduce_checks(rollup) == "pending"


def test_reduce_checks_failed():
    rollup = [
        {"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "b", "status": "COMPLETED", "conclusion": "FAILURE"},
    ]
    assert scm.GitHub.reduce_checks(rollup) == "failed"


# -- end to end scenarios via process_pr / process_branch --


def test_open_checks_passed_mergeable(workbench, gh_shim):
    report = common.Report()
    watch.process_pr("repo1", 201, report)
    rendered = report.render()
    assert "ok" in rendered
    assert "pr #201:" in rendered
    assert "state=OPEN" in rendered
    assert "checks=passed" in rendered
    assert "mergeable=mergeable" in rendered
    assert report.exit_code == 0


def test_open_checks_failed_reports_check_name_and_log_tail(workbench, gh_shim):
    report = common.Report()
    watch.process_pr("repo1", 202, report)
    rendered = report.render()
    assert "failed" in rendered
    assert "checks=failed" in rendered
    assert "ci/lint" in rendered
    assert "log line 150" in rendered
    assert "log line 1\n" not in rendered
    lines_in_detail = rendered.count("log line ")
    assert lines_in_detail <= 100
    assert report.exit_code == 1


def test_merge_conflict_reports_conflict_and_files(workbench, gh_shim):
    report = common.Report()
    watch.process_pr("repo1", 203, report)
    rendered = report.render()
    assert "failed" in rendered
    assert "conflict" in rendered
    assert "merge conflict in: a.py, b.py" in rendered
    assert report.exit_code == 1


def test_merged_pr_is_ok(workbench, gh_shim):
    report = common.Report()
    watch.process_pr("repo1", 204, report)
    rendered = report.render()
    assert "ok" in rendered
    assert "state=MERGED" in rendered
    assert report.exit_code == 0


def test_closed_pr_is_ok(workbench, gh_shim):
    report = common.Report()
    watch.process_pr("repo1", 205, report)
    rendered = report.render()
    assert "ok" in rendered
    assert "state=CLOSED" in rendered
    assert report.exit_code == 0


def test_unreachable_pr_is_failed(workbench, gh_shim):
    report = common.Report()
    watch.process_pr("repo1", 999, report)
    rendered = report.render()
    assert "failed" in rendered
    assert "pr #999:" in rendered
    assert report.exit_code == 1


def test_process_pr_unknown_repo_is_failed(workbench, gh_shim):
    report = common.Report()
    watch.process_pr("nope", 201, report)
    rendered = report.render()
    assert "failed" in rendered
    assert "nope" in rendered
    assert report.exit_code == 1


def test_process_branch_resolves_pr_for_branch(workbench, gh_shim):
    report = common.Report()
    watch.process_branch("repo1", "feature-x", report)
    rendered = report.render()
    assert "ok" in rendered
    assert "pr #201:" in rendered
    assert "state=OPEN" in rendered
    assert "checks=passed" in rendered


def test_process_branch_no_pr_for_branch_is_failed(workbench, gh_shim):
    report = common.Report()
    watch.process_branch("repo1", "no-such-branch", report)
    rendered = report.render()
    assert "failed" in rendered
    assert "branch no-such-branch: no PR found" in rendered
    assert report.exit_code == 1


# -- main() CLI wiring --


def test_main_prs_mode_exit_code(workbench, gh_shim, capsys):
    with pytest.raises(SystemExit) as exc_info:
        watch.main(["--prs", "repo1#201,repo1#202"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "repo1" in captured.out
    assert "checks=passed" in captured.out
    assert "checks=failed" in captured.out


def test_main_branch_mode_exit_code(workbench, gh_shim, capsys):
    with pytest.raises(SystemExit) as exc_info:
        watch.main(["--branch", "feature-x", "--repos", "repo1"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "repo1" in captured.out
    assert "checks=passed" in captured.out


def test_main_requires_prs_or_branch_and_repos(workbench, gh_shim):
    with pytest.raises(SystemExit) as exc_info:
        watch.main([])
    assert exc_info.value.code == 2


def test_main_rejects_both_prs_and_branch(workbench, gh_shim):
    with pytest.raises(SystemExit) as exc_info:
        watch.main(["--prs", "repo1#201", "--branch", "feature-x", "--repos", "repo1"])
    assert exc_info.value.code == 2


# -- poll / verdict_for / build_detail --


def test_poll_pr_shapes_state(workbench, gh_shim):
    poll = _gh().poll(201)
    assert poll["state"] == "OPEN"
    assert poll["checks"] == "passed"
    assert poll["mergeable"] == "mergeable"
    assert poll["url"] == "https://github.com/acme/repo1/pull/201"


def test_verdict_passed_and_failed():
    passed = {"state": "OPEN", "checks": "passed", "mergeable": "mergeable"}
    failed = {"state": "OPEN", "checks": "failed", "mergeable": "mergeable"}
    assert watch.verdict_for(passed, elapsed=5, grace=30) == "passed"
    assert watch.verdict_for(failed, elapsed=5, grace=30) == "failed"


def test_verdict_conflict_and_merged_and_closed():
    conflict = {"state": "OPEN", "checks": "none", "mergeable": "conflict"}
    merged = {"state": "MERGED", "checks": "passed", "mergeable": "mergeable"}
    closed = {"state": "CLOSED", "checks": "none", "mergeable": "unknown"}
    assert watch.verdict_for(conflict, elapsed=5, grace=30) == "conflict"
    assert watch.verdict_for(merged, elapsed=5, grace=30) == "merged"
    assert watch.verdict_for(closed, elapsed=5, grace=30) == "closed"


def test_verdict_none_before_and_after_grace():
    none_poll = {"state": "OPEN", "checks": "none", "mergeable": "mergeable"}
    assert watch.verdict_for(none_poll, elapsed=10, grace=30) == "pending"
    assert watch.verdict_for(none_poll, elapsed=30, grace=30) == "no-pipeline"


def test_verdict_pending_checks_stay_pending():
    pending = {"state": "OPEN", "checks": "pending", "mergeable": "mergeable"}
    assert watch.verdict_for(pending, elapsed=999, grace=30) == "pending"


def test_build_detail_no_pipeline_reason_and_waited(workbench, gh_shim):
    adapter = _gh()
    poll = adapter.poll(201)
    poll["checks"] = "none"
    ok, detail = watch.build_detail(adapter, 201, poll, waited=30, verdict="no-pipeline")
    assert ok is True
    assert "waited=30" in detail
    assert "no pipeline launched" in detail


def test_build_detail_timeout_is_failed(workbench, gh_shim):
    adapter = _gh()
    poll = adapter.poll(201)
    poll["checks"] = "pending"
    ok, detail = watch.build_detail(adapter, 201, poll, waited=1200, verdict="timeout")
    assert ok is False
    assert "timed out after 1200s" in detail
    assert "checks=pending" in detail


def test_now_and_sleep_are_indirections():
    assert callable(watch._now)
    assert callable(watch._sleep)


GH_WATCH_SHIM = fake_gh_rest.shim(
    textwrap.dedent(
        """\
    #!/usr/bin/env python3
    import json
    import pathlib
    import re
    import sys

    sys.path.insert(0, "__FAKE_GH_DIR__")
    import fake_gh_rest

    COUNTER = pathlib.Path(__file__).with_name("calls.txt")

    def _tick():
        n = int(COUNTER.read_text()) if COUNTER.exists() else 0
        COUNTER.write_text(str(n + 1))
        return n

    # 301: none -> pending -> passed ; 302: pending -> failed ;
    # 303: never launches (always none) ; 304: always pending (timeout)
    def rollup_for(num, tick):
        if num == "301":
            if tick == 0:
                return []
            if tick == 1:
                return [{"name": "ci/build", "status": "IN_PROGRESS", "conclusion": None,
                         "detailsUrl": "https://github.com/acme/repo1/actions/runs/1/jobs/1"}]
            return [{"name": "ci/build", "status": "COMPLETED", "conclusion": "SUCCESS",
                     "detailsUrl": "https://github.com/acme/repo1/actions/runs/1/jobs/1"}]
        if num == "302":
            if tick == 0:
                return [{"name": "ci/test", "status": "IN_PROGRESS", "conclusion": None,
                         "detailsUrl": "https://github.com/acme/repo1/actions/runs/2/jobs/1"}]
            return [{"name": "ci/test", "status": "COMPLETED", "conclusion": "FAILURE",
                     "detailsUrl": "https://github.com/acme/repo1/actions/runs/2/jobs/1"}]
        if num == "303":
            return []
        if num == "304":
            return [{"name": "ci/slow", "status": "IN_PROGRESS", "conclusion": None,
                     "detailsUrl": "https://github.com/acme/repo1/actions/runs/4/jobs/1"}]
        return []

    LOGS = {"2": "\\n".join(f"boom {i}" for i in range(1, 121))}

    def main():
        argv = sys.argv[1:]
        if argv[0] == "api":
            path = fake_gh_rest.parse(argv)[1]
            if re.fullmatch(r"repos/acme/repo1/pulls/\\d+", path):
                tick = _tick()  # one poll = the pull read, then its checks at the same tick
            else:
                tick = int(COUNTER.read_text()) - 1 if COUNTER.exists() else 0
            prs = {
                num: {
                    "state": "OPEN",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "url": f"https://github.com/acme/repo1/pull/{num}",
                    "statusCheckRollup": rollup_for(num, tick),
                }
                for num in ("301", "302", "303", "304")
            }
            code = fake_gh_rest.serve(argv, {"slug": "acme/repo1", "prs": prs})
            if code is not None:
                sys.exit(code)
        if argv[0] == "run" and argv[1] == "view":
            run_id = argv[2]
            if "--log-failed" in argv:
                sys.stdout.write(LOGS.get(run_id, ""))
                return
            print(json.dumps({}))
            return
        sys.stderr.write(f"unhandled gh args: {argv}\\n")
        sys.exit(1)

    if __name__ == "__main__":
        main()
    """
    )
)


@pytest.fixture
def gh_watch_shim(tmp_path, monkeypatch):
    gh_path = _install(tmp_path / "wbin", "gh", GH_WATCH_SHIM)
    monkeypatch.setenv("PATH", f"{gh_path.parent}:{os.environ['PATH']}")
    return gh_path


@pytest.fixture
def fake_clock(monkeypatch):
    state = {"t": 0.0}
    monkeypatch.setattr(watch, "_now", lambda: state["t"])
    monkeypatch.setattr(watch, "_sleep", lambda s: state.__setitem__("t", state["t"] + s))
    return state


# -- watch loop --


def test_watch_reports_success_when_pipeline_passes(fake_clock, gh_watch_shim):
    report = common.Report()
    watch.watch_targets([("repo1", _gh(), 301)], report, interval=10, timeout=1200, grace=30)
    rendered = report.render()
    assert "ok" in rendered
    assert "checks=passed" in rendered
    assert report.exit_code == 0


def test_watch_reports_failure_with_log_tail(fake_clock, gh_watch_shim):
    report = common.Report()
    watch.watch_targets([("repo1", _gh(), 302)], report, interval=10, timeout=1200, grace=30)
    rendered = report.render()
    assert "failed" in rendered
    assert "checks=failed" in rendered
    assert "ci/test" in rendered
    assert "boom 120" in rendered
    assert rendered.count("boom ") <= 100
    assert report.exit_code == 1


def test_watch_no_pipeline_after_grace(fake_clock, gh_watch_shim):
    report = common.Report()
    watch.watch_targets([("repo1", _gh(), 303)], report, interval=10, timeout=1200, grace=30)
    rendered = report.render()
    assert "no pipeline launched" in rendered
    assert "ok" in rendered
    assert report.exit_code == 0


def test_watch_timeout_is_failed(fake_clock, gh_watch_shim):
    report = common.Report()
    watch.watch_targets([("repo1", _gh(), 304)], report, interval=10, timeout=40, grace=30)
    rendered = report.render()
    assert "timed out after" in rendered
    assert "failed" in rendered
    assert report.exit_code == 1


def test_main_no_wait_is_snapshot(workbench, gh_shim, capsys):
    with pytest.raises(SystemExit) as exc_info:
        watch.main(["--no-wait", "--prs", "repo1#201"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "checks=passed" in captured.out
    assert "waited=" not in captured.out


def test_main_default_watches(workbench, gh_watch_shim, fake_clock, capsys):
    with pytest.raises(SystemExit) as exc_info:
        watch.main(["--prs", "repo1#301"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "checks=passed" in captured.out
    assert "waited=" in captured.out


def test_watch_prints_resolved_line_the_moment_a_target_resolves(fake_clock, gh_watch_shim, capsys):
    report = common.Report()
    watch.watch_targets([("repo1", _gh(), 301)], report, interval=10, timeout=1200, grace=30)
    captured = capsys.readouterr()
    assert "RESOLVED" in captured.out
    assert "repo1" in captured.out
    assert "ok" in captured.out
    rendered = report.render()
    assert "checks=passed" in rendered


def test_resolved_line_shape_github(fake_clock, gh_watch_shim, capsys):
    report = common.Report()
    watch.watch_targets([("repo1", _gh(), 301)], report, interval=10, timeout=1200, grace=30)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("RESOLVED")]
    assert lines == [
        f"RESOLVED {'repo1':<20} {'ok':<10} pr #301: state=OPEN checks=passed "
        "mergeable=mergeable url=https://github.com/acme/repo1/pull/301 waited=20"
    ]


# -- shim for two targets resolving on different ticks, per-target counters --
# (the regression test below needs each target's tick to advance
# independently, unlike gh_watch_shim's single shared counter which only
# works for a single-target call)

GH_WATCH_SHIM_PER_TARGET = fake_gh_rest.shim(
    textwrap.dedent(
        """\
    #!/usr/bin/env python3
    import json
    import pathlib
    import re
    import sys

    sys.path.insert(0, "__FAKE_GH_DIR__")
    import fake_gh_rest

    COUNTS_FILE = pathlib.Path(__file__).with_name("calls.json")

    def _tick(num):
        counts = json.loads(COUNTS_FILE.read_text()) if COUNTS_FILE.exists() else {}
        n = counts.get(num, 0)
        counts[num] = n + 1
        COUNTS_FILE.write_text(json.dumps(counts))
        return n

    # 401 resolves on tick 2 (earlier); 402 resolves on tick 4 (later).
    def rollup_for(num, tick):
        threshold = {"401": 2, "402": 4}[num]
        if tick < threshold:
            return [{"name": "ci/build", "status": "IN_PROGRESS", "conclusion": None,
                     "detailsUrl": f"https://github.com/acme/{num}/actions/runs/1/jobs/1"}]
        return [{"name": "ci/build", "status": "COMPLETED", "conclusion": "SUCCESS",
                 "detailsUrl": f"https://github.com/acme/{num}/actions/runs/1/jobs/1"}]

    def main():
        argv = sys.argv[1:]
        if argv[0] == "api":
            path = fake_gh_rest.parse(argv)[1]
            m = re.fullmatch(r"repos/acme/(repo[12])/pulls/(\\d+)", path)
            if m:
                _tick(m.group(2))  # the pull read starts a poll; its checks reuse the tick
            counts = json.loads(COUNTS_FILE.read_text()) if COUNTS_FILE.exists() else {}
            prs = {
                num: {
                    "state": "OPEN",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "url": f"https://github.com/acme/repo/pull/{num}",
                    "statusCheckRollup": rollup_for(num, counts.get(num, 1) - 1),
                }
                for num in ("401", "402")
            }
            slug = "acme/repo1" if "/repo1/" in path else "acme/repo2"
            code = fake_gh_rest.serve(argv, {"slug": slug, "prs": prs})
            if code is not None:
                sys.exit(code)
        sys.stderr.write(f"unhandled gh args: {argv}\\n")
        sys.exit(1)
    if __name__ == "__main__":
        main()
    """
    )
)


@pytest.fixture
def gh_watch_shim_per_target(tmp_path, monkeypatch):
    gh_path = _install(tmp_path / "wbin2", "gh", GH_WATCH_SHIM_PER_TARGET)
    monkeypatch.setenv("PATH", f"{gh_path.parent}:{os.environ['PATH']}")
    return gh_path


def test_watch_resolved_lines_are_edge_triggered_not_batched(
    fake_clock, gh_watch_shim_per_target, capsys
):
    """Regression test for the batch-completion barrier: the earlier-resolving
    target's RESOLVED line must appear before the later one's, proving output
    is emitted per target as it resolves rather than held until the whole
    watch_targets loop finishes."""
    report = common.Report()
    watch.watch_targets(
        [("repo1", _gh("repo1", "acme/repo1"), 401), ("repo2", _gh("repo2", "acme/repo2"), 402)],
        report,
        interval=10,
        timeout=1200,
        grace=30,
    )
    captured = capsys.readouterr()
    resolved_lines = [line for line in captured.out.splitlines() if line.startswith("RESOLVED")]
    assert len(resolved_lines) == 2
    assert "repo1" in resolved_lines[0]
    assert "repo2" in resolved_lines[1]
    rendered = report.render()
    assert "repo1" in rendered
    assert "repo2" in rendered


# -- one run over a GitHub and a GitLab repo --


def test_mixed_hosts_one_stream(mixed_workbench, mixed_shims, fake_clock, capsys):
    with pytest.raises(SystemExit) as exc_info:
        watch.main(["--branch", "feature-x", "--repos", "ghrepo,glrepo"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    resolved = [ln for ln in out.splitlines() if ln.startswith("RESOLVED")]
    assert resolved == [
        f"RESOLVED {'ghrepo':<20} {'ok':<10} pr #201: state=OPEN checks=passed "
        "mergeable=mergeable url=https://github.com/acme/repo1/pull/201 waited=0",
        f"RESOLVED {'glrepo':<20} {'ok':<10} mr !201: state=OPEN checks=passed "
        "mergeable=mergeable reviews=0 unresolved "
        "url=https://gitlab.com/acme/glrepo/-/merge_requests/201 waited=0",
    ]
    table = out.split("REPO ", 1)[1]
    assert "pr #201:" in table
    assert "mr !201:" in table


def test_mixed_hosts_prs_mode(mixed_workbench, mixed_shims, fake_clock, capsys):
    with pytest.raises(SystemExit) as exc_info:
        watch.main(["--prs", "ghrepo#201,glrepo#201"])
    assert exc_info.value.code == 0
    resolved = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("RESOLVED")]
    assert len(resolved) == 2
    assert "ghrepo" in resolved[0] and "pr #201: state=OPEN" in resolved[0]
    assert "glrepo" in resolved[1] and "mr !201: state=OPEN" in resolved[1]


def test_no_wait_snapshot_both_hosts(mixed_workbench, mixed_shims, capsys):
    with pytest.raises(SystemExit) as exc_info:
        watch.main(["--no-wait", "--branch", "feature-x", "--repos", "ghrepo,glrepo"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "RESOLVED" not in out
    assert "waited=" not in out
    rows = out.splitlines()[1:]
    assert len(rows) == 2
    assert rows[0].startswith("ghrepo") and "pr #201: state=OPEN checks=passed" in rows[0]
    assert rows[1].startswith("glrepo") and "mr !201: state=OPEN checks=passed" in rows[1]


def test_malformed_host_map_exits_2(workbench, gh_shim, monkeypatch, capsys):
    monkeypatch.setenv(scm.HOSTS_ENV, "git.corp.example")
    with pytest.raises(SystemExit) as exc_info:
        watch.main(["--prs", "repo1#201"])
    assert exc_info.value.code == 2
    assert "git.corp.example" in capsys.readouterr().err


def test_host_map_routes_a_self_hosted_origin(tmp_path, monkeypatch, gh_shim, capsys):
    root = _make_workbench(tmp_path / "wb", {"repo1": "git@code.corp.example:acme/repo1.git"})
    monkeypatch.chdir(root)
    monkeypatch.setenv(scm.HOSTS_ENV, "code.corp.example=github")
    with pytest.raises(SystemExit) as exc_info:
        watch.main(["--no-wait", "--prs", "repo1#201"])
    assert exc_info.value.code == 0
    assert "pr #201: state=OPEN checks=passed" in capsys.readouterr().out
