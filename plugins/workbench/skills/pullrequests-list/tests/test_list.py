import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_lib" / "tests"))

import fake_gh_rest  # noqa: E402
import list as plist  # noqa: E402
import scm  # noqa: E402


class FakeAdapter:
    def __init__(self, items):
        self.items = items

    def list_open(self):
        return self.items


def gl_item(url, status=None, detail="mergeable", conflicts=False, body="", created="2026-01-01"):
    pipeline = {"status": status} if status else None
    return {
        "url": url,
        "body": body,
        "created": created,
        "checks": scm.GitLab.reduce_checks(pipeline),
        "mergeable": scm.GitLab.mergeability(detail, conflicts),
    }


def gh_item(url, mergeable="MERGEABLE", state="CLEAN", rollup=None, body="", created="2026-01-01"):
    return {
        "url": url,
        "body": body,
        "created": created,
        "checks": scm.GitHub.reduce_checks(rollup),
        "mergeable": scm.GitHub.mergeability(mergeable, state),
    }


def run(monkeypatch, capsys, adapter):
    monkeypatch.setattr(scm, "adapter_for", lambda repo: adapter)
    monkeypatch.setattr(scm, "origin_url", lambda repo: "git@example.org:a/b.git")
    with pytest.raises(SystemExit) as exc:
        plist.main(["--repo", "r"])
    return exc.value.code, capsys.readouterr().out


@pytest.mark.parametrize(
    "item,code",
    [
        (gl_item("u", "success"), "G"),
        (gl_item("u"), "G"),
        (gl_item("u", detail="conflict"), "C"),
        (gl_item("u", conflicts=True), "C"),
        (gl_item("u", "failed"), "R"),
        (gl_item("u", "canceled"), "R"),
        (gl_item("u", "failed", detail="conflict"), "C"),
        (gl_item("u", "running", detail="not_approved"), "G"),
        (gh_item("u"), "G"),
        (gh_item("u", "CONFLICTING", "DIRTY"), "C"),
        (gh_item("u", rollup=[{"conclusion": "FAILURE", "status": "COMPLETED"}]), "R"),
        (
            gh_item(
                "u", "CONFLICTING", "DIRTY", [{"conclusion": "FAILURE", "status": "COMPLETED"}]
            ),
            "C",
        ),
        (
            gh_item(
                "u", "MERGEABLE", "BLOCKED", [{"conclusion": "SUCCESS", "status": "COMPLETED"}]
            ),
            "G",
        ),
    ],
)
def test_classify(item, code):
    assert plist.classify(item) == code


def test_truncate():
    assert plist.truncate_description(None) == ""
    assert plist.truncate_description("  hi  ") == "hi"
    assert plist.truncate_description("x" * 200) == "x" * 200
    assert plist.truncate_description("x" * 250) == "x" * 200 + "..."


@pytest.mark.parametrize("make", [gl_item, gh_item])
def test_table_green_exit_zero(monkeypatch, capsys, make):
    code, out = run(monkeypatch, capsys, FakeAdapter([make("https://h/1", body="the body")]))
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["URL", "ST", "DESCRIPTION"]
    assert lines[2].split(None, 2) == ["https://h/1", "G", "the body"]


def test_bad_exits_one_and_oldest_first(monkeypatch, capsys):
    items = [
        gh_item("https://h/2", "CONFLICTING", "DIRTY", created="2026-02-01"),
        gh_item("https://h/1", created="2026-01-01"),
    ]
    code, out = run(monkeypatch, capsys, FakeAdapter(items))
    assert code == 1
    rows = out.splitlines()[2:]
    assert [r.split()[0] for r in rows] == ["https://h/1", "https://h/2"]


def test_red_exits_one(monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, FakeAdapter([gl_item("u", "failed")]))
    assert code == 1 and " R " in out


def test_empty_table_exit_zero(monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, FakeAdapter([]))
    assert code == 0
    assert len(out.splitlines()) == 2


def test_description_truncated_in_table(monkeypatch, capsys):
    _, out = run(monkeypatch, capsys, FakeAdapter([gh_item("u", body="y" * 300)]))
    assert out.splitlines()[2].endswith("y" * 200 + "...")


def test_local_only(monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, None)
    assert code == 0
    assert out.strip() == "local-only: no PR/MR host for git@example.org:a/b.git"


def test_malformed_host_map_is_usage_error_exit_2(monkeypatch, capsys):
    monkeypatch.setenv(scm.HOSTS_ENV, "nohost")
    monkeypatch.setattr(scm, "origin_url", lambda repo: "git@example.org:a/b.git")
    with pytest.raises(SystemExit) as exc:
        plist.main(["--repo", "r"])
    assert exc.value.code == 2
    assert "nohost" in capsys.readouterr().err


def test_gitlab_classification():
    assert plist.classify(gl_item("u", "success")) == "G"
    assert plist.classify(gl_item("u", "failed")) == "R"
    assert plist.classify(gl_item("u", "canceled")) == "R"
    assert plist.classify(gl_item("u", detail="conflict")) == "C"
    assert plist.classify(gl_item("u", conflicts=True)) == "C"
    assert plist.classify(gl_item("u", "failed", detail="conflict")) == "C"


def test_github_classification():
    failed = [{"conclusion": "FAILURE", "status": "COMPLETED"}]
    assert plist.classify(gh_item("u")) == "G"
    assert plist.classify(gh_item("u", "CONFLICTING", "DIRTY")) == "C"
    assert plist.classify(gh_item("u", rollup=failed)) == "R"
    assert plist.classify(gh_item("u", "CONFLICTING", "DIRTY", failed)) == "C"


def test_table_order_and_truncation(monkeypatch, capsys):
    items = [
        gl_item("https://h/2", body="b" * 250, created="2026-02-01"),
        gh_item("https://h/1", body="a" * 200, created="2026-01-01"),
    ]
    _, out = run(monkeypatch, capsys, FakeAdapter(items))
    rows = out.splitlines()[2:]
    assert [r.split()[0] for r in rows] == ["https://h/1", "https://h/2"]
    assert rows[0].endswith("a" * 200) and not rows[0].endswith("...")
    assert rows[1].endswith("b" * 200 + "...")


def test_exit_codes(monkeypatch, capsys):
    assert run(monkeypatch, capsys, FakeAdapter([gl_item("u", "success")]))[0] == 0
    assert run(monkeypatch, capsys, FakeAdapter([]))[0] == 0
    assert run(monkeypatch, capsys, FakeAdapter([gl_item("u", "failed")]))[0] == 1
    assert run(monkeypatch, capsys, FakeAdapter([gh_item("u", "CONFLICTING", "DIRTY")]))[0] == 1


def test_local_only_repo(monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, None)
    assert code == 0
    assert out.strip() == "local-only: no PR/MR host for git@example.org:a/b.git"


@pytest.mark.parametrize("make", [gl_item, gh_item])
def test_description_is_body_on_both_hosts(monkeypatch, capsys, make):
    item = make("https://h/1", body="the body")
    item["title"] = "the title"
    _, out = run(monkeypatch, capsys, FakeAdapter([item]))
    assert "the body" in out and "the title" not in out


# -- a real GitHub adapter over the REST routes (no gh pr / graphql) --

GH_REST_SHIM = fake_gh_rest.shim(
    """#!/usr/bin/env python3
import sys

sys.path.insert(0, "__FAKE_GH_DIR__")
import fake_gh_rest

PASS = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]
FAIL = [{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]
WORLD = {
    "slug": "acme/repo1",
    "prs": {
        "1": {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
              "body": "good one", "createdAt": "2026-01-01T00:00:00Z", "statusCheckRollup": PASS},
        "2": {"state": "OPEN", "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY",
              "body": "conflicted", "createdAt": "2026-02-01T00:00:00Z", "statusCheckRollup": PASS},
        "3": {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "UNSTABLE",
              "body": "red", "createdAt": "2026-03-01T00:00:00Z", "statusCheckRollup": FAIL},
    },
}
code = fake_gh_rest.serve(sys.argv[1:], WORLD) if sys.argv[1] == "api" else None
sys.exit(1 if code is None else code)
"""
)


def test_github_list_end_to_end_over_rest(monkeypatch, capsys, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(GH_REST_SHIM)
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    adapter = scm.GitHub("r", "github.com", "acme/repo1")
    code, out = run(monkeypatch, capsys, adapter)
    assert code == 1
    rows = out.splitlines()[2:]
    assert [r.split(None, 2)[1:] for r in rows] == [
        ["G", "good one"],
        ["C", "conflicted"],
        ["R", "red"],
    ]
