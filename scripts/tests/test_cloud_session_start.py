"""Pins scripts/cloud_session_start.py: cloud-only, plugin install, mise config, submodules."""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cloud_session_start.py"
sys.path.insert(0, str(REPO / "scripts"))
import cloud_session_start as css  # noqa: E402

MARKET = "demo-workbench"


def _settings(enabled=None, known=None):
    return {
        "extraKnownMarketplaces": known
        if known is not None
        else {MARKET: {"source": {"source": "directory", "path": "./"}}},
        "enabledPlugins": enabled
        if enabled is not None
        else {f"workbench@{MARKET}": True, f"workbench-infra@{MARKET}": False},
    }


class Recorder:
    """A fake runner: records argv and cwd, answers from a {first-args: (rc, out)} table."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), cwd))
        for prefix, (rc, out) in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return rc, out
        return 0, ""


def test_local_plugins_picks_enabled_plugins_of_directory_marketplaces(tmp_path):
    settings = _settings(
        enabled={
            f"workbench@{MARKET}": True,
            f"workbench-infra@{MARKET}": False,
            "other@remote": True,
        },
        known={
            MARKET: {"source": {"source": "directory", "path": "./"}},
            "remote": {"source": {"source": "github", "repo": "o/r"}},
        },
    )
    assert css.local_plugins(settings, tmp_path) == [(tmp_path, [f"workbench@{MARKET}"])]


def test_local_plugins_resolves_relative_marketplace_path(tmp_path):
    settings = _settings(known={MARKET: {"source": {"source": "directory", "path": "sub"}}})
    assert css.local_plugins(settings, tmp_path) == [(tmp_path / "sub", [f"workbench@{MARKET}"])]


def test_install_plugins_adds_marketplace_then_installs_each_at_user_scope(tmp_path):
    run = Recorder()
    failures = css.install_plugins([(tmp_path, ["a@m", "b@m"])], run, tmp_path)
    assert failures == []
    assert [c[0] for c in run.calls] == [
        ["claude", "plugin", "marketplace", "add", str(tmp_path), "--scope", "user"],
        ["claude", "plugin", "install", "a@m", "--scope", "user"],
        ["claude", "plugin", "install", "b@m", "--scope", "user"],
    ]


def test_install_plugins_reports_a_failed_install_and_keeps_going(tmp_path):
    run = Recorder({("claude", "plugin", "install", "a@m"): (1, "boom\nreal reason")})
    failures = css.install_plugins([(tmp_path, ["a@m", "b@m"])], run, tmp_path)
    assert failures == ["plugin a@m: real reason"]
    assert run.calls[-1][0][3] == "b@m"


def test_write_mise_config_disables_attestations_and_aliases_bats_to_npm(tmp_path):
    path = css.write_mise_config(tmp_path)
    text = path.read_text()
    assert path == tmp_path / "mise" / "conf.d" / "workbench-cloud.toml"
    assert "aqua.github_attestations = false" in text
    assert 'npm.package_manager = "npm"' in text
    assert 'bats = "npm:bats"' in text


def test_github_slug_handles_https_ssh_and_other_hosts():
    assert css.github_slug("https://github.com/o/r") == "o/r"
    assert css.github_slug("https://github.com/o/r.git") == "o/r"
    assert css.github_slug("git@github.com:o/r.git") == "o/r"
    assert css.github_slug("ssh://git@github.com/o/r") == "o/r"
    assert css.github_slug("https://gitlab.com/o/r") is None


def test_submodules_reads_paths_and_urls(tmp_path):
    out = (
        "submodule.a.path code/a\n"
        "submodule.a.url git@github.com:o/a.git\n"
        "submodule.b.path code/b\n"
        "submodule.b.url https://gitlab.com/o/b\n"
    )
    run = Recorder({("git", "config", "-f", ".gitmodules"): (0, out)})
    (tmp_path / ".gitmodules").write_text("x")
    assert css.submodules(tmp_path, run) == [
        ("code/a", "git@github.com:o/a.git"),
        ("code/b", "https://gitlab.com/o/b"),
    ]


def test_submodules_is_empty_without_gitmodules(tmp_path):
    assert css.submodules(tmp_path, Recorder()) == []


def test_context_names_add_repo_for_each_github_submodule_and_failed_inits():
    text = css.context(
        subs=[("code/a", "git@github.com:o/a.git"), ("code/b", "https://gitlab.com/o/b")],
        failed_subs=["code/a"],
        failures=["mise install: x"],
    )
    assert 'add_repo(owner="o", repo="a", access="push")' in text
    assert "code/b" not in text.split("add_repo")[1]
    assert "git submodule update --init --recursive -- code/a" in text
    assert "gh api repos/" in text
    assert "mise install: x" in text


def test_init_submodules_skips_checked_out_ones_and_reports_failures(tmp_path):
    (tmp_path / "code" / "a").mkdir(parents=True)
    (tmp_path / "code" / "a" / ".git").write_text("gitdir: x")
    run = Recorder({("git", "submodule", "update", "--init", "--recursive", "--", "code/c"):
                    (1, "denied")})  # fmt: skip
    failed = css.init_submodules(tmp_path, [("code/a", "u"), ("code/b", "u"), ("code/c", "u")], run)
    assert failed == ["code/c"]
    assert [c[0][-1] for c in run.calls] == ["code/b", "code/c"]


def _run_script(tmp_path, remote):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDE_PROJECT_DIR": str(tmp_path)}
    if remote is not None:
        env["CLAUDE_CODE_REMOTE"] = remote
    return subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, input="{}"
    )


def test_outside_cloud_the_hook_does_nothing(tmp_path):
    proc = _run_script(tmp_path, None)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert not (tmp_path / ".config").exists()


def test_main_in_cloud_emits_session_start_context(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(_settings()))
    run = Recorder()
    out = css.main({"CLAUDE_CODE_REMOTE": "true", "CLAUDE_PROJECT_DIR": str(tmp_path)}, run,
                   config_home=tmp_path / "cfg")  # fmt: skip
    payload = json.loads(out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "gh api repos/" in payload["hookSpecificOutput"]["additionalContext"]
    argvs = [c[0] for c in run.calls]
    assert ["claude", "plugin", "install", f"workbench@{MARKET}", "--scope", "user"] in argvs
    assert ["mise", "install"] in argvs
    assert (tmp_path / "cfg" / "mise" / "conf.d" / "workbench-cloud.toml").is_file()


def test_project_settings_register_the_hook():
    settings = json.loads((REPO / ".claude" / "settings.json").read_text())
    commands = [h["command"] for entry in settings["hooks"]["SessionStart"] for h in entry["hooks"]]
    assert any("scripts/cloud_session_start.py" in c for c in commands)
