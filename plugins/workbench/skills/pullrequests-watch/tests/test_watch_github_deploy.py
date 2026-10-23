"""pullrequests-watch --deploy and --sha on GitHub: the post-merge GitHub Actions
push runs of a commit, reduced to passed|failed|pending|none, through a gh shim."""

import argparse
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

SHA_OK = "deadbeef01deadbeef01deadbeef01deadbeef01"
SHA_BAD = "cafebabe02cafebabe02cafebabe02cafebabe02"
SHA_TIP = "feedface03feedface03feedface03feedface03"
SHA_REL = "abad1dea04abad1dea04abad1dea04abad1dea04"
SHA_PEND = "aaa1aaa1aaa1aaa1aaa1aaa1aaa1aaa1aaa1aaa1"
SHA_NONE = "bbb2bbb2bbb2bbb2bbb2bbb2bbb2bbb2bbb2bbb2"
SHA_STUCK = "ccc3ccc3ccc3ccc3ccc3ccc3ccc3ccc3ccc3ccc3"


@pytest.fixture(autouse=True)
def _no_host_map(monkeypatch):
    monkeypatch.delenv(scm.HOSTS_ENV, raising=False)


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def _gh(repo="repo1", slug="acme/repo1"):
    return scm.GitHub(repo, "github.com", slug)


def _make_workbench(root, repos, branches=None):
    code_dir = root / "code"
    code_dir.mkdir(parents=True)
    gitmodules = ""
    for name, url in repos.items():
        repo = code_dir / name
        repo.mkdir(parents=True)
        _run(["git", "init", "-q", "-b", "main"], repo)
        _run(["git", "remote", "add", "origin", url], repo)
        gitmodules += f'[submodule "{name}"]\n\tpath = code/{name}\n\turl = {url}\n'
        if branches and name in branches:
            gitmodules += f"\tbranch = {branches[name]}\n"
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
        branches={"repo2": "release"},
    )
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def fake_clock(monkeypatch):
    state = {"t": 0.0}
    monkeypatch.setattr(watch, "_now", lambda: state["t"])
    monkeypatch.setattr(watch, "_sleep", lambda s: state.__setitem__("t", state["t"] + s))
    return state


GH_SHIM = fake_gh_rest.shim(
    textwrap.dedent(
        f"""\
    #!/usr/bin/env python3
    import json
    import pathlib
    import sys

    sys.path.insert(0, "__FAKE_GH_DIR__")
    import fake_gh_rest

    SHA_OK = "{SHA_OK}"
    SHA_BAD = "{SHA_BAD}"
    SHA_TIP = "{SHA_TIP}"
    SHA_REL = "{SHA_REL}"
    SHA_PEND = "{SHA_PEND}"
    SHA_STUCK = "{SHA_STUCK}"
    COUNTER = pathlib.Path(__file__).with_name("calls.txt")
    LOG = pathlib.Path(__file__).with_name("argv.log")

    PRS = {{
        "501": {{"state": "MERGED", "mergeCommit": SHA_OK, "baseRefName": "main"}},
        "502": {{"state": "MERGED", "mergeCommit": SHA_BAD, "baseRefName": "main"}},
        "503": {{"state": "OPEN", "mergeCommit": None, "baseRefName": "main"}},
    }}
    WORLD = {{"slug": "acme/repo1", "default_branch": "trunk", "prs": PRS}}
    TIPS = {{"trunk": SHA_TIP, "release": SHA_REL, "main": SHA_OK}}

    def run(rid, wf, status, conclusion, sha, n=1):
        return {{
            "databaseId": rid, "workflowName": wf, "status": status,
            "conclusion": conclusion, "headSha": sha,
            "url": "https://github.com/acme/repo1/actions/runs/%d" % rid,
        }}

    RUNS = {{
        SHA_OK: [run(11, "CI", "completed", "success", SHA_OK)],
        SHA_BAD: [
            run(21, "CI", "completed", "success", SHA_BAD),
            run(77, "Deploy", "completed", "failure", SHA_BAD),
        ],
        SHA_TIP: [
            run(31, "CI", "completed", "success", SHA_TIP),
            run(32, "Lint", "completed", "skipped", SHA_TIP),
            run(33, "Docs", "completed", "neutral", SHA_TIP),
            run(34, "OldCI", "completed", "failure", "0" * 40),
        ],
        SHA_REL: [run(41, "Release", "completed", "success", SHA_REL)],
        SHA_STUCK: [run(51, "CI", "in_progress", None, SHA_STUCK)],
    }}

    def tick(key):
        state = {{}}
        if COUNTER.exists():
            for line in COUNTER.read_text().splitlines():
                k, v = line.split("=")
                state[k] = int(v)
        n = state.get(key, 0)
        state[key] = n + 1
        COUNTER.write_text("\\n".join(f"{{k}}={{v}}" for k, v in state.items()))
        return n

    def opt(argv, name):
        return argv[argv.index(name) + 1] if name in argv else None

    def main():
        argv = sys.argv[1:]
        with LOG.open("a") as f:
            f.write(json.dumps(argv) + "\\n")
        if argv[0] == "api":
            prefix = "repos/acme/repo1/branches/"
            path = argv[1]
            for slug in ("repo1", "repo2"):
                prefix = f"repos/acme/{{slug}}/branches/"
                if path.startswith(prefix):
                    b = path[len(prefix):]
                    if b not in TIPS:
                        sys.stderr.write("gh: Branch not found (HTTP 404)\\n")
                        sys.exit(1)
                    print(json.dumps({{"name": b, "commit": {{"sha": TIPS[b]}}}}))
                    return
            code = fake_gh_rest.serve(argv, WORLD)
            if code is not None:
                sys.exit(code)
        if argv[:2] == ["run", "list"]:
            assert opt(argv, "--event") == "push", argv
            sha = opt(argv, "--commit")
            runs = list(RUNS.get(sha, []))
            runs.append(run(99, "Other", "completed", "failure", "9" * 40))
            if sha == SHA_PEND:
                t = tick(sha)
                if t == 0:
                    runs = []
                elif t == 1:
                    runs = [run(61, "CI", "queued", None, sha)]
                else:
                    runs = [run(61, "CI", "completed", "success", sha)]
            elif sha == "bbb2bbb2bbb2bbb2bbb2bbb2bbb2bbb2bbb2bbb2":
                runs = []
            print(json.dumps(runs))
            return
        if argv[:2] == ["run", "view"]:
            assert "--log-failed" in argv
            if argv[2] == "77":
                sys.stdout.write("\\n".join("deploy log line %d" % i for i in range(1, 151)))
                return
            sys.stderr.write("no such run\\n")
            sys.exit(1)
        sys.stderr.write("unhandled gh args: %s\\n" % argv)
        sys.exit(1)

    main()
    """
    )
)


@pytest.fixture
def gh_shim(tmp_path, monkeypatch):
    shim_dir = tmp_path / "ghbin"
    shim_dir.mkdir()
    path = shim_dir / "gh"
    path.write_text(GH_SHIM)
    path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    return shim_dir


# -- adapter: resolve_deploy --


def test_resolve_deploy_merged_pr(workbench, gh_shim):
    assert _gh().resolve_deploy(501) == ("main", SHA_OK)


def test_resolve_deploy_unmerged_pr_fails_clearly(workbench, gh_shim):
    with pytest.raises(common.RepoError) as exc_info:
        _gh().resolve_deploy(503)
    assert str(exc_info.value) == "pr #503 not merged yet - deploy-watch is a post-merge action"


# -- adapter: run reduction --


def _r(status, conclusion=None):
    return {"status": status, "conclusion": conclusion}


@pytest.mark.parametrize(
    "conclusion", ["failure", "cancelled", "timed_out", "action_required", "startup_failure"]
)
def test_run_state_failed_conclusions(conclusion):
    assert scm.GitHub.run_state(_r("completed", conclusion)) == "failed"


@pytest.mark.parametrize(
    "status", ["queued", "in_progress", "waiting", "requested", "pending", "stale"]
)
def test_run_state_pending_statuses(status):
    assert scm.GitHub.run_state(_r(status)) == "pending"


@pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral"])
def test_run_state_success_conclusions(conclusion):
    assert scm.GitHub.run_state(_r("completed", conclusion)) == "passed"


def test_reduce_runs_precedence():
    ok, bad, pend = _r("completed", "success"), _r("completed", "failure"), _r("queued")
    assert scm.GitHub.reduce_runs([]) == "none"
    assert scm.GitHub.reduce_runs([ok, _r("completed", "skipped")]) == "passed"
    assert scm.GitHub.reduce_runs([ok, pend]) == "pending"
    assert scm.GitHub.reduce_runs([pend, bad, ok]) == "failed"


# -- adapter: poll_deploy --


def test_poll_deploy_sha_pinned_success(workbench, gh_shim):
    poll = _gh().poll_deploy("main", SHA_OK)
    assert poll["branch"] == "main"
    assert poll["sha"] == SHA_OK
    assert poll["checks"] == "passed"
    assert poll["pipeline"]["web_url"] == "https://github.com/acme/repo1/actions/runs/11"


def test_poll_deploy_uses_push_run_list_for_sha(workbench, gh_shim):
    _gh().poll_deploy("main", SHA_OK)
    calls = [line for line in (gh_shim / "argv.log").read_text().splitlines() if "run" in line]
    assert any(
        '"run", "list"' in c
        and '"--branch", "main"' in c
        and f'"--commit", "{SHA_OK}"' in c
        and '"--event", "push"' in c
        and "databaseId,status,conclusion,headSha,url,workflowName" in c
        for c in calls
    )


def test_poll_deploy_failed_run(workbench, gh_shim):
    poll = _gh().poll_deploy("main", SHA_BAD)
    assert poll["checks"] == "failed"
    assert poll["pipeline"]["web_url"] == "https://github.com/acme/repo1/actions/runs/77"


def test_poll_deploy_branch_tip_pinned_and_head_sha_filtered(workbench, gh_shim):
    # The tip's runs include an old-sha failure that must be ignored.
    poll = _gh().poll_deploy("trunk")
    assert poll["sha"] == SHA_TIP
    assert poll["checks"] == "passed"


def test_poll_deploy_no_runs_is_none(workbench, gh_shim):
    poll = _gh().poll_deploy("main", SHA_NONE)
    assert poll["checks"] == "none"
    assert poll["pipeline"] is None


def test_poll_deploy_unknown_branch_raises(workbench, gh_shim):
    with pytest.raises(common.RepoError):
        _gh().poll_deploy("nope")


def test_deploy_failed_reasons_names_workflow_and_log_tail(workbench, gh_shim):
    gh = _gh()
    reasons = gh.deploy_failed_reasons(gh.poll_deploy("main", SHA_BAD))
    assert len(reasons) == 1
    assert reasons[0].startswith("run 'Deploy' failed:")
    assert "deploy log line 150" in reasons[0]
    assert "deploy log line 50\n" not in reasons[0]


def test_deploy_failed_reasons_empty_unless_failed(workbench, gh_shim):
    gh = _gh()
    assert gh.deploy_failed_reasons(gh.poll_deploy("main", SHA_OK)) == []


def test_default_branch_reads_the_repo_default_branch(workbench, gh_shim):
    assert _gh().default_branch() == "trunk"


# -- branch resolution: tracked branch first, repo default only as fallback --


def test_deploy_branch_prefers_tracked_branch(workbench, gh_shim):
    assert _gh("repo2", "acme/repo2").deploy_branch() == "release"


def test_deploy_branch_falls_back_to_repo_default(workbench, gh_shim):
    assert _gh("repo1", "acme/repo1").deploy_branch() == "trunk"


# -- process_deploy_* --


def test_process_deploy_pr_success(workbench, gh_shim):
    report = common.Report()
    watch.process_deploy_pr("repo1", 501, report)
    rendered = report.render()
    assert "pr #501 deploy: branch=main sha=deadbeef status=passed" in rendered
    assert "url=https://github.com/acme/repo1/actions/runs/11" in rendered
    assert report.exit_code == 0


def test_process_deploy_pr_failed_has_run_name_and_log_tail(workbench, gh_shim):
    report = common.Report()
    watch.process_deploy_pr("repo1", 502, report)
    rendered = report.render()
    assert "status=failed" in rendered
    assert "run 'Deploy' failed:" in rendered
    assert "deploy log line 150" in rendered
    assert report.exit_code == 1


def test_process_deploy_pr_not_merged(workbench, gh_shim):
    report = common.Report()
    watch.process_deploy_pr("repo1", 503, report)
    assert "pr #503 not merged yet - deploy-watch is a post-merge action" in report.render()
    assert report.exit_code == 1


def test_process_deploy_branch_default_branch_fallback(workbench, gh_shim):
    report = common.Report()
    watch.process_deploy_branch("repo1", None, None, report)
    rendered = report.render()
    assert "deploy: branch=trunk sha=feedface status=passed" in rendered
    assert report.exit_code == 0


def test_process_deploy_branch_uses_tracked_branch(workbench, gh_shim):
    report = common.Report()
    watch.process_deploy_branch("repo2", None, None, report)
    assert "deploy: branch=release sha=abad1dea status=passed" in report.render()


def test_process_deploy_branch_explicit_branch_overrides(workbench, gh_shim):
    report = common.Report()
    watch.process_deploy_branch("repo2", "trunk", None, report)
    assert "deploy: branch=trunk sha=feedface" in report.render()


def test_process_deploy_branch_sha_pin(workbench, gh_shim):
    report = common.Report()
    watch.process_deploy_branch("repo1", "main", SHA_BAD, report)
    rendered = report.render()
    assert "status=failed" in rendered
    assert "run 'Deploy' failed:" in rendered
    assert report.exit_code == 1


# -- watch loop --


def test_watch_deploy_pending_then_passed(fake_clock, workbench, gh_shim):
    report = common.Report()
    watch.watch_deploy_targets(
        [("repo1", _gh(), "main", SHA_PEND, "deploy")], report, interval=10, timeout=1200, grace=30
    )
    assert "status=passed" in report.render()
    assert report.exit_code == 0


def test_watch_deploy_no_pipeline_after_grace(fake_clock, workbench, gh_shim):
    report = common.Report()
    watch.watch_deploy_targets(
        [("repo1", _gh(), "main", SHA_NONE, "deploy")], report, interval=10, timeout=1200, grace=30
    )
    rendered = report.render()
    assert "no pipeline launched" in rendered
    assert report.exit_code == 0


def test_watch_deploy_timeout_is_failed(fake_clock, workbench, gh_shim):
    report = common.Report()
    watch.watch_deploy_targets(
        [("repo1", _gh(), "main", SHA_STUCK, "deploy")], report, interval=10, timeout=40, grace=30
    )
    rendered = report.render()
    assert "timed out after" in rendered
    assert report.exit_code == 1


# -- main() wiring --


def test_deploy_targets_from_args_prs_mode(workbench, gh_shim):
    report = common.Report()
    args = argparse.Namespace(prs="repo1#501", branch=None, repos=None, sha=None)
    targets = watch._deploy_targets_from_args(args, report)
    assert [(t[0], t[2], t[3], t[4]) for t in targets] == [
        ("repo1", "main", SHA_OK, "pr #501 deploy")
    ]


def test_deploy_targets_from_args_repos_mode(workbench, gh_shim):
    report = common.Report()
    args = argparse.Namespace(prs=None, branch=None, repos="repo1,repo2", sha=f"repo1={SHA_BAD}")
    targets = watch._deploy_targets_from_args(args, report)
    assert [(t[0], t[2], t[3]) for t in targets] == [
        ("repo1", "trunk", SHA_BAD),
        ("repo2", "release", None),
    ]


def test_main_deploy_prs_no_wait(workbench, gh_shim, capsys):
    with pytest.raises(SystemExit) as exc:
        watch.main(["--deploy", "--prs", "repo1#501", "--no-wait"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "pr #501 deploy: branch=main" in out
    assert "status=passed" in out


def test_main_deploy_prs_unmerged_exits_1(workbench, gh_shim, capsys):
    with pytest.raises(SystemExit) as exc:
        watch.main(["--deploy", "--prs", "repo1#503", "--no-wait"])
    assert exc.value.code == 1
    assert "not merged yet - deploy-watch is a post-merge action" in capsys.readouterr().out


def test_main_deploy_repos_watch_mode_with_sha(workbench, gh_shim, capsys):
    with pytest.raises(SystemExit) as exc:
        watch.main(["--deploy", "--repos", "repo1", "--sha", f"repo1={SHA_OK}", "--interval", "0"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "RESOLVED repo1" in out
    assert "deploy: branch=trunk" in out
    assert "sha=deadbeef" in out


# -- acceptance tests named by the task --


def test_github_deploy_prs_merge_commit_runs(workbench, gh_shim):
    report = common.Report()
    watch.process_deploy_pr("repo1", 501, report)
    assert "pr #501 deploy: branch=main sha=deadbeef status=passed" in report.render()
    unmerged = common.Report()
    watch.process_deploy_pr("repo1", 503, unmerged)
    assert "pr #503 not merged yet - deploy-watch is a post-merge action" in unmerged.render()


def test_github_deploy_branch_mode_tip_and_sha(workbench, gh_shim):
    tip = common.Report()
    watch.process_deploy_branch("repo1", None, None, tip)
    assert "sha=feedface" in tip.render()
    pinned = common.Report()
    watch.process_deploy_branch("repo1", "main", SHA_BAD, pinned)
    assert "status=failed" in pinned.render()


def test_github_deploy_failed_run_tail(workbench, gh_shim):
    report = common.Report()
    watch.process_deploy_pr("repo1", 502, report)
    rendered = report.render()
    assert "run 'Deploy' failed:" in rendered
    assert "deploy log line 150" in rendered


def test_github_deploy_prs_uses_merge_commit(workbench, gh_shim):
    assert _gh().resolve_deploy(501) == ("main", SHA_OK)
    report = common.Report()
    watch.process_deploy_pr("repo1", 501, report)
    assert "pr #501 deploy: branch=main sha=deadbeef" in report.render()


def test_github_deploy_unmerged_pr_fails(workbench, gh_shim):
    with pytest.raises(common.RepoError) as exc_info:
        _gh().resolve_deploy(503)
    assert str(exc_info.value) == "pr #503 not merged yet - deploy-watch is a post-merge action"
    with pytest.raises(SystemExit) as exc:
        watch.main(["--deploy", "--prs", "repo1#503", "--no-wait"])
    assert exc.value.code == 1


def test_github_deploy_branch_mode_and_sha(workbench, gh_shim):
    assert _gh().deploy_branch() == "trunk"
    assert scm.GitHub("repo2", "github.com", "acme/repo2").deploy_branch() == "release"
    tip = common.Report()
    watch.process_deploy_branch("repo1", None, None, tip)
    assert "sha=feedface" in tip.render()
    pinned = common.Report()
    watch.process_deploy_branch("repo1", "main", SHA_BAD, pinned)
    assert "sha=cafebabe" in pinned.render()
    assert "status=failed" in pinned.render()


def test_github_deploy_run_reduction(workbench, gh_shim):
    assert _gh().poll_deploy("main", SHA_OK)["checks"] == "passed"
    assert _gh().poll_deploy("main", SHA_BAD)["checks"] == "failed"
    assert _gh().poll_deploy("main", SHA_STUCK)["checks"] == "pending"
    assert _gh().poll_deploy("main", SHA_NONE)["checks"] == "none"
    assert scm.GitHub.reduce_runs([_r("queued"), _r("completed", "failure")]) == "failed"
    assert scm.GitHub.reduce_runs([_r("completed", "skipped")]) == "passed"


@pytest.mark.parametrize(
    "run,expected",
    [
        (_r("completed", c), "failed")
        for c in ("failure", "cancelled", "timed_out", "action_required", "startup_failure")
    ]
    + [
        (_r(s), "pending")
        for s in ("queued", "in_progress", "waiting", "requested", "pending", "stale")
    ]
    + [(_r("completed", c), "passed") for c in ("success", "skipped", "neutral")],
)
def test_github_deploy_status_mapping(run, expected):
    assert scm.GitHub.run_state(run) == expected


GLAB_DEPLOY_SHIM = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import sys

    argv = sys.argv[1:]
    path = argv[1] if len(argv) > 1 else ""
    if argv[0] == "api" and "/merge_requests/401" in path:
        print(json.dumps({"merge_commit_sha": "glsha0000", "target_branch": "main"}))
    elif argv[0] == "api" and "/pipelines" in path:
        print(json.dumps([{"id": 1, "status": "success",
                           "web_url": "https://gitlab.com/acme/glrepo/-/pipelines/1"}]))
    else:
        sys.stderr.write("unhandled glab args: %s\\n" % argv)
        sys.exit(1)
    """
)


def test_mixed_hosts_deploy(tmp_path, monkeypatch, gh_shim, capsys):
    root = _make_workbench(
        tmp_path / "mixed",
        {
            "ghrepo": "git@github.com:acme/repo1.git",
            "glrepo": "https://gitlab.com/acme/glrepo.git",
        },
    )
    monkeypatch.chdir(root)
    glab = gh_shim / "glab"
    glab.write_text(GLAB_DEPLOY_SHIM)
    glab.chmod(0o755)
    with pytest.raises(SystemExit) as exc:
        watch.main(["--deploy", "--prs", "ghrepo#501,glrepo#401", "--no-wait"])
    out = capsys.readouterr().out
    assert exc.value.code == 0, out
    assert "pr #501 deploy: branch=main sha=deadbeef" in out
    assert "mr !401 deploy: branch=main sha=glsha000 status=passed" in out
