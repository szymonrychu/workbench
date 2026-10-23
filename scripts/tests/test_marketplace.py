"""Pins the root marketplace, the pack manifests and the committed project settings."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = {
    "workbench": "./plugins/workbench",
    "workbench-infra": "./plugins/workbench-infra",
    "workbench-observability": "./plugins/workbench-observability",
    "workbench-secrets": "./plugins/workbench-secrets",
}
PACKS = [n for n in PLUGINS if n != "workbench"]


def _json(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text())


def _manifest(source: str) -> dict:
    return _json(f"{source}/.claude-plugin/plugin.json")


def test_marketplace_header():
    m = _json(".claude-plugin/marketplace.json")
    assert m["name"] == "workbench-template"
    assert m["owner"] == {"name": "workbench contributors"}
    assert len(m["plugins"]) == 4


def test_entries_sources_and_names():
    m = _json(".claude-plugin/marketplace.json")
    assert {e["name"]: e["source"] for e in m["plugins"]} == PLUGINS
    for e in m["plugins"]:
        assert e["source"].startswith("./") and ".." not in e["source"]
        assert (ROOT / e["source"] / ".claude-plugin" / "plugin.json").is_file()
        assert _manifest(e["source"])["name"] == e["name"]
        assert e["description"] and e["version"]
        assert e["version"] == _manifest(e["source"])["version"]


def test_core_description_equals_manifest():
    m = _json(".claude-plugin/marketplace.json")
    core = next(e for e in m["plugins"] if e["name"] == "workbench")
    assert core["description"] == _manifest("plugins/workbench")["description"]


def test_pack_manifests():
    for name in PACKS:
        p = _manifest(PLUGINS[name])
        assert p["name"] == name
        assert p["version"] == "1.0.0"
        assert p["description"].endswith("Requires the workbench core plugin.")
        assert "\n" not in p["description"]
        assert p["author"] == {"name": "workbench contributors"}


HOOK = 'python3 "$CLAUDE_PROJECT_DIR/scripts/cloud_session_start.py"'


def test_project_settings_exact():
    assert _json(".claude/settings.json") == {
        "extraKnownMarketplaces": {
            "workbench-template": {"source": {"source": "directory", "path": "./"}}
        },
        "enabledPlugins": {
            "workbench@workbench-template": True,
            "workbench-infra@workbench-template": False,
            "workbench-observability@workbench-template": False,
            "workbench-secrets@workbench-template": False,
        },
        "permissions": {"allow": []},
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume",
                    "hooks": [
                        {
                            "type": "command",
                            "command": HOOK,
                            "timeout": 600,
                            "statusMessage": "Preparing the cloud session...",
                        }
                    ],
                }
            ]
        },
    }


def test_probe_script_present_and_executable():
    probe = ROOT / "scripts/probes/claude-plugin-load.sh"
    assert probe.stat().st_mode & 0o111
    text = probe.read_text()
    assert "exit 3" not in text or "claude" in text
    assert "hasTrustDialogAccepted" in text


def test_core_version_is_spec_1_0_0_in_entry_manifest_and_changelog():
    m = _json(".claude-plugin/marketplace.json")
    core = next(e for e in m["plugins"] if e["name"] == "workbench")
    assert core["version"] == "1.0.0"
    assert _manifest("plugins/workbench")["version"] == "1.0.0"
    changelog = (ROOT / "plugins/workbench/CHANGELOG.md").read_text()
    assert changelog.splitlines()[2].startswith("## v1.0.0")


def test_edit_hooks_cover_all_edit_tools():
    hooks = _manifest("./plugins/workbench")["hooks"]
    seen = 0
    for groups in hooks.values():
        for g in groups:
            cmds = " ".join(h["command"] for h in g["hooks"])
            if "protect_files" in cmds or "format-file" in cmds:
                seen += 1
                want = {"Edit", "MultiEdit", "NotebookEdit", "Write"}
                assert set(g["matcher"].split("|")) == want
    assert seen == 2
