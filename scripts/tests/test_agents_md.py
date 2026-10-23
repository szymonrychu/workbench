"""Pins AGENTS.md (the one instruction source) and CLAUDE.md (the Claude-only shim)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS = (ROOT / "AGENTS.md").read_text()
CLAUDE = (ROOT / "CLAUDE.md").read_text()


def _block(name: str) -> str:
    m = re.search(
        rf"<!-- workbench:{name}:begin -->(.*?)<!-- workbench:{name}:end -->", AGENTS, re.S
    )
    assert m, f"missing block {name}"
    return m.group(1)


def test_sizes():
    assert len(AGENTS.encode()) < 16000
    assert len(CLAUDE.encode()) < 2000


def test_block_order():
    order = [
        AGENTS.index("<!-- workbench:uninitialised:begin -->"),
        AGENTS.index("## What this repository is"),
        AGENTS.index("<!-- workbench:project:begin -->"),
        AGENTS.index("## Ground rules"),
        AGENTS.index("## Writing and working conventions"),
        AGENTS.index("<!-- workbench:rules:begin -->"),
        AGENTS.index("## Template maintenance"),
    ]
    assert order == sorted(order)
    assert AGENTS.index("<!-- workbench:rules:end -->") < order[-1]


def test_uninitialised_block():
    b = _block("uninitialised")
    for needle in (
        "not initialised",
        "init",
        "workbench:init",
        "Template maintenance",
        "never run init",
    ):
        assert needle in b
    assert AGENTS.lstrip().startswith("<!-- workbench:uninitialised:begin -->")


def test_project_block_placeholders():
    b = _block("project")
    for h in (
        "What this project is building",
        "Components (code/)",
        "Component hard rules",
        "Highest-risk surface",
    ):
        assert h in b
    assert "| Submodule | Lang | Role | Build |" in b


def test_rules_block_generated():
    assert "Generated from plugins/workbench/rules/" in _block("rules")


def test_ground_rules_content():
    for needle in (
        "start-development",
        "start-review",
        "start-debugging",
        "plan-execute",
        "full path",
        "research",
        "opus",
        "sonnet",
        "fable",
        "every dispatch",
        "noted, not doing now",
        "never skip",
        "Never report waiting",
        "first item",
        "10 minutes",
        "wall-clock",
        "main is always green",
        "least destructive",
        "standing approval",
        "wrapper",
        "said they will run",
        "simplify the files the task already touches",
        "em dashes",
        "MEMORY.md",
        "ROADMAP.md",
        "handoff",
    ):
        assert needle.lower() in AGENTS.lower(), needle


def test_template_maintenance():
    t = AGENTS[AGENTS.index("## Template maintenance") :]
    for needle in ("mise run test", "mise run lint", "main checkout", "never run init"):
        assert needle.lower() in t.lower()


def test_no_personal_or_stale_content():
    low = AGENTS.lower()
    banned = ("adhd", "superpowers", "engineering:debug", "szy" + "mon", "/us" + "ers/")
    for bad in (*banned, "—", "’", "→"):
        assert bad not in low
    assert AGENTS.isascii() and CLAUDE.isascii()


def test_claude_md():
    assert CLAUDE.splitlines()[0] == "@AGENTS.md"
    assert "## Claude Code only" in CLAUDE
    for needle in (
        "workbench:<skill>",
        "/reload-plugins",
        "Workflow",
        "trust dialog",
        "heartbeat",
        "docs/claude-cloud.md",
    ):
        assert needle in CLAUDE


def test_project_block_has_no_parking_wording():
    b = _block("project").lower()
    assert "placeholder" not in b
    assert "until then" not in b


PAIRS = ("uninitialised", "project", "rules")


def _marker_problems(text: str) -> list[str]:
    """Empty when every pair occurs exactly once, begin before end, pairs in order."""
    problems = []
    spans = []
    for name in PAIRS:
        b, e = f"<!-- workbench:{name}:begin -->", f"<!-- workbench:{name}:end -->"
        if text.count(b) != 1 or text.count(e) != 1:
            problems.append(f"{name}: marker not exactly once")
            continue
        if text.index(b) > text.index(e):
            problems.append(f"{name}: end before begin")
        spans.append((text.index(b), text.index(e)))
    for (_, prev_end), (nxt_begin, _) in zip(spans, spans[1:], strict=False):
        if prev_end > nxt_begin:
            problems.append("blocks out of order or overlapping")
    return problems


def test_marker_pairs_unique_and_ordered():
    assert _marker_problems(AGENTS) == []


def test_marker_pairs_detect_breakage(tmp_path):
    end = "<!-- workbench:project:end -->"
    begin = "<!-- workbench:project:begin -->"
    dup = tmp_path / "dup.md"
    dup.write_text(AGENTS + end)
    assert _marker_problems(dup.read_text())
    missing = tmp_path / "missing.md"
    missing.write_text(AGENTS.replace(end, ""))
    assert _marker_problems(missing.read_text())
    swapped = tmp_path / "swapped.md"
    swapped.write_text(AGENTS.replace(begin, "@@").replace(end, begin).replace("@@", end))
    assert _marker_problems(swapped.read_text())


def test_model_table_has_haiku():
    flat = " ".join(AGENTS.split())
    assert "mechanical relays and classifiers: haiku" in flat


def test_claude_md_heartbeat_wording():
    assert "keeps long unattended runs alive" not in CLAUDE
    assert "reports progress on long runs; keepalive only per its table" in CLAUDE


def test_banner_routes_other_requests():
    flat = " ".join(_block("uninitialised").replace(">", " ").split()).lower()
    assert "any other development request" in flat
    assert "ask the user whether to run init first" in flat
