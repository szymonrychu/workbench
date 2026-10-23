import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common  # noqa: E402
import scm  # noqa: E402

# One shim serves as both gh and glab. It records argv, the host env vars it
# saw, the names of the *_TOKEN vars it saw and its cwd, then answers from the
# first rule in $SHIM_RULES whose "match" tokens all appear in argv (default:
# exit 0, no output).
SHIM = """#!/usr/bin/env python3
import json
import os
import sys

name = os.path.basename(sys.argv[0])
argv = sys.argv[1:]
env = {k: os.environ[k] for k in ("GH_HOST", "GH_REPO", "GITLAB_HOST", "GL_HOST")
       if k in os.environ}
tokens = sorted(k for k in os.environ if k.endswith("_TOKEN"))
with open(os.environ["SHIM_LOG"], "a") as f:
    f.write(json.dumps(
        {"cli": name, "argv": argv, "env": env, "tokens": tokens, "cwd": os.getcwd()}
    ) + "\\n")
rules = json.load(open(os.environ["SHIM_RULES"])) if os.environ.get("SHIM_RULES") else []
for rule in rules:
    if rule.get("cli", name) == name and all(t in argv for t in rule.get("match", [])):
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(rule.get("exit", 0))
"""

GITMODULES = """[submodule "app"]
\tpath = code/app
\turl = https://github.com/acme/app.git
[submodule "tool"]
\tpath = code/tool
\turl = ssh://git@ghe.corp:2222/acme/tool.git
[submodule "proj"]
\tpath = code/proj
\turl = git@gitlab.com:grp/sub/proj.git
\tbranch = develop
[submodule "svc"]
\tpath = code/svc
\turl = https://git.corp.example/team/svc.git
[submodule "local"]
\tpath = code/local
\turl = /srv/git/local.git
[submodule "other"]
\tpath = code/other
\turl = https://bitbucket.org/a/b.git
"""

ORIGINS = {
    "app": "https://github.com/acme/app.git",
    "tool": "ssh://git@ghe.corp:2222/acme/tool.git",
    "proj": "git@gitlab.com:grp/sub/proj.git",
    "svc": "https://git.corp.example/team/svc.git",
    "local": None,
    "other": "https://bitbucket.org/a/b.git",
}

HOSTS_ENV = "ghe.corp=github, git.corp.example=gitlab"


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    root = tmp_path / "workbench"
    root.mkdir()
    (root / ".gitmodules").write_text(GITMODULES)
    for name, origin in ORIGINS.items():
        path = root / "code" / name
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        if origin:
            subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)
    monkeypatch.chdir(root / "code" / "app")
    monkeypatch.setenv("WORKBENCH_SCM_HOSTS", HOSTS_ENV)
    return root


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
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    # Inherited host pins that must never reach the CLI.
    monkeypatch.setenv("GH_HOST", "evil.example")
    monkeypatch.setenv("GITLAB_HOST", "evil.example")
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)

    class Shim:
        def respond(self, *new_rules):
            rules.write_text(json.dumps(list(new_rules)))

        def calls(self):
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text().splitlines()]

        def last(self):
            return self.calls()[-1]

    s = Shim()
    s.respond()
    return s


# --- host_map ---------------------------------------------------------------


def test_host_map_unset_is_empty():
    assert scm.host_map({}) == {}


def test_host_map_empty_and_blank_are_empty():
    assert scm.host_map({"WORKBENCH_SCM_HOSTS": ""}) == {}
    assert scm.host_map({"WORKBENCH_SCM_HOSTS": "  "}) == {}


def test_host_map_parses_entries_with_whitespace():
    env = {"WORKBENCH_SCM_HOSTS": " Git.Corp.Example = gitlab ,ghe.example=github,"}
    assert scm.host_map(env) == {"git.corp.example": "gitlab", "ghe.example": "github"}


def test_host_map_defaults_to_process_env(monkeypatch):
    monkeypatch.setenv("WORKBENCH_SCM_HOSTS", "a.example=github")
    assert scm.host_map() == {"a.example": "github"}


@pytest.mark.parametrize("entry", ["nohost", "=gitlab", "  =github", "h.example=bitbucket", "h="])
def test_host_map_malformed_entry_raises_naming_it(entry):
    with pytest.raises(scm.HostMapError) as exc:
        scm.host_map({"WORKBENCH_SCM_HOSTS": f"ok.example=github,{entry}"})
    assert entry.strip() in str(exc.value)
    assert "WORKBENCH_SCM_HOSTS" in str(exc.value)


# --- origin_host --------------------------------------------------------------


@pytest.mark.parametrize(
    "url,host",
    [
        ("https://github.com/acme/app.git", "github.com"),
        ("https://user:tok@GitHub.com:8443/acme/app", "github.com"),
        ("http://git.corp.example/team/svc", "git.corp.example"),
        ("ssh://git@ghe.corp:2222/acme/tool.git", "ghe.corp"),
        ("git@gitlab.com:grp/sub/proj.git", "gitlab.com"),
        ("gitlab.example:grp/proj.git", "gitlab.example"),
        ("  git@gitlab.com:grp/proj.git\n", "gitlab.com"),
    ],
)
def test_origin_host_parses_forms(url, host):
    assert scm.origin_host(url) == host


@pytest.mark.parametrize("url", ["", "   ", None, "/srv/git/local.git", "file:///srv/x.git"])
def test_origin_host_none_without_host(url):
    assert scm.origin_host(url) is None


# --- detect_host --------------------------------------------------------------


def test_detect_host_map_first():
    env = {"WORKBENCH_SCM_HOSTS": "github.corp.example=gitlab"}
    assert scm.detect_host("https://github.corp.example/a/b.git", env) == "gitlab"


def test_detect_host_map_case_insensitive_exact():
    env = {"WORKBENCH_SCM_HOSTS": "GIT.Corp.Example=gitlab,ghe.corp=github"}
    assert scm.detect_host("git@git.corp.EXAMPLE:team/svc.git", env) == "gitlab"
    assert scm.detect_host("ssh://git@ghe.corp:22/a/b", env) == "github"
    # exact hostname only: a superstring host does not match the entry
    assert scm.detect_host("https://x.ghe.corp/a/b", env) is None


def test_detect_host_substring_rule():
    assert scm.detect_host("https://github.com/a/b.git", {}) == "github"
    assert scm.detect_host("git@github.acme.io:a/b.git", {}) == "github"
    assert scm.detect_host("git@gitlab.com:g/s/p.git", {}) == "gitlab"
    assert scm.detect_host("https://gitlab.internal/g/p", {}) == "gitlab"


def test_detect_host_unknown_or_empty_is_none():
    assert scm.detect_host("https://bitbucket.org/a/b.git", {}) is None
    assert scm.detect_host("", {}) is None
    assert scm.detect_host(None, {}) is None
    assert scm.detect_host("/srv/git/x.git", {}) is None


def test_detect_host_malformed_map_raises():
    with pytest.raises(scm.HostMapError):
        scm.detect_host("https://github.com/a/b", {"WORKBENCH_SCM_HOSTS": "broken"})


def test_detect_host_defaults_to_process_env(monkeypatch):
    monkeypatch.setenv("WORKBENCH_SCM_HOSTS", "code.example=gitlab")
    assert scm.detect_host("https://code.example/g/p.git") == "gitlab"


# --- slug_from_url ------------------------------------------------------------


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://github.com/acme/app.git", "acme/app"),
        ("https://github.com/acme/app", "acme/app"),
        ("https://github.com/acme/app/", "acme/app"),
        ("https://github.com/acme/app.git/", "acme/app"),
        ("git@github.com:acme/app.git", "acme/app"),
        ("git@gitlab.com:grp/sub/proj.git", "grp/sub/proj"),
        ("ssh://git@ghe.corp:2222/acme/tool.git", "acme/tool"),
        ("https://git.corp.example/team/a/b/svc.git\n", "team/a/b/svc"),
        ("https://user:tok@git.corp.example:8443/team/svc", "team/svc"),
    ],
)
def test_slug_from_url_any_host(url, slug):
    assert scm.slug_from_url(url) == slug


@pytest.mark.parametrize(
    "url", ["", "not a url", "https://github.com/", "https://github.com/onlyowner", "/srv/x.git"]
)
def test_slug_from_url_unparsable_raises_repo_error(url):
    with pytest.raises(common.RepoError):
        scm.slug_from_url(url)


def test_number_from_url():
    assert scm.number_from_url("https://github.com/a/b/pull/12") == 12
    assert scm.number_from_url("https://gitlab.com/g/p/-/merge_requests/7\n") == 7
    with pytest.raises(common.RepoError):
        scm.number_from_url("https://github.com/a/b")


# --- adapter_for --------------------------------------------------------------


def test_adapter_for_github_com(workbench):
    a = scm.adapter_for("app")
    assert isinstance(a, scm.GitHub)
    assert (a.host, a.hostname, a.slug, a.noun) == ("github", "github.com", "acme/app", "PR")
    assert a.ref(4) == "pr #4"


def test_adapter_for_self_hosted_github_via_map(workbench):
    a = scm.adapter_for("tool")
    assert isinstance(a, scm.GitHub)
    assert (a.hostname, a.slug) == ("ghe.corp", "acme/tool")


def test_adapter_for_gitlab_com_nested(workbench):
    a = scm.adapter_for("proj")
    assert isinstance(a, scm.GitLab)
    assert (a.host, a.hostname, a.slug, a.noun) == ("gitlab", "gitlab.com", "grp/sub/proj", "MR")
    assert a.ref(9) == "mr !9"


def test_adapter_for_self_hosted_gitlab_via_map(workbench):
    a = scm.adapter_for("svc")
    assert isinstance(a, scm.GitLab)
    assert (a.hostname, a.slug) == ("git.corp.example", "team/svc")


def test_adapter_for_local_only_and_unknown_host(workbench):
    assert scm.adapter_for("local") is None
    assert scm.adapter_for("other") is None


def test_adapter_for_without_map_falls_back_to_substring(workbench, monkeypatch):
    monkeypatch.delenv("WORKBENCH_SCM_HOSTS")
    assert scm.adapter_for("tool") is None
    assert scm.adapter_for("svc") is None
    assert isinstance(scm.adapter_for("app"), scm.GitHub)


def test_adapter_for_unknown_repo_raises(workbench):
    with pytest.raises(common.RepoError):
        scm.adapter_for("nope")


def test_adapter_for_malformed_map_raises(workbench, monkeypatch):
    monkeypatch.setenv("WORKBENCH_SCM_HOSTS", "bad-entry")
    with pytest.raises(scm.HostMapError):
        scm.adapter_for("app")


def test_origin_url(workbench):
    assert scm.origin_url("proj") == "git@gitlab.com:grp/sub/proj.git"
    assert scm.origin_url("local") == ""


# --- run / json: targeting and env --------------------------------------------


def test_gh_run_targets_github_com_with_short_slug(workbench, shim):
    scm.adapter_for("app").run(["pr", "list"])
    call = shim.last()
    assert call["cli"] == "gh"
    assert call["argv"] == ["pr", "list", "-R", "acme/app"]
    assert "GH_HOST" not in call["env"]
    assert "GITLAB_HOST" not in call["env"]
    assert Path(call["cwd"]).resolve() != (workbench / "code" / "app").resolve()


def test_gh_run_targets_self_hosted_with_host_prefix(workbench, shim):
    scm.adapter_for("tool").run(["pr", "view", "3", "--json", "labels"])
    call = shim.last()
    assert call["argv"] == ["pr", "view", "3", "--json", "labels", "-R", "ghe.corp/acme/tool"]
    assert "GH_HOST" not in call["env"]


def test_gh_run_keeps_double_dash_args_after_target(workbench, shim):
    scm.adapter_for("app").run(["pr", "create", "--title", "t", "--", "x"])
    assert shim.last()["argv"] == ["pr", "create", "--title", "t", "-R", "acme/app", "--", "x"]


def test_gh_api_github_com_has_no_repo_or_hostname(workbench, shim):
    scm.adapter_for("app").run(["api", "repos/acme/app/pulls/1/comments"])
    call = shim.last()
    assert call["argv"] == ["api", "repos/acme/app/pulls/1/comments"]
    assert "GH_HOST" not in call["env"]


def test_gh_api_self_hosted_passes_hostname(workbench, shim):
    scm.adapter_for("tool").run(["api", "graphql", "-f", "query=q"])
    call = shim.last()
    assert call["argv"] == ["api", "graphql", "-f", "query=q", "--hostname", "ghe.corp"]
    assert "-R" not in call["argv"]
    assert "GH_HOST" not in call["env"]


def test_gh_repo_subcommand_takes_target_positionally(workbench, shim):
    scm.adapter_for("tool").run(["repo", "view", "--json", "defaultBranchRef"])
    assert shim.last()["argv"] == [
        "repo",
        "view",
        "ghe.corp/acme/tool",
        "--json",
        "defaultBranchRef",
    ]


def test_gh_env_drops_inherited_gh_repo(workbench, shim, monkeypatch):
    monkeypatch.setenv("GH_REPO", "evil/repo")
    scm.adapter_for("app").run(["pr", "list"])
    assert "GH_REPO" not in shim.last()["env"]


def test_glab_api_gitlab_com_uses_encoded_project_and_no_host(workbench, shim):
    a = scm.adapter_for("proj")
    assert a.project == "grp%2Fsub%2Fproj"
    a.run(["api", f"projects/{a.project}/merge_requests/3"])
    call = shim.last()
    assert call["cli"] == "glab"
    assert call["argv"] == ["api", "projects/grp%2Fsub%2Fproj/merge_requests/3"]
    assert "GITLAB_HOST" not in call["env"]
    assert "GL_HOST" not in call["env"]
    assert "GH_HOST" not in call["env"]
    assert Path(call["cwd"]).resolve() != (workbench / "code" / "app").resolve()


def test_glab_non_api_passes_repo_slug(workbench, shim):
    scm.adapter_for("proj").run(["mr", "view", "3"])
    call = shim.last()
    assert call["argv"] == ["mr", "view", "3", "-R", "grp/sub/proj"]
    assert "GITLAB_HOST" not in call["env"]


def test_glab_self_hosted_sets_gitlab_host(workbench, shim):
    a = scm.adapter_for("svc")
    a.run(["api", f"projects/{a.project}"])
    call = shim.last()
    assert call["argv"] == ["api", "projects/team%2Fsvc"]
    assert call["env"]["GITLAB_HOST"] == "git.corp.example"
    a.run(["mr", "list"])
    call = shim.last()
    assert call["argv"] == ["mr", "list", "-R", "team/svc"]
    assert call["env"]["GITLAB_HOST"] == "git.corp.example"


def test_run_failure_raises_repo_error_with_stderr(workbench, shim):
    shim.respond({"match": ["pr"], "stderr": "HTTP 404: Not Found", "exit": 1})
    with pytest.raises(common.RepoError) as exc:
        scm.adapter_for("app").run(["pr", "view", "9"])
    assert "HTTP 404: Not Found" in str(exc.value)
    assert exc.value.stderr == "HTTP 404: Not Found"
    assert exc.value.returncode == 1


def test_run_missing_cli_raises_repo_error(workbench, tmp_path, monkeypatch):
    empty = tmp_path / "emptybin"
    empty.mkdir()
    adapter = scm.adapter_for("app")
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(common.RepoError) as exc:
        adapter.run(["pr", "list"])
    assert "gh" in str(exc.value)


def test_json_parses_stdout(workbench, shim):
    shim.respond({"match": ["pr", "view"], "stdout": '{"number": 3}'})
    assert scm.adapter_for("app").json(["pr", "view", "3", "--json", "number"]) == {"number": 3}


def test_json_unparsable_raises_repo_error(workbench, shim):
    shim.respond({"match": ["api"], "stdout": "<html>"})
    with pytest.raises(common.RepoError):
        scm.adapter_for("proj").json(["api", "projects/x"])


def test_explicit_env_overrides_process_env(workbench, shim):
    env = {k: os.environ[k] for k in ("PATH", "SHIM_LOG", "SHIM_RULES")}
    a = scm.GitLab("svc", "git.corp.example", "team/svc", env=env)
    a.run(["mr", "list"])
    assert shim.last()["env"] == {"GITLAB_HOST": "git.corp.example"}


# --- host hardening: global tokens, slug validation, ports -----------------------

GLOBAL_TOKENS = (
    "GITLAB_TOKEN",
    "GITLAB_ACCESS_TOKEN",
    "OAUTH_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)


def _set_origin(workbench, repo, url):
    subprocess.run(
        ["git", "remote", "set-url", "origin", url], cwd=workbench / "code" / repo, check=True
    )


def test_substring_host_drops_global_tokens(workbench, shim, monkeypatch):
    for name in GLOBAL_TOKENS:
        monkeypatch.setenv(name, f"secret-{name}")
    # A hostile origin matched only by the substring rule gets no global token.
    for url, cli in (
        ("https://gitlab.attacker.tld/a/b.git", "glab"),
        ("git@github.attacker.tld:a/b.git", "gh"),
    ):
        _set_origin(workbench, "other", url)
        a = scm.adapter_for("other")
        assert a is not None and a.cli == cli
        a.run(["api", "user"])
        a.run(["pr" if cli == "gh" else "mr", "list"])
        for call in shim.calls()[-2:]:
            assert not set(GLOBAL_TOKENS) & set(call["tokens"]), (url, call)
    assert shim.last()["argv"][-2:] == ["-R", "github.attacker.tld/a/b"]
    # Mapped hosts (WORKBENCH_SCM_HOSTS) and the default hosts keep today's env.
    for repo in ("app", "proj", "tool", "svc"):
        scm.adapter_for(repo).run(["api", "user"])
        assert set(GLOBAL_TOKENS) <= set(shim.last()["tokens"]), repo
    # The map is honoured for a directly built adapter too, and only for its host.
    env = {k: os.environ[k] for k in ("PATH", "SHIM_LOG", "SHIM_RULES", *GLOBAL_TOKENS)}
    env["WORKBENCH_SCM_HOSTS"] = "gitlab.mine.tld=gitlab"
    scm.GitLab("svc", "gitlab.mine.tld", "a/b", env=env).run(["mr", "list"])
    assert set(GLOBAL_TOKENS) <= set(shim.last()["tokens"])
    scm.GitHub("svc", "gitlab.mine.tld", "a/b", env=env).run(["pr", "list"])
    assert not set(GLOBAL_TOKENS) & set(shim.last()["tokens"])
    # Other variables pass through untouched.
    assert "SHIM_LOG" in env and shim.last()["cli"] == "gh"


def test_github_slug_validated(workbench, shim):
    shim.respond({"match": ["api"], "stdout": '{"commit": {"sha": "abc"}}'})
    assert scm.GitHub("app", "github.com", "acme/app.js-x_1").branch_tip("main") == "abc"
    calls = len(shim.calls())
    for slug in (
        "acme/..",
        "../app",
        "acme/.",
        "./app",
        "acme/../../user",
        "acme/app/extra",
        "acme/a b",
        "acme/app?x=1",
        "acme/%2e%2e",
        "acme/app\n",
        "acme/\u00e9",
        "/app",
        "acme/",
    ):
        with pytest.raises(common.RepoError) as exc:
            scm.GitHub("app", "github.com", slug).branch_tip("main")
        assert "slug" in str(exc.value), slug
    assert len(shim.calls()) == calls


def test_non_default_port_kept(workbench, shim, monkeypatch):
    env = {"WORKBENCH_SCM_HOSTS": "git.corp:8443=gitlab, ghe.corp:8443=github"}
    # The map is looked up by host:port first, then by the bare hostname.
    assert scm.detect_host("https://git.corp:8443/team/svc.git", env) == "gitlab"
    assert scm.detect_host("https://ghe.corp:8443/acme/tool.git", env) == "github"
    assert scm.detect_host("https://git.corp:9000/team/svc.git", env) is None
    assert (
        scm.detect_host("https://git.corp.example:9000/t/s", {"WORKBENCH_SCM_HOSTS": HOSTS_ENV})
        == "gitlab"
    )
    assert scm.origin_authority("https://git.corp:8443/team/svc.git") == "git.corp:8443"
    assert scm.origin_authority("http://u:p@Git.Corp:8080/t/s") == "git.corp:8080"
    # A scheme's default port, an ssh port and an scp-like url carry no API port.
    assert scm.origin_authority("https://github.com:443/acme/app") == "github.com"
    assert scm.origin_authority("http://git.corp:80/t/s") == "git.corp"
    assert scm.origin_authority("ssh://git@ghe.corp:2222/acme/tool.git") == "ghe.corp"
    assert scm.origin_authority("git@gitlab.com:grp/proj.git") == "gitlab.com"
    assert scm.origin_authority("/srv/git/local.git") is None

    monkeypatch.setenv("WORKBENCH_SCM_HOSTS", env["WORKBENCH_SCM_HOSTS"])
    monkeypatch.setenv("GITLAB_TOKEN", "secret")
    _set_origin(workbench, "svc", "https://git.corp:8443/team/svc.git")
    a = scm.adapter_for("svc")
    assert isinstance(a, scm.GitLab) and (a.hostname, a.slug) == ("git.corp:8443", "team/svc")
    a.run(["mr", "list"])
    assert shim.last()["env"]["GITLAB_HOST"] == "git.corp:8443"
    assert "GITLAB_TOKEN" in shim.last()["tokens"]

    _set_origin(workbench, "tool", "https://ghe.corp:8443/acme/tool.git")
    a = scm.adapter_for("tool")
    assert isinstance(a, scm.GitHub) and a.hostname == "ghe.corp:8443"
    a.run(["api", "graphql", "-f", "query=q"])
    assert shim.last()["argv"] == ["api", "graphql", "-f", "query=q", "--hostname", "ghe.corp:8443"]
    a.run(["pr", "list"])
    assert shim.last()["argv"] == ["pr", "list", "-R", "ghe.corp:8443/acme/tool"]


# --- GitHub update operations (REST: GraphQL is blocked in cloud sessions) --------


def test_github_find_open(workbench, shim):
    shim.respond(
        {
            "match": ["repos/acme/app/pulls?state=open&head=acme:feat/x"],
            "stdout": json.dumps([{"number": 5, "html_url": "https://u/5"}]),
        }
    )
    assert scm.adapter_for("app").find_open("feat/x") == (5, "https://u/5")
    assert shim.last()["argv"] == ["api", "repos/acme/app/pulls?state=open&head=acme:feat/x"]


def test_github_find_open_none(workbench, shim):
    shim.respond({"match": ["api"], "stdout": "[]"})
    assert scm.adapter_for("app").find_open("feat/x") is None


def test_github_create_targets_default_branch(workbench, shim):
    shim.respond(
        {
            "match": ["POST"],
            "stdout": json.dumps({"html_url": "https://github.com/acme/app/pull/8"}),
        },
        {"match": ["repos/acme/app"], "stdout": json.dumps({"default_branch": "trunk"})},
    )
    url = scm.adapter_for("app").create("feat/x", "Title", "Body")
    assert url == "https://github.com/acme/app/pull/8"
    assert [c["argv"] for c in shim.calls()] == [
        ["api", "repos/acme/app"],
        [
            "api",
            "-X",
            "POST",
            "repos/acme/app/pulls",
            "-f",
            "head=feat/x",
            "-f",
            "base=trunk",
            "-f",
            "title=Title",
            "-f",
            "body=Body",
        ],
    ]


def test_github_edit_body(workbench, shim):
    scm.adapter_for("tool").edit_body(5, "new body")
    assert shim.last()["argv"] == [
        "api",
        "-X",
        "PATCH",
        "repos/acme/tool/pulls/5",
        "-f",
        "body=new body",
        "--hostname",
        "ghe.corp",
    ]


def test_github_semver_label(workbench, shim):
    pages = [[{"name": "bug"}], [{"name": "semver:minor"}]]
    shim.respond({"match": ["--slurp"], "stdout": json.dumps(pages)})
    assert scm.adapter_for("app").semver_label(5) == "minor"
    assert shim.last()["argv"] == [
        "api",
        "--paginate",
        "--slurp",
        "repos/acme/app/issues/5/labels?per_page=100",
    ]


def test_github_semver_label_none(workbench, shim):
    shim.respond({"match": ["--slurp"], "stdout": json.dumps([[{"name": "x"}]])})
    assert scm.adapter_for("app").semver_label(5) is None


def test_github_ensure_label_creates(workbench, shim):
    scm.adapter_for("app").ensure_label("patch")
    assert [c["argv"] for c in shim.calls()] == [
        [
            "api",
            "-X",
            "POST",
            "repos/acme/app/labels",
            "-f",
            "name=semver:patch",
            "-f",
            "color=0e8a16",
        ]
    ]


def test_github_ensure_label_existing_is_recoloured(workbench, shim):
    shim.respond({"match": ["POST"], "stderr": "gh: Validation Failed (HTTP 422)\n", "exit": 1})
    scm.adapter_for("app").ensure_label("minor")
    assert shim.last()["argv"] == [
        "api",
        "-X",
        "PATCH",
        "repos/acme/app/labels/semver%3Aminor",
        "-f",
        "color=fbca04",
    ]


def test_github_ensure_label_raises_other_errors(workbench, shim):
    shim.respond({"match": ["POST"], "stderr": "gh: Forbidden (HTTP 403)\n", "exit": 1})
    with pytest.raises(common.RepoError, match="403"):
        scm.adapter_for("app").ensure_label("minor")


def test_github_apply_label(workbench, shim):
    scm.adapter_for("app").apply_label(5, "major")
    assert shim.last()["argv"] == [
        "api",
        "-X",
        "POST",
        "repos/acme/app/issues/5/labels",
        "-f",
        "labels[]=semver:major",
    ]


def test_ensure_label_rejects_invalid_level(workbench, shim):
    with pytest.raises(common.RepoError):
        scm.adapter_for("app").ensure_label("huge")
    with pytest.raises(common.RepoError):
        scm.adapter_for("proj").ensure_label("huge")
    assert shim.calls() == []


# --- GitLab update operations ---------------------------------------------------


def test_gitlab_find_open(workbench, shim):
    shim.respond({"match": ["api"], "stdout": json.dumps([{"iid": 7, "web_url": "https://gl/7"}])})
    assert scm.adapter_for("proj").find_open("feat/x") == (7, "https://gl/7")
    assert shim.last()["argv"] == [
        "api",
        "projects/grp%2Fsub%2Fproj/merge_requests?source_branch=feat/x&state=opened",
    ]


def test_gitlab_find_open_encodes_branch_query_chars(workbench, shim):
    # A raw `+` decodes to a space and `&`/`#` cut the query; `/` stays readable.
    shim.respond({"match": ["api"], "stdout": "[]"})
    scm.adapter_for("proj").find_open("fix/a+b&c#d")
    assert shim.last()["argv"] == [
        "api",
        "projects/grp%2Fsub%2Fproj/merge_requests?source_branch=fix/a%2Bb%26c%23d&state=opened",
    ]


def test_gitlab_find_open_none(workbench, shim):
    shim.respond({"match": ["api"], "stdout": "[]"})
    assert scm.adapter_for("proj").find_open("feat/x") is None


def test_gitlab_create_targets_tracked_branch(workbench, shim):
    shim.respond({"match": ["POST"], "stdout": json.dumps({"iid": 4, "web_url": "https://gl/4"})})
    assert scm.adapter_for("proj").create("feat/x", "Title", "Body") == "https://gl/4"
    assert shim.last()["argv"] == [
        "api",
        "--method",
        "POST",
        "projects/grp%2Fsub%2Fproj/merge_requests",
        "-f",
        "source_branch=feat/x",
        "-f",
        "target_branch=develop",
        "-f",
        "title=Title",
        "-f",
        "description=Body",
    ]


def test_gitlab_create_defaults_target_to_main(workbench, shim):
    shim.respond({"match": ["POST"], "stdout": json.dumps({"iid": 1, "web_url": "https://gl/1"})})
    scm.adapter_for("svc").create("b", "T", "B")
    assert "target_branch=main" in shim.last()["argv"]
    assert shim.last()["env"]["GITLAB_HOST"] == "git.corp.example"


def test_gitlab_edit_body(workbench, shim):
    scm.adapter_for("proj").edit_body(7, "new body")
    assert shim.last()["argv"] == [
        "api",
        "--method",
        "PUT",
        "projects/grp%2Fsub%2Fproj/merge_requests/7",
        "-f",
        "description=new body",
    ]


def test_gitlab_semver_label(workbench, shim):
    shim.respond({"match": ["api"], "stdout": json.dumps({"labels": ["x", "semver:patch"]})})
    assert scm.adapter_for("proj").semver_label(7) == "patch"
    assert shim.last()["argv"] == ["api", "projects/grp%2Fsub%2Fproj/merge_requests/7"]


def test_gitlab_semver_label_none(workbench, shim):
    shim.respond({"match": ["api"], "stdout": json.dumps({"labels": []})})
    assert scm.adapter_for("proj").semver_label(7) is None


def test_gitlab_ensure_label(workbench, shim):
    scm.adapter_for("proj").ensure_label("minor")
    assert shim.last()["argv"] == [
        "api",
        "--method",
        "POST",
        "projects/grp%2Fsub%2Fproj/labels",
        "-f",
        "name=semver:minor",
        "-f",
        "color=#fbca04",
    ]


def test_gitlab_ensure_label_ignores_409(workbench, shim):
    shim.respond({"match": ["POST"], "stderr": "glab: 409 Conflict", "exit": 1})
    scm.adapter_for("proj").ensure_label("major")


def test_gitlab_ensure_label_raises_other_errors(workbench, shim):
    shim.respond({"match": ["POST"], "stderr": "glab: 403 Forbidden", "exit": 1})
    with pytest.raises(common.RepoError) as exc:
        scm.adapter_for("proj").ensure_label("major")
    assert "403 Forbidden" in str(exc.value)


def test_gitlab_apply_label(workbench, shim):
    scm.adapter_for("proj").apply_label(7, "major")
    assert shim.last()["argv"] == [
        "api",
        "--method",
        "PUT",
        "projects/grp%2Fsub%2Fproj/merge_requests/7",
        "-f",
        "add_labels=semver:major",
    ]


# --- base adapter ----------------------------------------------------------------


def test_adapter_base_is_abstract():
    with pytest.raises(TypeError):
        scm.Adapter("app", "github.com", "acme/app")


# --- acceptance conditions, one test each ------------------------------------------


def test_host_map_parses_and_rejects_malformed():
    env = {"WORKBENCH_SCM_HOSTS": " GHE.corp = github ,git.corp.example=gitlab, "}
    assert scm.host_map(env) == {"ghe.corp": "github", "git.corp.example": "gitlab"}
    assert scm.host_map({}) == {}
    for entry in ("nohost", "=gitlab", "h.example=bitbucket"):
        with pytest.raises(scm.HostMapError) as exc:
            scm.host_map({"WORKBENCH_SCM_HOSTS": f"ok.example=github,{entry}"})
        assert entry in str(exc.value)


def test_detect_host_order_and_url_forms():
    env = {"WORKBENCH_SCM_HOSTS": "gitlab-mirror.github.io=gitlab,ghe.corp=github"}
    # The map beats the substring rule, for every url form.
    assert scm.detect_host("https://gitlab-mirror.github.io/a/b.git", env) == "gitlab"
    assert scm.detect_host("ssh://git@GHE.corp:2222/a/b.git", env) == "github"
    assert scm.detect_host("git@ghe.corp:a/b.git", env) == "github"
    # Substring rule next.
    assert scm.detect_host("https://github.com/a/b.git", {}) == "github"
    assert scm.detect_host("ssh://git@gitlab.example:22/a/b.git", {}) == "gitlab"
    assert scm.detect_host("git@gitlab.com:a/b.git", {}) == "gitlab"
    # Else None.
    assert scm.detect_host("https://bitbucket.org/a/b.git", {}) is None
    assert scm.detect_host("", env) is None


def test_adapter_for_mixed_hosts_and_local_only(workbench):
    adapters = {name: scm.adapter_for(name) for name in ORIGINS}
    assert isinstance(adapters["app"], scm.GitHub)
    assert isinstance(adapters["tool"], scm.GitHub)
    assert isinstance(adapters["proj"], scm.GitLab)
    assert isinstance(adapters["svc"], scm.GitLab)
    assert adapters["local"] is None
    assert adapters["other"] is None


def test_adapter_targets_repo_explicitly(workbench, shim):
    scm.adapter_for("app").run(["pr", "list"])
    assert shim.last()["argv"] == ["pr", "list", "-R", "acme/app"]
    scm.adapter_for("tool").run(["pr", "list"])
    assert shim.last()["argv"] == ["pr", "list", "-R", "ghe.corp/acme/tool"]
    scm.adapter_for("tool").run(["api", "user"])
    assert shim.last()["argv"][-2:] == ["--hostname", "ghe.corp"]
    scm.adapter_for("proj").run(["mr", "list"])
    call = shim.last()
    assert call["argv"] == ["mr", "list", "-R", "grp/sub/proj"]
    assert "GITLAB_HOST" not in call["env"]
    svc = scm.adapter_for("svc")
    svc.run(["api", f"projects/{svc.project}"])
    call = shim.last()
    assert call["argv"] == ["api", "projects/team%2Fsvc"]
    assert call["env"]["GITLAB_HOST"] == "git.corp.example"
    # The fixture exports GH_HOST/GITLAB_HOST=evil.example; no call may see it.
    assert all(c["env"].get(k) != "evil.example" for c in shim.calls() for k in c["env"])
    assert all(
        Path(c["cwd"]).resolve() != (workbench / "code" / "app").resolve() for c in shim.calls()
    )


def test_ref_is_host_native(workbench):
    assert scm.adapter_for("app").ref(12) == "pr #12"
    assert scm.adapter_for("svc").ref(12) == "mr !12"


def test_update_operations_both_hosts(workbench, shim):
    shim.respond(
        {
            "cli": "gh",
            "match": ["repos/acme/app/pulls?state=open&head=acme:feat/x"],
            "stdout": json.dumps([{"number": 5, "html_url": "u5"}]),
        },
        {
            "cli": "gh",
            "match": ["repos/acme/app"],
            "stdout": json.dumps({"default_branch": "main"}),
        },
        {
            "cli": "gh",
            "match": ["POST", "repos/acme/app/pulls"],
            "stdout": json.dumps({"html_url": "u6"}),
        },
        {
            "cli": "gh",
            "match": ["--slurp"],
            "stdout": json.dumps([[{"name": "semver:minor"}]]),
        },
        {
            "cli": "glab",
            "match": ["POST", "projects/grp%2Fsub%2Fproj/labels"],
            "stderr": "409 Conflict",
            "exit": 1,
        },
        {"cli": "glab", "match": ["POST"], "stdout": json.dumps({"iid": 4, "web_url": "u4"})},
        {"cli": "glab", "match": ["PUT"], "stdout": "{}"},
        {"cli": "glab", "match": ["api"], "stdout": json.dumps([{"iid": 7, "web_url": "u7"}])},
    )
    gh = scm.adapter_for("app")
    assert gh.find_open("feat/x") == (5, "u5")
    assert gh.create("feat/x", "T", "B") == "u6"
    assert "base=main" in shim.last()["argv"]
    gh.edit_body(5, "b")
    assert gh.semver_label(5) == "minor"
    gh.ensure_label("patch")
    gh.apply_label(5, "patch")
    assert shim.last()["argv"][3:5] == ["repos/acme/app/issues/5/labels", "-f"]

    gl = scm.adapter_for("proj")
    assert gl.find_open("feat/x") == (7, "u7")
    assert gl.create("feat/x", "T", "B") == "u4"
    assert "target_branch=develop" in shim.last()["argv"]
    gl.edit_body(7, "b")
    assert "description=b" in shim.last()["argv"]
    gl.ensure_label("major")  # 409 tolerated
    gl.apply_label(7, "major")
    assert "add_labels=semver:major" in shim.last()["argv"]
