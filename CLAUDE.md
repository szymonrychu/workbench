@AGENTS.md

## Claude Code only

- Plugin skills are namespaced `workbench:<skill>` (for example `workbench:init`,
  `workbench:start-development`). Other harnesses use the bare skill name.
- After editing a plugin, run `/reload-plugins` to pick up the change.
- `plan-execute` runs through the Workflow tool.
- The first run in a clone shows the trust dialog; accept it so project settings and the
  workbench plugins load.
- The `heartbeat` skill reports progress on long runs; keepalive only per its table.
- In a Claude Code cloud session the trust dialog and `/reload-plugins` do not exist: the
  project SessionStart hook installs the plugins instead. Setup and limits:
  `docs/claude-cloud.md`.
