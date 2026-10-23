#!/usr/bin/env python3
"""SessionStart hook that makes a workbench usable in a Claude Code cloud session.

Runs only when CLAUDE_CODE_REMOTE=true; anywhere else it exits 0 silently. A cloud session
differs from a local one in three ways this hook repairs, see docs/claude-cloud.md:

- It never installs the plugins `.claude/settings.json` enables, so the workbench plugins
  are installed here at user scope from the in-repo marketplace; a plugin installed by a
  SessionStart hook loads in that same session.
- Its egress proxy blocks sigstore and github.com source archives, which `mise install`
  needs; a mise conf.d file skips aqua's attestation check and takes bats from npm.
- Its git proxy credentials only repositories attached to the session, so submodule pushes
  fail until the agent attaches each one; the hook tells the agent how.

Never fails the session: every problem is reported in additionalContext and the exit is 0.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

Runner = Callable[[list[str], Path], tuple[int, str]]

MISE_CONFIG = """\
# Written by scripts/cloud_session_start.py for Claude Code cloud sessions.
[settings]
# The proxy blocks tuf-repo-cdn.sigstore.dev; aqua still checks release checksums.
aqua.github_attestations = false
# mise's default npm installer creates no bin links for bats.
npm.package_manager = "npm"

[tool_alias]
# github.com source tarballs are refused for repositories not attached to the session.
bats = "npm:bats"
"""

_GITHUB = re.compile(r"^(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
                     r"([^/]+)/([^/]+?)(?:\.git)?/?$")  # fmt: skip


def run(argv: list[str], cwd: Path) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=540)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _last_line(out: str) -> str:
    lines = [line for line in out.splitlines() if line.strip()]
    return lines[-1].strip() if lines else "no output"


def local_plugins(settings: Mapping, root: Path) -> list[tuple[Path, list[str]]]:
    """(marketplace dir, enabled plugin ids) for each directory marketplace the settings name."""
    enabled = [k for k, v in (settings.get("enabledPlugins") or {}).items() if v is True]
    found = []
    for name, entry in (settings.get("extraKnownMarketplaces") or {}).items():
        source = (entry or {}).get("source") or {}
        if source.get("source") != "directory" or not source.get("path"):
            continue
        ids = [k for k in enabled if k.endswith(f"@{name}")]
        if ids:
            found.append(((root / source["path"]).resolve(), ids))
    return found


def install_plugins(markets: list[tuple[Path, list[str]]], runner: Runner, root: Path) -> list[str]:
    failures = []
    for path, ids in markets:
        rc, out = runner(
            ["claude", "plugin", "marketplace", "add", str(path), "--scope", "user"], root
        )
        if rc:
            failures.append(f"marketplace {path}: {_last_line(out)}")
            continue
        for plugin in ids:
            rc, out = runner(["claude", "plugin", "install", plugin, "--scope", "user"], root)
            if rc:
                failures.append(f"plugin {plugin}: {_last_line(out)}")
    return failures


def write_mise_config(config_home: Path) -> Path:
    path = config_home / "mise" / "conf.d" / "workbench-cloud.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MISE_CONFIG)
    return path


def github_slug(url: str) -> str | None:
    m = _GITHUB.match(url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def submodules(root: Path, runner: Runner) -> list[tuple[str, str]]:
    """(path, url) of each submodule .gitmodules declares, in file order."""
    if not (root / ".gitmodules").is_file():
        return []
    rc, out = runner(
        ["git", "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.(path|url)$"],
        root,
    )
    if rc:
        return []
    paths: dict[str, str] = {}
    urls: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        name, _, field = key[len("submodule.") :].rpartition(".")
        (paths if field == "path" else urls)[name] = value
    return [(paths[n], urls[n]) for n in paths if n in urls]


def init_submodules(root: Path, subs: list[tuple[str, str]], runner: Runner) -> list[str]:
    """Check out each submodule not yet checked out; returns the paths that failed. One that
    is already checked out is left alone: on resume it may hold the agent's unpinned work."""
    failed = []
    for path, _url in subs:
        if (root / path / ".git").exists():
            continue
        rc, _out = runner(["git", "submodule", "update", "--init", "--recursive", "--", path], root)
        if rc:
            failed.append(path)
    return failed


def context(subs: list[tuple[str, str]], failed_subs: list[str], failures: list[str]) -> str:
    lines = [
        "Claude Code cloud session (scripts/cloud_session_start.py, docs/claude-cloud.md):",
        "- GitHub GraphQL is blocked here, so `gh pr`, `gh issue` and `gh repo view --json` fail;"
        " use REST: `gh api repos/{owner}/{repo}/...`.",
    ]
    pushable = [(p, github_slug(u)) for p, u in subs if github_slug(u)]
    if pushable:
        lines.append(
            "- The git proxy only credentials repositories attached to this session. Before"
            " fetching a private submodule or pushing in one, call the add_repo tool once per"
            " repository; do not clone it again, keep working in its submodule path:"
        )
        for path, slug in pushable:
            owner, _, repo = slug.partition("/")
            lines.append(f'  - {path}: add_repo(owner="{owner}", repo="{repo}", access="push")')
    for path in failed_subs:
        lines.append(
            f"- Submodule {path} is not checked out; after add_repo run"
            f" `git submodule update --init --recursive -- {path}`."
        )
    for failure in failures:
        lines.append(f"- Setup problem: {failure}")
    return "\n".join(lines)


def main(env: Mapping[str, str], runner: Runner = run, config_home: Path | None = None) -> str:
    """The hook's stdout: empty outside a cloud session, else a SessionStart JSON payload."""
    if env.get("CLAUDE_CODE_REMOTE") != "true":
        return ""
    root = Path(env.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    failures: list[str] = []

    try:
        settings = json.loads((root / ".claude" / "settings.json").read_text())
    except (OSError, ValueError) as e:
        settings = {}
        failures.append(f"reading .claude/settings.json: {e}")
    failures += install_plugins(local_plugins(settings, root), runner, root)

    subs = submodules(root, runner)
    failed_subs = init_submodules(root, subs, runner)

    home = config_home or Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    write_mise_config(home)
    for argv in (["mise", "trust", "--quiet"], ["mise", "install"]):
        rc, out = runner(argv, root)
        if rc:
            failures.append(f"{' '.join(argv)}: {_last_line(out)}")
            break

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context(subs, failed_subs, failures),
        }
    }
    return json.dumps(payload)


if __name__ == "__main__":
    out = main(os.environ)
    if out:
        print(out)
    sys.exit(0)
