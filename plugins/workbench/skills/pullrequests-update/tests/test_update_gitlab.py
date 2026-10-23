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

# Fake `glab api` that dispatches on --method + endpoint path, parsing -f
# key=val pairs and query strings, mimicking GitLab's REST responses closely
# enough to drive update.py's logic.
GLAB_SHIM = """#!/usr/bin/env python3
import json
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

argv = sys.argv[1:]
with open(os.environ["GLAB_STUB_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\\n")

if argv[0] != "api":
    sys.exit(1)

argv = argv[1:]


def flag(name):
    if name in argv:
        return argv[argv.index(name) + 1]
    return None


method = flag("--method") or "GET"
endpoint = argv[0] if argv and not argv[0].startswith("--") else None
for a in argv:
    if not a.startswith("--") and a != method:
        endpoint = a
        break

fields = {}
i = 0
while i < len(argv):
    if argv[i] == "-f":
        k, _, v = argv[i + 1].partition("=")
        fields[k] = v
        i += 2
    else:
        i += 1

parsed = urlparse(endpoint)
path = parsed.path
query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

m = re.match(r"^projects/([^/]+)/merge_requests$", path)
m_iid = re.match(r"^projects/([^/]+)/merge_requests/(\\d+)$", path)
m_labels = re.match(r"^projects/([^/]+)/labels$", path)

if method == "GET" and m:
    project = m.group(1)
    source_branch = query.get("source_branch")
    existing = json.loads(os.environ.get("GLAB_STUB_EXISTING", "{}"))
    key = f"{project}:{source_branch}"
    entry = existing.get(key)
    print(json.dumps([entry] if entry else []))
elif method == "POST" and m:
    project = m.group(1)
    create_urls = json.loads(os.environ.get("GLAB_STUB_CREATE_URLS", "{}"))
    create_iids = json.loads(os.environ.get("GLAB_STUB_CREATE_IIDS", "{}"))
    print(
        json.dumps(
            {
                "web_url": create_urls[project],
                "iid": create_iids.get(project, 1),
            }
        )
    )
elif method == "PUT" and m_iid:
    project, iid = m_iid.group(1), m_iid.group(2)
    print(json.dumps({"iid": int(iid)}))
elif method == "GET" and m_iid:
    project, iid = m_iid.group(1), m_iid.group(2)
    labels = json.loads(os.environ.get("GLAB_STUB_LABELS", "{}"))
    print(json.dumps({"iid": int(iid), "labels": labels.get(f"{project}:{iid}", [])}))
elif method == "POST" and m_labels:
    fail_conflict = os.environ.get("GLAB_STUB_LABEL_CONFLICT", "")
    if fields.get("name") in fail_conflict.split(","):
        sys.stderr.write("POST https://gitlab.example/api/v4/projects/x/labels: 409 Conflict\\n")
        sys.exit(1)
    print(json.dumps({"name": fields.get("name")}))
else:
    sys.exit(1)
"""


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
        "\turl = git@gitlab.com:acme/repo-ssh.git\n"
        "\tbranch = main\n"
        '[submodule "repo-https"]\n'
        "\tpath = code/repo-https\n"
        "\turl = https://gitlab.com/acme/repo-https.git\n"
        "\tbranch = main\n"
    )
    code_dir = root / "code"
    code_dir.mkdir()
    _init_repo(code_dir / "repo-ssh", "git@gitlab.com:acme/repo-ssh.git")
    _init_repo(code_dir / "repo-https", "https://gitlab.com/acme/repo-https.git")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def glab_stub(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    glab_path = bin_dir / "glab"
    glab_path.write_text(GLAB_SHIM)
    glab_path.chmod(0o755)

    log_path = tmp_path / "glab_calls.log"
    monkeypatch.setenv("GLAB_STUB_LOG", str(log_path))
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")

    def read_calls():
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text().splitlines() if line]

    return read_calls


def _endpoint(call):
    """Extract the api endpoint arg from a logged glab call, regardless of
    how many -f pairs follow it."""
    if "--method" in call:
        return call[call.index("--method") + 2]
    return call[1]


def test_repo_slug_parses_ssh_url():
    assert scm.slug_from_url("git@gitlab.com:acme/repo-ssh.git") == "acme/repo-ssh"


def test_repo_slug_parses_https_url():
    assert scm.slug_from_url("https://gitlab.com/acme/repo-https.git") == "acme/repo-https"


def test_repo_slug_parses_https_url_without_git_suffix():
    assert scm.slug_from_url("https://gitlab.com/acme/repo-https") == "acme/repo-https"


def test_repo_slug_parses_nested_group_url():
    assert scm.slug_from_url("git@gitlab.com:acme/sub/repo-nested.git") == "acme/sub/repo-nested"


def test_encode_project_path():
    assert scm.GitLab("r", "gitlab.com", "acme/repo-ssh").project == "acme%2Frepo-ssh"
    assert scm.GitLab("r", "gitlab.com", "acme/sub/repo").project == "acme%2Fsub%2Frepo"


def test_create_and_edit_branching(workbench, glab_stub, monkeypatch, capsys):
    monkeypatch.setenv(
        "GLAB_STUB_EXISTING",
        json.dumps(
            {
                "acme%2Frepo-https:feature/x": {
                    "iid": 7,
                    "web_url": "https://gitlab.com/acme/repo-https/-/merge_requests/7",
                    "title": "old title",
                    "labels": [],
                },
            }
        ),
    )
    monkeypatch.setenv(
        "GLAB_STUB_CREATE_URLS",
        json.dumps(
            {
                "acme%2Frepo-ssh": "https://gitlab.com/acme/repo-ssh/-/merge_requests/99",
            }
        ),
    )
    monkeypatch.setenv("GLAB_STUB_CREATE_IIDS", json.dumps({"acme%2Frepo-ssh": 99}))

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
    assert "https://gitlab.com/acme/repo-ssh/-/merge_requests/99" in out
    assert "repo-https" in out
    assert "updated" in out
    assert "https://gitlab.com/acme/repo-https/-/merge_requests/7" in out

    calls = glab_stub()
    create_calls = [
        c
        for c in calls
        if "--method" in c
        and c[c.index("--method") + 1] == "POST"
        and "merge_requests" in _endpoint(c)
    ]
    assert len(create_calls) == 1
    call = create_calls[0]
    assert "acme%2Frepo-ssh/merge_requests" in _endpoint(call)
    assert "-f" in call
    title_field = [
        call[i + 1]
        for i in range(len(call))
        if call[i] == "-f" and call[i + 1].startswith("title=")
    ]
    assert title_field == ["title=Add feature X"]

    list_calls = [
        c for c in calls if "merge_requests?" in _endpoint(c) and "source_branch" in _endpoint(c)
    ]
    assert len(list_calls) == 2


def test_pass_two_cross_links_siblings_and_omits_self(workbench, glab_stub, monkeypatch):
    monkeypatch.setenv("GLAB_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GLAB_STUB_CREATE_URLS",
        json.dumps(
            {
                "acme%2Frepo-ssh": "https://gitlab.com/acme/repo-ssh/-/merge_requests/1",
                "acme%2Frepo-https": "https://gitlab.com/acme/repo-https/-/merge_requests/2",
            }
        ),
    )
    monkeypatch.setenv(
        "GLAB_STUB_CREATE_IIDS",
        json.dumps({"acme%2Frepo-ssh": 1, "acme%2Frepo-https": 2}),
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

    calls = glab_stub()
    put_calls = [
        c
        for c in calls
        if "--method" in c
        and c[c.index("--method") + 1] == "PUT"
        and "merge_requests/" in _endpoint(c)
    ]
    # pass 2 issues one PUT per repo (pass 1 only edits pre-existing MRs; both here are new)
    assert len(put_calls) == 2

    def body_for(endpoint_suffix):
        for c in put_calls:
            if _endpoint(c).endswith(endpoint_suffix):
                idx = [
                    i for i in range(len(c)) if c[i] == "-f" and c[i + 1].startswith("description=")
                ][0]
                return c[idx + 1][len("description=") :]
        raise AssertionError(f"no PUT call for {endpoint_suffix}")

    ssh_body = body_for("merge_requests/1")
    https_body = body_for("merge_requests/2")

    assert "Complementary MRs" in ssh_body
    assert "https://gitlab.com/acme/repo-https/-/merge_requests/2" in ssh_body
    assert "https://gitlab.com/acme/repo-ssh/-/merge_requests/1" not in ssh_body

    assert "Complementary MRs" in https_body
    assert "https://gitlab.com/acme/repo-ssh/-/merge_requests/1" in https_body
    assert "https://gitlab.com/acme/repo-https/-/merge_requests/2" not in https_body


def test_aggregate_report_and_exit_code_with_unknown_repo(
    workbench, glab_stub, monkeypatch, capsys
):
    monkeypatch.setenv("GLAB_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GLAB_STUB_CREATE_URLS",
        json.dumps(
            {
                "acme%2Frepo-ssh": "https://gitlab.com/acme/repo-ssh/-/merge_requests/5",
            }
        ),
    )
    monkeypatch.setenv("GLAB_STUB_CREATE_IIDS", json.dumps({"acme%2Frepo-ssh": 5}))

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

    calls = glab_stub()
    assert not any("nope" in json.dumps(c) for c in calls)


def test_missing_body_is_reported_as_failure(workbench, glab_stub, monkeypatch, capsys):
    monkeypatch.setenv("GLAB_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv("GLAB_STUB_CREATE_URLS", json.dumps({}))

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


def test_semver_applies_when_absent(workbench, glab_stub, monkeypatch, capsys):
    monkeypatch.setenv("GLAB_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GLAB_STUB_CREATE_URLS",
        json.dumps({"acme%2Frepo-ssh": "https://gitlab.com/acme/repo-ssh/-/merge_requests/10"}),
    )
    monkeypatch.setenv("GLAB_STUB_CREATE_IIDS", json.dumps({"acme%2Frepo-ssh": 10}))
    monkeypatch.setenv("GLAB_STUB_LABELS", json.dumps({}))

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

    calls = glab_stub()
    label_create = [
        c
        for c in calls
        if "--method" in c
        and c[c.index("--method") + 1] == "POST"
        and _endpoint(c).endswith("labels")
    ]
    assert len(label_create) == 1
    name_field = [
        label_create[0][i + 1]
        for i in range(len(label_create[0]))
        if label_create[0][i] == "-f" and label_create[0][i + 1].startswith("name=")
    ]
    assert name_field == ["name=semver:patch"]

    add_label_calls = [
        c
        for c in calls
        if "--method" in c
        and c[c.index("--method") + 1] == "PUT"
        and "merge_requests/" in _endpoint(c)
        and any(a.startswith("add_labels=") for a in c)
    ]
    assert len(add_label_calls) == 1
    assert "add_labels=semver:patch" in add_label_calls[0]


def test_semver_skipped_when_already_present(workbench, glab_stub, monkeypatch, capsys):
    monkeypatch.setenv("GLAB_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GLAB_STUB_CREATE_URLS",
        json.dumps({"acme%2Frepo-ssh": "https://gitlab.com/acme/repo-ssh/-/merge_requests/11"}),
    )
    monkeypatch.setenv("GLAB_STUB_CREATE_IIDS", json.dumps({"acme%2Frepo-ssh": 11}))
    monkeypatch.setenv(
        "GLAB_STUB_LABELS",
        json.dumps({"acme%2Frepo-ssh:11": ["semver:major"]}),
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

    calls = glab_stub()
    assert not any(
        "--method" in c and c[c.index("--method") + 1] == "POST" and _endpoint(c).endswith("labels")
        for c in calls
    )
    assert not any(
        "--method" in c
        and c[c.index("--method") + 1] == "PUT"
        and any(a.startswith("add_labels=") for a in c)
        for c in calls
    )


def test_semver_invalid_level_is_per_repo_failure(workbench, glab_stub, monkeypatch, capsys):
    monkeypatch.setenv("GLAB_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GLAB_STUB_CREATE_URLS",
        json.dumps({"acme%2Frepo-ssh": "https://gitlab.com/acme/repo-ssh/-/merge_requests/12"}),
    )
    monkeypatch.setenv("GLAB_STUB_CREATE_IIDS", json.dumps({"acme%2Frepo-ssh": 12}))
    monkeypatch.setenv("GLAB_STUB_LABELS", json.dumps({}))

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
    # the MR itself still succeeded despite the semver failure
    assert "created" in out
    assert "https://gitlab.com/acme/repo-ssh/-/merge_requests/12" in out

    calls = glab_stub()
    assert not any(
        "--method" in c and c[c.index("--method") + 1] == "POST" and _endpoint(c).endswith("labels")
        for c in calls
    )
    assert not any(
        "--method" in c
        and c[c.index("--method") + 1] == "GET"
        and "merge_requests/12" in _endpoint(c)
        for c in calls
    )


def test_semver_missing_is_noop(workbench, glab_stub, monkeypatch, capsys):
    monkeypatch.setenv("GLAB_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GLAB_STUB_CREATE_URLS",
        json.dumps({"acme%2Frepo-ssh": "https://gitlab.com/acme/repo-ssh/-/merge_requests/13"}),
    )
    monkeypatch.setenv("GLAB_STUB_CREATE_IIDS", json.dumps({"acme%2Frepo-ssh": 13}))

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

    calls = glab_stub()
    assert not any(
        "--method" in c
        and c[c.index("--method") + 1] == "GET"
        and "merge_requests/13" in _endpoint(c)
        for c in calls
    )
    assert not any(
        "--method" in c and c[c.index("--method") + 1] == "POST" and _endpoint(c).endswith("labels")
        for c in calls
    )


def test_semver_creates_label_if_missing(workbench, glab_stub, monkeypatch):
    monkeypatch.setenv("GLAB_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GLAB_STUB_CREATE_URLS",
        json.dumps({"acme%2Frepo-ssh": "https://gitlab.com/acme/repo-ssh/-/merge_requests/14"}),
    )
    monkeypatch.setenv("GLAB_STUB_CREATE_IIDS", json.dumps({"acme%2Frepo-ssh": 14}))
    monkeypatch.setenv("GLAB_STUB_LABELS", json.dumps({}))

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

    calls = glab_stub()
    label_create = [
        c
        for c in calls
        if "--method" in c
        and c[c.index("--method") + 1] == "POST"
        and _endpoint(c).endswith("labels")
    ]
    assert len(label_create) == 1
    call = label_create[0]
    assert "acme%2Frepo-ssh/labels" in _endpoint(call)
    name_field = [
        call[i + 1] for i in range(len(call)) if call[i] == "-f" and call[i + 1].startswith("name=")
    ]
    assert name_field == ["name=semver:major"]
    color_field = [
        call[i + 1]
        for i in range(len(call))
        if call[i] == "-f" and call[i + 1].startswith("color=")
    ]
    assert color_field == ["color=#d93f0b"]


def test_semver_label_conflict_is_not_a_failure(workbench, glab_stub, monkeypatch):
    monkeypatch.setenv("GLAB_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GLAB_STUB_CREATE_URLS",
        json.dumps({"acme%2Frepo-ssh": "https://gitlab.com/acme/repo-ssh/-/merge_requests/15"}),
    )
    monkeypatch.setenv("GLAB_STUB_CREATE_IIDS", json.dumps({"acme%2Frepo-ssh": 15}))
    monkeypatch.setenv("GLAB_STUB_LABELS", json.dumps({}))
    monkeypatch.setenv("GLAB_STUB_LABEL_CONFLICT", "semver:patch")

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

    calls = glab_stub()
    add_label_calls = [
        c
        for c in calls
        if "--method" in c
        and c[c.index("--method") + 1] == "PUT"
        and any(a.startswith("add_labels=") for a in c)
    ]
    assert len(add_label_calls) == 1


def test_semver_failure_does_not_abort_other_repos(workbench, glab_stub, monkeypatch, capsys):
    monkeypatch.setenv("GLAB_STUB_EXISTING", json.dumps({}))
    monkeypatch.setenv(
        "GLAB_STUB_CREATE_URLS",
        json.dumps(
            {
                "acme%2Frepo-ssh": "https://gitlab.com/acme/repo-ssh/-/merge_requests/20",
                "acme%2Frepo-https": "https://gitlab.com/acme/repo-https/-/merge_requests/21",
            }
        ),
    )
    monkeypatch.setenv(
        "GLAB_STUB_CREATE_IIDS",
        json.dumps({"acme%2Frepo-ssh": 20, "acme%2Frepo-https": 21}),
    )
    monkeypatch.setenv("GLAB_STUB_LABELS", json.dumps({}))

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

    calls = glab_stub()
    label_create = [
        c
        for c in calls
        if "--method" in c
        and c[c.index("--method") + 1] == "POST"
        and _endpoint(c).endswith("labels")
    ]
    assert len(label_create) == 1
    name_field = [
        label_create[0][i + 1]
        for i in range(len(label_create[0]))
        if label_create[0][i] == "-f" and label_create[0][i + 1].startswith("name=")
    ]
    assert name_field == ["name=semver:minor"]


def test_local_only_repo_skipped(mixed_workbench, mixed_stubs, run_mixed, capsys):
    code = run_mixed(
        "--branch",
        "feature/l",
        "--repos",
        "gh-repo,plain-repo",
        "--body",
        "gh-repo=GH body",
        "--body",
        "plain-repo=Plain body",
    )
    assert code == 0
    out = capsys.readouterr().out
    row = [line for line in out.splitlines() if line.startswith("plain-repo")][0]
    assert "skipped" in row
    assert "local-only: no PR/MR host for git@git.internal.example:acme/plain-repo.git" in row
    gh_edit = [
        c
        for c in mixed_stubs()
        if c[:4] == ["gh", "api", "-X", "PATCH"] and any(a.startswith("body=") for a in c)
    ]
    assert "Complementary" not in [a for a in gh_edit[0] if a.startswith("body=")][0]


def test_host_map_routes_unknown_hostname(
    mixed_workbench, mixed_stubs, run_mixed, monkeypatch, capsys
):
    monkeypatch.setenv("WORKBENCH_SCM_HOSTS", "git.internal.example=gitlab")
    code = run_mixed("--branch", "feature/h", "--repos", "plain-repo", "--body", "plain-repo=Body")
    out = capsys.readouterr().out
    assert "skipped" not in out
    assert any(c[0] == "glab" for c in mixed_stubs())
    assert code in (0, 1)


def test_bad_host_map_exits_2(mixed_workbench, mixed_stubs, run_mixed, monkeypatch, capsys):
    monkeypatch.setenv("WORKBENCH_SCM_HOSTS", "good.example=github,bad-entry")
    code = run_mixed("--branch", "b", "--repos", "gh-repo", "--body", "gh-repo=x")
    assert code == 2
    assert "bad-entry" in capsys.readouterr().err
    assert mixed_stubs() == []
