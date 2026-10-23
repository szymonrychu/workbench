import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
LIB_DIR = Path(__file__).resolve().parents[2] / "_lib"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(LIB_DIR))

import scm  # noqa: E402
import update  # noqa: E402

GH_SHIM = """#!/usr/bin/env python3
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["GH_STUB_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\\n")


if argv[0] != "api":
    sys.exit(1)
method = argv[argv.index("-X") + 1] if "-X" in argv else "GET"
endpoint = next(a for a in argv[1:] if a.startswith("repos/")).split("?")[0]
parts = endpoint.split("/")
slug, rest = "/".join(parts[1:3]), parts[3:]
if method == "GET" and rest == ["pulls"]:
    existing = json.loads(os.environ.get("GH_STUB_EXISTING", "{}"))
    entry = existing.get(slug)
    print(json.dumps([{"number": entry["number"], "html_url": entry["url"]}] if entry else []))
elif method == "GET" and rest == []:
    print(json.dumps({"default_branch": "main"}))
elif method == "POST" and rest == ["pulls"]:
    urls = json.loads(os.environ.get("GH_STUB_CREATE_URLS", "{}"))
    print(json.dumps({"html_url": urls[slug]}))
elif method == "GET" and rest[0] == "issues" and rest[2:] == ["labels"]:
    labels = json.loads(os.environ.get("GH_STUB_LABELS", "{}"))
    print(json.dumps([[{"name": n} for n in labels.get(rest[1], [])]]))
elif method in ("POST", "PATCH"):
    print("{}")
else:
    sys.exit(1)
"""


def field(call, name):
    prefix = name + "="
    return next(a[len(prefix) :] for a in call if a.startswith(prefix))


def api_calls(calls, method, suffix=""):
    """The recorded `gh api -X <method> repos/...` calls whose endpoint ends in `suffix`."""
    return [c for c in calls if c[:3] == ["api", "-X", method] and c[3].endswith(suffix)]


def label_creates(calls):
    """The repo-level `POST repos/<owner>/<repo>/labels` calls (not a PR's label list)."""
    return [c for c in api_calls(calls, "POST", "/labels") if c[3].count("/") == 3]


def label_reads(calls):
    return [c for c in calls if c[0] == "api" and "/issues/" in c[-1] and "/labels" in c[-1]]


def _init_repo(path, origin_url):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", origin_url], cwd=path, check=True)


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    root = tmp_path / "workbench"
    root.mkdir()
    (root / ".gitmodules").write_text(
        '[submodule "repo-ssh"]\n'
        "\tpath = code/repo-ssh\n"
        "\turl = git@github.com:acme/repo-ssh.git\n"
        "\tbranch = main\n"
        '[submodule "repo-https"]\n'
        "\tpath = code/repo-https\n"
        "\turl = https://github.com/acme/repo-https.git\n"
        "\tbranch = main\n"
    )
    code_dir = root / "code"
    code_dir.mkdir()
    _init_repo(code_dir / "repo-ssh", "git@github.com:acme/repo-ssh.git")
    _init_repo(code_dir / "repo-https", "https://github.com/acme/repo-https.git")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def gh_stub(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_path = bin_dir / "gh"
    gh_path.write_text(GH_SHIM)
    gh_path.chmod(0o755)

    log_path = tmp_path / "gh_calls.log"
    monkeypatch.setenv("GH_STUB_LOG", str(log_path))
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")

    def read_calls():
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text().splitlines() if line]

    return read_calls


def test_repo_slug_parses_ssh_url():
    assert scm.slug_from_url("git@github.com:acme/repo-ssh.git") == "acme/repo-ssh"


def test_repo_slug_parses_https_url():
    assert scm.slug_from_url("https://github.com/acme/repo-https.git") == "acme/repo-https"


def test_repo_slug_parses_https_url_without_git_suffix():
    assert scm.slug_from_url("https://github.com/acme/repo-https") == "acme/repo-https"


def test_create_and_edit_branching(workbench, gh_stub, monkeypatch, capsys):
    monkeypatch.setenv(
        "GH_STUB_EXISTING",
        json.dumps(
            {
                "acme/repo-https": {
                    "number": 7,
                    "url": "https://github.com/acme/repo-https/pull/7",
                },
            }
        ),
    )
    monkeypatch.setenv(
        "GH_STUB_CREATE_URLS",
        json.dumps(
            {
                "acme/repo-ssh": "https://github.com/acme/repo-ssh/pull/99",
            }
        ),
    )

    with pytest.raises(SystemExit) as exc:
        update.main(
            [
                "--branch",
                "feature/x",
                "--repos",
                "repo-ssh,repo-https",
                "--body",
                "repo-ssh=Add feature X\n\nDetails here.",
                "--body",
                "repo-https=Add feature X support\n\nMore details.",
            ]
        )

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "repo-ssh" in out
    assert "created" in out
    assert "https://github.com/acme/repo-ssh/pull/99" in out
    assert "repo-https" in out
    assert "updated" in out
    assert "https://github.com/acme/repo-https/pull/7" in out

    calls = gh_stub()
    create_calls = api_calls(calls, "POST", "/pulls")
    assert len(create_calls) == 1
    assert create_calls[0][3] == "repos/acme/repo-ssh/pulls"
    assert field(create_calls[0], "title") == "Add feature X"
    assert field(create_calls[0], "head") == "feature/x"
    assert field(create_calls[0], "base") == "main"

    list_calls = [c for c in calls if c[0] == "api" and "/pulls?state=open" in c[1]]
    assert len(list_calls) == 2


def test_pass_two_cross_links_siblings_and_omits_self(workbench, gh_stub, monkeypatch):
    monkeypatch.setenv("GH_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GH_STUB_CREATE_URLS",
        json.dumps(
            {
                "acme/repo-ssh": "https://github.com/acme/repo-ssh/pull/1",
                "acme/repo-https": "https://github.com/acme/repo-https/pull/2",
            }
        ),
    )

    with pytest.raises(SystemExit) as exc:
        update.main(
            [
                "--branch",
                "feature/y",
                "--repos",
                "repo-ssh,repo-https",
                "--body",
                "repo-ssh=Body for ssh repo",
                "--body",
                "repo-https=Body for https repo",
            ]
        )
    assert exc.value.code == 0

    calls = gh_stub()
    edit_calls = api_calls(calls, "PATCH")
    # pass 2 issues one edit per repo (pass 1 only edits pre-existing PRs; both here are new)
    assert len(edit_calls) == 2

    def body_for(url):
        for c in edit_calls:
            if c[3].rsplit("/", 1)[1] == url.rsplit("/", 1)[1]:
                return field(c, "body")
        raise AssertionError(f"no edit call for {url}")

    ssh_body = body_for("https://github.com/acme/repo-ssh/pull/1")
    https_body = body_for("https://github.com/acme/repo-https/pull/2")

    assert "Complementary PRs" in ssh_body
    assert "https://github.com/acme/repo-https/pull/2" in ssh_body
    assert "https://github.com/acme/repo-ssh/pull/1" not in ssh_body

    assert "Complementary PRs" in https_body
    assert "https://github.com/acme/repo-ssh/pull/1" in https_body
    assert "https://github.com/acme/repo-https/pull/2" not in https_body


def test_aggregate_report_and_exit_code_with_unknown_repo(workbench, gh_stub, monkeypatch, capsys):
    monkeypatch.setenv("GH_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GH_STUB_CREATE_URLS",
        json.dumps(
            {
                "acme/repo-ssh": "https://github.com/acme/repo-ssh/pull/5",
            }
        ),
    )

    with pytest.raises(SystemExit) as exc:
        update.main(
            [
                "--branch",
                "feature/z",
                "--repos",
                "repo-ssh,nope",
                "--body",
                "repo-ssh=Body for ssh repo",
                "--body",
                "nope=Body for missing repo",
            ]
        )

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "repo-ssh" in out
    assert "created" in out
    assert "nope" in out
    assert "failed" in out

    calls = gh_stub()
    assert not any("nope" in json.dumps(c) for c in calls)


def test_missing_body_is_reported_as_failure(workbench, gh_stub, monkeypatch, capsys):
    monkeypatch.setenv("GH_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv("GH_STUB_CREATE_URLS", json.dumps({}))

    with pytest.raises(SystemExit) as exc:
        update.main(
            [
                "--branch",
                "feature/w",
                "--repos",
                "repo-ssh",
            ]
        )

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "repo-ssh" in out
    assert "failed" in out


def test_semver_applies_when_absent(workbench, gh_stub, monkeypatch, capsys):
    monkeypatch.setenv("GH_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GH_STUB_CREATE_URLS",
        json.dumps({"acme/repo-ssh": "https://github.com/acme/repo-ssh/pull/10"}),
    )
    monkeypatch.setenv("GH_STUB_LABELS", json.dumps({}))

    with pytest.raises(SystemExit) as exc:
        update.main(
            [
                "--branch",
                "feature/semver",
                "--repos",
                "repo-ssh",
                "--body",
                "repo-ssh=Add feature",
                "--semver",
                "repo-ssh=patch",
            ]
        )
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "semver:patch applied" in out

    calls = gh_stub()
    label_create = label_creates(calls)
    assert len(label_create) == 1
    assert field(label_create[0], "name") == "semver:patch"

    add_label_calls = api_calls(calls, "POST", "/issues/10/labels")
    assert len(add_label_calls) == 1
    assert "labels[]=semver:patch" in add_label_calls[0]


def test_semver_skipped_when_already_present(workbench, gh_stub, monkeypatch, capsys):
    monkeypatch.setenv("GH_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GH_STUB_CREATE_URLS",
        json.dumps({"acme/repo-ssh": "https://github.com/acme/repo-ssh/pull/11"}),
    )
    monkeypatch.setenv(
        "GH_STUB_LABELS",
        json.dumps({"11": ["semver:major"]}),
    )

    with pytest.raises(SystemExit) as exc:
        update.main(
            [
                "--branch",
                "feature/semver",
                "--repos",
                "repo-ssh",
                "--body",
                "repo-ssh=Add feature",
                "--semver",
                "repo-ssh=patch",
            ]
        )
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "semver skipped (has semver:major)" in out

    calls = gh_stub()
    assert label_reads(calls)
    assert label_creates(calls) == []
    assert api_calls(calls, "POST", "/issues/11/labels") == []


def test_semver_invalid_level_is_per_repo_failure(workbench, gh_stub, monkeypatch, capsys):
    monkeypatch.setenv("GH_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GH_STUB_CREATE_URLS",
        json.dumps({"acme/repo-ssh": "https://github.com/acme/repo-ssh/pull/12"}),
    )
    monkeypatch.setenv("GH_STUB_LABELS", json.dumps({}))

    with pytest.raises(SystemExit) as exc:
        update.main(
            [
                "--branch",
                "feature/semver",
                "--repos",
                "repo-ssh",
                "--body",
                "repo-ssh=Add feature",
                "--semver",
                "repo-ssh=bogus",
            ]
        )
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "semver failed (invalid level: bogus)" in out
    # the PR itself still succeeded despite the semver failure
    assert "created" in out
    assert "https://github.com/acme/repo-ssh/pull/12" in out

    calls = gh_stub()
    assert label_creates(calls) == []
    assert label_reads(calls) == []


def test_semver_missing_is_noop(workbench, gh_stub, monkeypatch, capsys):
    monkeypatch.setenv("GH_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GH_STUB_CREATE_URLS",
        json.dumps({"acme/repo-ssh": "https://github.com/acme/repo-ssh/pull/13"}),
    )

    with pytest.raises(SystemExit) as exc:
        update.main(
            [
                "--branch",
                "feature/semver",
                "--repos",
                "repo-ssh",
                "--body",
                "repo-ssh=Add feature",
            ]
        )
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "| -" in out

    calls = gh_stub()
    assert label_reads(calls) == []
    assert label_creates(calls) == []


def test_semver_creates_label_if_missing(workbench, gh_stub, monkeypatch):
    monkeypatch.setenv("GH_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GH_STUB_CREATE_URLS",
        json.dumps({"acme/repo-ssh": "https://github.com/acme/repo-ssh/pull/14"}),
    )
    monkeypatch.setenv("GH_STUB_LABELS", json.dumps({}))

    with pytest.raises(SystemExit) as exc:
        update.main(
            [
                "--branch",
                "feature/semver",
                "--repos",
                "repo-ssh",
                "--body",
                "repo-ssh=Add feature",
                "--semver",
                "repo-ssh=major",
            ]
        )
    assert exc.value.code == 0

    calls = gh_stub()
    label_create = label_creates(calls)
    assert len(label_create) == 1
    call = label_create[0]
    assert call[3] == "repos/acme/repo-ssh/labels"
    assert field(call, "name") == "semver:major"
    assert field(call, "color") == "d93f0b"


def test_semver_failure_does_not_abort_other_repos(workbench, gh_stub, monkeypatch, capsys):
    monkeypatch.setenv("GH_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GH_STUB_CREATE_URLS",
        json.dumps(
            {
                "acme/repo-ssh": "https://github.com/acme/repo-ssh/pull/20",
                "acme/repo-https": "https://github.com/acme/repo-https/pull/21",
            }
        ),
    )
    monkeypatch.setenv("GH_STUB_LABELS", json.dumps({}))

    with pytest.raises(SystemExit) as exc:
        update.main(
            [
                "--branch",
                "feature/semver",
                "--repos",
                "repo-ssh,repo-https",
                "--body",
                "repo-ssh=Body ssh",
                "--body",
                "repo-https=Body https",
                "--semver",
                "repo-ssh=bogus",
                "--semver",
                "repo-https=minor",
            ]
        )
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "semver failed (invalid level: bogus)" in out
    assert "semver:minor applied" in out

    calls = gh_stub()
    label_create = label_creates(calls)
    assert len(label_create) == 1
    assert field(label_create[0], "name") == "semver:minor"


def test_mixed_hosts_one_run(mixed_workbench, mixed_stubs, run_mixed, capsys):
    code = run_mixed(
        "--branch",
        "feature/m",
        "--repos",
        "gh-repo,gl-repo",
        "--body",
        "gh-repo=GH body",
        "--body",
        "gl-repo=GL body",
        "--semver",
        "gh-repo=minor",
        "--semver",
        "gl-repo=patch",
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "https://github.com/acme/gh-repo/pull/5 | semver:minor applied" in out
    assert "https://gitlab.com/acme/gl-repo/-/merge_requests/8 | semver:patch applied" in out

    calls = mixed_stubs()
    gh_edit = [
        c
        for c in calls
        if c[:4] == ["gh", "api", "-X", "PATCH"] and any(a.startswith("body=") for a in c)
    ]
    gl_put = [
        c
        for c in calls
        if c[0] == "glab" and "PUT" in c and any(a.startswith("description=") for a in c)
    ]
    gh_body = [a for a in gh_edit[0] if a.startswith("body=")][0][len("body=") :]
    gl_body = [a for a in gl_put[0] if a.startswith("description=")][0]
    assert "## Complementary PRs" in gh_body
    assert "https://gitlab.com/acme/gl-repo/-/merge_requests/8" in gh_body
    assert "## Complementary MRs" in gl_body
    assert "https://github.com/acme/gh-repo/pull/5" in gl_body
