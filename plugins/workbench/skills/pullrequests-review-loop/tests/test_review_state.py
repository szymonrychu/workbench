import json
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
import review_state  # noqa: E402
import scm  # noqa: E402


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


GH_SHIM = fake_gh_rest.shim(
    textwrap.dedent(
        """\
    #!/usr/bin/env python3
    import json
    import os
    import sys

    sys.path.insert(0, "__FAKE_GH_DIR__")
    import fake_gh_rest

    BOT = "review-bot"
    OTHER = "someone-else"

    BRANCHES = {"feature-x": "601"}

    PRS = {
        # never reviewed by the bot
        "601": {
            "headRefOid": "headA",
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "url": "https://github.com/acme/repo1/pull/601",
            "reviews": [],
        },
        # CHANGES_REQUESTED on head, substantive body; other reviewer's
        # approval must be filtered out
        "602": {
            "headRefOid": "headB",
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "url": "https://github.com/acme/repo1/pull/602",
            "reviews": [
                {
                    "user": {"login": OTHER},
                    "state": "APPROVED",
                    "submitted_at": "2026-07-01T09:00:00Z",
                    "commit_id": "headB",
                    "body": "lgtm from a human",
                },
                {
                    "user": {"login": BOT},
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2026-07-01T10:00:00Z",
                    "commit_id": "headB",
                    "body": "Found a real bug in foo.go:42",
                },
            ],
        },
        # stale review only: commit differs from current head
        "603": {
            "headRefOid": "headC-new",
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "url": "https://github.com/acme/repo1/pull/603",
            "reviews": [
                {
                    "user": {"login": BOT},
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2026-07-01T10:00:00Z",
                    "commit_id": "headC-old",
                    "body": "stale finding",
                }
            ],
        },
        # flaky flip-flop: CHANGES_REQUESTED then APPROVED, SAME commit
        "604": {
            "headRefOid": "headD",
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "url": "https://github.com/acme/repo1/pull/604",
            "reviews": [
                {
                    "user": {"login": BOT},
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2026-07-01T10:00:00Z",
                    "commit_id": "headD",
                    "body": "Bug: nil deref in bar.go",
                },
                {
                    "user": {"login": BOT},
                    "state": "APPROVED",
                    "submitted_at": "2026-07-01T10:05:00Z",
                    "commit_id": "headD",
                    "body": "Approve. Looks fine now.",
                },
            ],
        },
        # no formal review, but a substantive on-head inline comment
        "605": {
            "headRefOid": "headE",
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "url": "https://github.com/acme/repo1/pull/605",
            "reviews": [],
        },
        # APPROVED on head with an empty body -> not actionable
        "606": {
            "headRefOid": "headF",
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "url": "https://github.com/acme/repo1/pull/606",
            "reviews": [
                {
                    "user": {"login": BOT},
                    "state": "APPROVED",
                    "submitted_at": "2026-07-01T10:00:00Z",
                    "commit_id": "headF",
                    "body": "",
                }
            ],
        },
    }

    COMMENTS = {
        "605": [
            {
                "user": {"login": BOT},
                "path": "foo.go",
                "line": 10,
                "commit_id": "headE",
                "created_at": "2026-07-01T10:00:00Z",
                "body": "nit: rename this variable",
            }
        ],
    }


    def main():
        argv = sys.argv[1:]
        log = os.environ.get("GH_SHIM_LOG")
        if log:
            with open(log, "a") as fh:
                fh.write(json.dumps({"argv": argv, "env": dict(os.environ)}) + "\\n")
        if argv[0] == "api":
            world = {
                "slug": "acme/repo1",
                "prs": {
                    num: {k: v for k, v in data.items() if k != "reviews"}
                    for num, data in PRS.items()
                },
                "branches": BRANCHES,
                "reviews": {num: data["reviews"] for num, data in PRS.items()},
                "comments": COMMENTS,
            }
            # the self-hosted test targets another slug: serve the same data under it
            slug = next((a for a in argv if a.startswith("repos/")), "repos/acme/repo1")
            world["slug"] = "/".join(slug.split("?")[0].split("/")[1:3])
            code = fake_gh_rest.serve(argv, world)
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
def gh_shim(tmp_path, monkeypatch):
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    gh_path = shim_dir / "gh"
    gh_path.write_text(GH_SHIM)
    gh_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("GH_SHIM_LOG", str(shim_dir / "calls.jsonl"))
    return gh_path


def _init_submodule(code_dir, name, origin_url):
    repo = code_dir / name
    repo.mkdir(parents=True)
    _run(["git", "init", "-q", "-b", "main"], repo)
    _run(["git", "remote", "add", "origin", origin_url], repo)
    return repo


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    root = tmp_path / "workbench"
    code_dir = root / "code"
    code_dir.mkdir(parents=True)
    _init_submodule(code_dir, "repo1", "git@github.com:acme/repo1.git")
    (root / ".gitmodules").write_text(
        '[submodule "repo1"]\n\tpath = code/repo1\n\turl = git@github.com:acme/repo1.git\n'
    )
    monkeypatch.chdir(root)
    return root


# -- owner/repo url parsing + adapter slug --


def test_slug_from_url():
    assert scm.slug_from_url("git@github.com:acme/repo1.git") == "acme/repo1"


def test_slug_from_url_garbage_raises():
    with pytest.raises(common.RepoError):
        scm.slug_from_url("not-a-url")


def test_adapter_slug_reads_origin(workbench):
    assert scm.adapter_for("repo1").slug == "acme/repo1"


def test_adapter_unknown_repo_raises(workbench):
    with pytest.raises(common.RepoError):
        scm.adapter_for("nope")


# -- --prs parsing --


def test_parse_pr_pairs():
    assert review_state.parse_pr_pairs("repo1#601,repo2#602") == [
        ("repo1", 601),
        ("repo2", 602),
    ]


def test_parse_pr_pairs_invalid_entry_raises():
    with pytest.raises(common.RepoError):
        review_state.parse_pr_pairs("repo1-601")


# -- reviews_by_reviewer / comments_by_reviewer --


def test_reviews_by_reviewer_filters_and_tags_on_head():
    reviews = [
        {
            "user": {"login": "someone-else"},
            "state": "APPROVED",
            "commit_id": "h",
        },
        {
            "user": {"login": "bot"},
            "state": "CHANGES_REQUESTED",
            "commit_id": "old",
        },
    ]
    out = review_state.reviews_by_reviewer(reviews, "bot", "h")
    assert len(out) == 1
    assert out[0]["state"] == "CHANGES_REQUESTED"
    assert out[0]["onHead"] is False


def test_reviews_by_reviewer_sorts_newest_first():
    reviews = [
        {
            "user": {"login": "bot"},
            "state": "CHANGES_REQUESTED",
            "submitted_at": "2026-07-01T10:00:00Z",
            "commit_id": "h",
        },
        {
            "user": {"login": "bot"},
            "state": "APPROVED",
            "submitted_at": "2026-07-01T10:05:00Z",
            "commit_id": "h",
        },
    ]
    out = review_state.reviews_by_reviewer(reviews, "bot", "h")
    assert [r["state"] for r in out] == ["APPROVED", "CHANGES_REQUESTED"]


def test_comments_by_reviewer_filters_and_tags_on_head():
    comments = [
        {"user": {"login": "someone-else"}, "commit_id": "h", "path": "a.go"},
        {"user": {"login": "bot"}, "commit_id": "old", "path": "b.go"},
    ]
    out = review_state.comments_by_reviewer(comments, "bot", "h")
    assert len(out) == 1
    assert out[0]["path"] == "b.go"
    assert out[0]["onHead"] is False


# -- decision_on_head / is_actionable --


def test_decision_on_head_picks_latest_on_head():
    reviews_all = [
        {"state": "APPROVED", "onHead": True, "body": "ok"},
        {"state": "CHANGES_REQUESTED", "onHead": True, "body": "bug"},
    ]
    assert review_state.decision_on_head(reviews_all) == "APPROVED"


def test_decision_on_head_skips_stale():
    reviews_all = [{"state": "CHANGES_REQUESTED", "onHead": False, "body": "stale"}]
    assert review_state.decision_on_head(reviews_all) == "NONE"


def test_decision_on_head_none_when_no_reviews():
    assert review_state.decision_on_head([]) == "NONE"


def test_is_actionable_changes_requested():
    assert review_state.is_actionable("CHANGES_REQUESTED", [], []) is True


def test_is_actionable_substantive_on_head_review():
    reviews_all = [{"onHead": True, "body": "explains a real bug"}]
    assert review_state.is_actionable("APPROVED", reviews_all, []) is True


def test_is_actionable_substantive_on_head_comment():
    comments = [{"onHead": True, "body": "nit: rename"}]
    assert review_state.is_actionable("NONE", [], comments) is True


def test_is_actionable_false_when_empty_body_and_approved():
    reviews_all = [{"onHead": True, "body": ""}]
    assert review_state.is_actionable("APPROVED", reviews_all, []) is False


def test_is_actionable_false_when_stale_only():
    reviews_all = [{"onHead": False, "body": "stale finding"}]
    assert review_state.is_actionable("NONE", reviews_all, []) is False


# -- fetch_pr_state / process_pr end-to-end scenarios --


def test_never_reviewed_is_none_and_not_actionable(workbench, gh_shim):
    entry = review_state.process_pr("repo1", 601, "review-bot")
    assert "error" not in entry
    assert entry["reviewerDecisionOnHead"] == "NONE"
    assert entry["reviewsAll"] == []
    assert entry["inlineComments"] == []
    assert entry["hasStaleReviews"] is False
    assert entry["actionable"] is False


def test_changes_requested_on_head_is_actionable_and_filters_other_reviewer(workbench, gh_shim):
    entry = review_state.process_pr("repo1", 602, "review-bot")
    assert entry["reviewerDecisionOnHead"] == "CHANGES_REQUESTED"
    assert len(entry["reviewsAll"]) == 1
    assert entry["reviewsAll"][0]["body"] == "Found a real bug in foo.go:42"
    assert entry["actionable"] is True
    assert entry["hasStaleReviews"] is False


def test_stale_review_only_is_none_decision_but_flags_stale(workbench, gh_shim):
    entry = review_state.process_pr("repo1", 603, "review-bot")
    assert entry["reviewerDecisionOnHead"] == "NONE"
    assert entry["hasStaleReviews"] is True
    assert entry["actionable"] is False
    assert entry["reviewsAll"][0]["onHead"] is False


def test_flaky_flipflop_latest_wins_but_history_retained(workbench, gh_shim):
    entry = review_state.process_pr("repo1", 604, "review-bot")
    assert entry["reviewerDecisionOnHead"] == "APPROVED"
    assert entry["hasStaleReviews"] is False
    states = [r["state"] for r in entry["reviewsAll"]]
    assert states == ["APPROVED", "CHANGES_REQUESTED"]
    assert entry["reviewsAll"][1]["body"] == "Bug: nil deref in bar.go"
    assert entry["actionable"] is True


def test_inline_comment_only_is_actionable(workbench, gh_shim):
    entry = review_state.process_pr("repo1", 605, "review-bot")
    assert entry["reviewerDecisionOnHead"] == "NONE"
    assert len(entry["inlineComments"]) == 1
    assert entry["inlineComments"][0]["onHead"] is True
    assert entry["actionable"] is True


def test_approved_empty_body_is_not_actionable(workbench, gh_shim):
    entry = review_state.process_pr("repo1", 606, "review-bot")
    assert entry["reviewerDecisionOnHead"] == "APPROVED"
    assert entry["actionable"] is False


def test_unreachable_pr_reports_error_field(workbench, gh_shim):
    entry = review_state.process_pr("repo1", 999, "review-bot")
    assert "error" in entry
    assert entry["repo"] == "repo1"
    assert entry["pr"] == 999


def test_unknown_repo_reports_error_field(workbench, gh_shim):
    entry = review_state.process_pr("nope", 601, "review-bot")
    assert "error" in entry


def test_process_branch_resolves_pr(workbench, gh_shim):
    entry = review_state.process_branch("repo1", "feature-x", "review-bot")
    assert entry["pr"] == 601
    assert "error" not in entry


def test_process_branch_no_pr_for_branch_reports_error(workbench, gh_shim):
    entry = review_state.process_branch("repo1", "no-such-branch", "review-bot")
    assert "error" in entry


# -- main() CLI wiring --


def test_main_prs_mode_prints_jsonl_and_exits_zero_on_partial_success(workbench, gh_shim, capsys):
    with pytest.raises(SystemExit) as exc_info:
        review_state.main(["--prs", "repo1#601,repo1#999"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.out.splitlines()]
    assert len(lines) == 2
    assert "error" not in lines[0]
    assert "error" in lines[1]


def test_main_exits_nonzero_on_total_failure(workbench, gh_shim, capsys):
    with pytest.raises(SystemExit) as exc_info:
        review_state.main(["--prs", "repo1#999"])
    assert exc_info.value.code == 1


def test_main_branch_mode(workbench, gh_shim, capsys):
    with pytest.raises(SystemExit) as exc_info:
        review_state.main(["--branch", "feature-x", "--repos", "repo1"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    line = json.loads(captured.out.strip())
    assert line["pr"] == 601


@pytest.fixture(autouse=True)
def _reviewer_env(monkeypatch):
    monkeypatch.setenv("WORKBENCH_REVIEWER", "review-bot")


def test_main_env_reviewer_is_used(workbench, gh_shim, capsys):
    with pytest.raises(SystemExit):
        review_state.main(["--prs", "repo1#602"])
    line = json.loads(capsys.readouterr().out.strip())
    assert line["reviewerDecisionOnHead"] == "CHANGES_REQUESTED"


def test_main_flag_reviewer_is_used_without_env(workbench, gh_shim, capsys, monkeypatch):
    monkeypatch.delenv("WORKBENCH_REVIEWER")
    with pytest.raises(SystemExit):
        review_state.main(["--prs", "repo1#602", "--reviewer", "review-bot"])
    line = json.loads(capsys.readouterr().out.strip())
    assert line["reviewerDecisionOnHead"] == "CHANGES_REQUESTED"


def test_flag_beats_env(workbench, gh_shim, capsys, monkeypatch):
    monkeypatch.setenv("WORKBENCH_REVIEWER", "review-bot")
    with pytest.raises(SystemExit):
        review_state.main(["--prs", "repo1#602", "--reviewer", "someone-else"])
    line = json.loads(capsys.readouterr().out.strip())
    assert line["reviewerDecisionOnHead"] == "APPROVED"


def test_no_reviewer_refuses(workbench, gh_shim, capsys, monkeypatch):
    monkeypatch.delenv("WORKBENCH_REVIEWER")
    with pytest.raises(SystemExit) as exc_info:
        review_state.main(["--prs", "repo1#602"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "review_state: no reviewer; pass --reviewer <login> or set WORKBENCH_REVIEWER"
        in captured.err
    )


def test_main_empty_env_reviewer_refuses(workbench, gh_shim, monkeypatch):
    monkeypatch.setenv("WORKBENCH_REVIEWER", "")
    with pytest.raises(SystemExit) as exc_info:
        review_state.main(["--prs", "repo1#602"])
    assert exc_info.value.code == 2


def test_main_custom_reviewer_filters_differently(workbench, gh_shim, capsys):
    with pytest.raises(SystemExit):
        review_state.main(["--prs", "repo1#602", "--reviewer", "someone-else"])
    captured = capsys.readouterr()
    line = json.loads(captured.out.strip())
    assert line["reviewerDecisionOnHead"] == "APPROVED"
    assert len(line["reviewsAll"]) == 1


def test_main_requires_prs_or_branch_and_repos(workbench, gh_shim):
    with pytest.raises(SystemExit) as exc_info:
        review_state.main([])
    assert exc_info.value.code == 2


def test_main_rejects_both_prs_and_branch(workbench, gh_shim):
    with pytest.raises(SystemExit) as exc_info:
        review_state.main(["--prs", "repo1#601", "--branch", "feature-x", "--repos", "repo1"])
    assert exc_info.value.code == 2


# -- hosts: any GitHub host, GitLab unsupported, local-only skipped --


def _add_repo(workbench, name, origin_url):
    _init_submodule(workbench / "code", name, origin_url)
    with (workbench / ".gitmodules").open("a") as fh:
        fh.write(f'[submodule "{name}"]\n\tpath = code/{name}\n\turl = {origin_url}\n')


def test_gitlab_repo_unsupported_exits_nonzero(workbench, gh_shim, capsys):
    _add_repo(workbench, "glrepo", "git@gitlab.com:acme/glrepo.git")
    with pytest.raises(SystemExit) as exc_info:
        review_state.main(["--prs", "glrepo#5,repo1#601"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert (
        "pullrequests-review-loop: glrepo: unsupported on GitLab "
        "(review-loop is GitHub-only)" in captured.err
    )
    lines = [json.loads(line) for line in captured.out.splitlines()]
    assert [entry["repo"] for entry in lines] == ["repo1"]
    assert lines[0]["pr"] == 601


def test_main_local_only_repo_skipped_without_failing(workbench, gh_shim, capsys):
    _add_repo(workbench, "localrepo", "/srv/git/localrepo.git")
    with pytest.raises(SystemExit) as exc_info:
        review_state.main(["--prs", "localrepo#5,repo1#601"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "localrepo: skipped" in captured.err
    assert [json.loads(line)["repo"] for line in captured.out.splitlines()] == ["repo1"]


def test_self_hosted_github_via_host_map(workbench, gh_shim, capsys, monkeypatch):
    _add_repo(workbench, "ghe", "git@git.corp.example:acme/ghe.git")
    monkeypatch.setenv("WORKBENCH_SCM_HOSTS", "git.corp.example=github")
    with pytest.raises(SystemExit) as exc_info:
        review_state.main(["--prs", "ghe#601"])
    assert exc_info.value.code == 0
    line = json.loads(capsys.readouterr().out.strip())
    assert line["repo"] == "ghe"
    assert "error" not in line
    calls = [json.loads(x) for x in (gh_shim.parent / "calls.jsonl").read_text().splitlines()]
    assert calls, "gh was never invoked"
    for call in calls:
        assert "git.corp.example" in " ".join(call["argv"]), call["argv"]
        assert "GH_HOST" not in call["env"]


def test_review_state_uses_rest_only(workbench, gh_shim):
    """Retirement pin: `gh pr view --json` is GraphQL, blocked in Claude Code cloud sessions."""
    review_state.process_pr("repo1", 602, "review-bot")
    review_state.process_branch("repo1", "feature-x", "review-bot")
    calls = [json.loads(x) for x in (gh_shim.parent / "calls.jsonl").read_text().splitlines()]
    assert calls
    for call in calls:
        assert call["argv"][0] == "api" and "graphql" not in call["argv"], call["argv"]


def test_pull_state_is_mapped_from_rest(workbench, gh_shim):
    entry = review_state.process_pr("repo1", 602, "review-bot")
    assert entry["prState"] == "OPEN"
    assert entry["mergeable"] == "MERGEABLE"
    assert entry["url"] == "https://github.com/acme/repo1/pull/602"
    assert entry["headSha"] == "headB"
