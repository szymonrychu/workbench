# Workbench

A generic workbench template: a thin metarepo that an agent turns into a new
project, or into a home for repos you already have. It works in Claude Code with
zero setup, and in GitHub Copilot, Cursor, OpenAI Codex, Gemini CLI and
Antigravity after one `mise run harness-setup -- <target>` (or `all`).

Template URL: <https://github.com/szymonrychu/workbench>

## What a workbench is, and why

A workbench is a thin superrepo. It holds almost no product code of its own:

- **The code** lives in git submodules under `code/`. Each component is its own
  repo with its own tests, `MEMORY.md` and `ROADMAP.md`.
- **The tooling** lives in plugins under `plugins/`: a core plugin plus optional
  packs, offered through a root marketplace that the project settings enable.
- **The rules** live in `AGENTS.md`, the one instruction file, with the
  load-bearing working rules embedded. Nothing depends on anyone's home
  directory.
- **The agents** follow the same flow everywhere: discovery, design and plan
  behind one approval gate, then unattended execution with tests, review and a
  scope audit.

The point: a new project no longer starts by hand-copying root files and rules
from an older metarepo, and colleagues can reuse the same flow regardless of
which agent harness they use.

## Supported platforms

macOS, Linux and WSL. Native Windows is not supported; use WSL.

## Quick start

1. Clone the template, or copy it into a new directory:

   ```sh
   git clone https://github.com/szymonrychu/workbench my-project
   cd my-project
   ```

2. Install the pinned tools (python, uv, ruff, shellcheck, bats, jq, gitleaks,
   gh, pre-commit, pytest):

   ```sh
   mise install
   ```

3. Run `claude` at the repo root and accept the trust dialog. Project settings
   then load the workbench plugin from the root marketplace.
4. If the plugin is not listed, run `/reload-plugins`.

   In a Claude Code cloud session (claude.ai/code, the Claude app, `claude --cloud`) there is
   no trust dialog and no `/reload-plugins`; a SessionStart hook installs the plugins and the
   tools instead. Set the cloud environment up once with
   [docs/claude-cloud.md](docs/claude-cloud.md).
5. Ask, in plain words:
   - "start a new project called X ..." or
   - "adopt the repos at /path/a /path/b ..."

   (Skill names: `workbench:init` for new and adopt flows.)

Until you do this, `AGENTS.md` carries an "uninitialised" banner telling any
agent to run init first.

## What init does

- **New project:** asks what you are building and the rules every component
  must obey, then writes the project description, hard rules and highest-risk
  surface into `AGENTS.md`, `MEMORY.md` and `ROADMAP.md`, and renames the
  marketplace after your project.
- **Adopt repos:** takes existing repo paths or URLs, adds each as a submodule
  under `code/`, detects branch and language, and fills the components table.
- **Adopt a monorepo:** registers the directories of one repo as components
  without splitting it.
- **Components later:** add or remove a component at any time; the components
  table and `code/` tasks are regenerated.
- **Forge creation only after confirmation:** init never creates a GitHub or
  GitLab repo or pushes without you confirming first.
- **Template docs removed:** the template's own docs (plans, records, contracts,
  research, handoffs) are deleted, and `MEMORY.md` and `ROADMAP.md` start fresh.
- **A single commit:** everything init writes lands in one commit.

## Repo layout

| Path | Purpose |
|---|---|
| `AGENTS.md` | the one instruction file, read by every harness |
| `CLAUDE.md` | imports `AGENTS.md` and adds Claude Code specifics |
| `MEMORY.md` | dated, non-obvious decisions with the why |
| `ROADMAP.md` | human-owned requirements |
| `code/` | component submodules |
| `plugins/` | the core plugin and the packs |
| `.claude-plugin/marketplace.json` | root marketplace listing the plugins |
| `.claude/` | project settings and `rules/` |
| `docs/` | plans, research, handoffs, architecture, harnesses, enterprise, claude-cloud |
| `scripts/` | init engine, harness generators, doctor, lint checks |
| `mise-tasks/` | the `mise run ...` tasks |
| `.github/workflows/` | CI |

## Plugins and packs

The core plugin `workbench` is enabled by default. It brings the development
flow, the approval ledger, file-based epic tickets, research skills, tiered
review, debugging flow, verification hooks, per-task git worktrees with an epic integration worktree and
destructive-command guardrails.

Optional packs, all requiring the core:

| Pack | Adds |
|---|---|
| `workbench-infra` | Kubernetes, Terraform and Helm diagnostic agents, chart drift checks |
| `workbench-observability` | Grafana, Prometheus, Loki and Kubernetes health diagnostics |
| `workbench-secrets` | dotenv and SOPS secret handling guardrails |

Enable a pack by flipping it to `true` in `.claude/settings.json`, then run
`/reload-plugins`:

```json
"enabledPlugins": {
  "workbench@workbench-template": true,
  "workbench-infra@workbench-template": true
}
```

The marketplace name after `@` is the one init wrote (see
`.claude-plugin/marketplace.json`). Each plugin has its own README under
`plugins/<name>/`.

## Working with the workbench

These are the ways of working that hold up day to day. Each has an example
prompt. In other harnesses, use the bare skill names instead of the
`workbench:` prefix.

### Write requests as a brief

State what you want, why, the constraints, and what done looks like. A brief
that carries all four needs no clarifying questions.

> Add rate limiting to the public API. Why: one client saturated it last week.
> Constraints: no new infrastructure, keep the current auth. Done when: a client
> over 100 requests per minute gets 429, the limit is configurable, tests and
> docs are updated.

Several features in one message are fine: the agent splits them into separate
epics, each with its own design, plan and branch, so they run independently.

### The one gate

Non-trivial work starts with `workbench:start-development`: discovery, design
and plan, then one approval gate. The gate lists the open questions as numbered
options. Answer by number and state corrections bluntly.

> 1b, 2a, 3: no, drop it. The plan is wrong on step 4: the cache must be
> invalidated on write, not on a timer.

After the gate nothing waits for you until the run ends.

### The autonomous run to merge

Approval starts `plan-execute`: per task, tests first, implementation, review
and fixes, then one scope audit over the whole epic and the merge. "Approved
means fully delivered": every approved item is implemented in full, with its
tests and docs, and nothing is left half done. The only acceptable
non-delivery is an external blocker, which the run names and escalates.

### Resume after an escalation

When a run stops on a blocker (a credential, a permission, a human merge),
clear the blocker, then resume.

> Continue the run from docs/plans/in-progress/<epic>/. The token is set now.

### Debug a field issue

Hand over the symptom and the evidence, not a theory.

> Use start-debugging: uploads over 10 MB fail intermittently in production.
> Logs are in the ticket. Find the root cause before proposing a fix, then fix
> it with a regression test.

### Independent review and retrospective

After a batch of work, ask for a review by a different or stronger model than
the one that wrote it, through `workbench:start-review`.

> Review the last three merged epics with a stronger model, against the design
> docs. Findings that are real get fixed; the rest are dropped with a reason.

Then run a workflow retrospective: what stalled, what cost the most, what the
agents got wrong. Turn the result into a new epic, planned and executed like any other work.

### Research and feasibility

For questions with no code change attached, use `workbench:start-research`.

> Is it feasible to move the queue to a managed service? Compare cost,
> migration effort and failure modes. Output a short report in docs/research/.

Research output is a document; acting on it is a new request.

### Parallel epics, background runs, heartbeats

Independent epics run in parallel, each in its own worktree. Long runs go to the
background and report when they exit. Do not poll: use the `heartbeat` skill for
unattended waits, with a bound (10 minutes unless a tool sets less), and act on
the first item that changes state instead of waiting for the whole batch.

### Handoff, resume and compaction

Near the context limit, hand off instead of pushing on.

> Stop after the migration task lands and write a handoff.
> Continue from docs/handoffs/<file>.md.

Use `/compact` at natural boundaries (a task landed, a decision recorded), not
mid-task, and write anything that must survive into the handoff or `MEMORY.md`
first.

### Loops and campaigns

Repeating work (sweeps, audits, bulk migrations) is budgeted by usage. Check
`/usage` between rounds, set a stop threshold in the prompt, and stop at the
threshold with a report of what is covered and what is left.

> Run the dependency audit across all components. Stop when usage passes 70
> percent of the window, and report what is covered.

### Standing approvals and deferral

Grant an approval once, in writing, and the agents honour it without re-asking;
revoke it the same way.

> Standing approval: you may merge green PRs on the default branch. Revoked
> for the infra component as of now.

Record it in `MEMORY.md` with the date. Deferral sticks the same way: what you
deferred stays deferred until you say otherwise.

### Estimates

Ask for and give estimates in agent wall-clock time (how long the agents will
run), not human-days.

### Project skills and wrappers

Deploys and diagnostics go through project skills and wrappers (`mise run ...`,
helper scripts), not raw commands, so every agent runs them the same way and
the steps are reviewed once. If you repeat a procedure, ask for a skill.

### Least destructive first, and commands you run yourself

Where an action has real side effects (deletes, force-pushes, applies,
deploys), the agent takes the least destructive step first (a dry run, a plan,
a read-only check) and escalates only if it fails. Privileged commands you want
to run yourself, such as an apply or a credential change, you run in the
session with the `!` prefix; the output lands in the conversation and the agent
continues from it.

> I will run the apply myself. Tell me the command.

### Model policy

Every subagent gets an explicit model. Research uses the strongest available
reasoning model; implementation uses a mid-tier model, stepping up only for the
hardest tasks; review uses a mid-tier model; mechanical work (lookups,
summaries, merges, monitoring) uses the cheapest that suffices. Very complex
architecture decisions are the one place for the top tier, as a single,
well-framed question with the options laid out.

## Other harnesses

Claude Code needs nothing more. For GitHub Copilot, Cursor, OpenAI Codex,
Gemini CLI and Antigravity, run once:

```sh
mise run harness-setup -- all      # or name targets: copilot cursor codex gemini antigravity
```

Later, plain `mise run harness-setup` re-syncs the `AGENTS.md` generated blocks and
every recorded target.

`harness-setup` generates each harness's config, instructions and skills from the same
sources. See [docs/harnesses.md](docs/harnesses.md) for what each harness gets
and the known differences. Re-run it after changing rules or skills.

### Config-dir migration is repo-wide

Naming exactly one target also moves the workbench config dir to that
harness's directory; that is a repo-wide choice committed for the whole team. Teammates on other
harnesses run `mise run harness-setup -- <their target> --no-migrate` to generate their files
without migrating, and `mise run harness-setup -- claude` switches back.


## Claude Code cloud

Cloud sessions need a one-time environment setup and a single-repository session; pushing to
component submodules needs each component repo attached with `add_repo`. The runbook covers
setup, verification and troubleshooting: [docs/claude-cloud.md](docs/claude-cloud.md).

## Enterprise

Managed Claude Code policy, allowlisted marketplaces, hooks and workflows:
see [docs/enterprise.md](docs/enterprise.md).

The `guard-bash` hook is an advisory guardrail against agent mistakes, not a security
sandbox: it catches common forms (chained commands, `git -C`, `terraform -chdir`,
`bash -c`, `eval`, command substitutions) but a determined or obfuscated command can
evade it. Enterprises that need enforcement should use managed permissions (deny rules)
or a sandbox.

## Conventions

- `AGENTS.md` is the one instruction file; `CLAUDE.md` only imports it.
- `MEMORY.md` records non-obvious decisions, dated, newest last.
- `ROADMAP.md` is human-owned; agents ask rather than invent entries.
- `.claude/rules/` holds per-language rules (python, go, terraform, helm ...).
- `docs/plans/` holds plans and tickets, `docs/research/` research output,
  `docs/handoffs/` context handoffs.
- Before working in a component, read its `AGENTS.md`, `MEMORY.md` and
  `ROADMAP.md`.

## Maintaining the template

Develop the template in an uninitialised clone; `mise run test` and
`mise run lint` must be green before a change lands. The `workbench:rules`
block in `AGENTS.md` is generated from `plugins/workbench/rules/`: change the
rule files, then rebuild with `mise run harness-setup`, never edit the block by
hand.

## Pulling template updates

To pull template updates into an initialised project, run the `template-update`
skill (`workbench:template-update` in Claude Code), or the engine directly:

```sh
mise run init:update                  # template from the default URL, branch main
mise run init:update -- --url <url> --branch <branch> --base <sha>
```

`--url` and `--branch` pick another template source; `--base` names the template
commit your workbench was created from.

What arrives: tooling (`scripts/`, `mise-tasks/`, `.mise.toml`), plugins, rules
and the generated blocks. What never arrives: the template's plans and records,
`contracts.json`, `index.json`, `facts.md`, research, handoffs, and the entries
in your `MEMORY.md` and `ROADMAP.md`. Those stay yours.

The update refuses, and changes nothing, when:

- the workbench is uninitialised;
- the working tree is dirty;
- the merge conflicts outside the known template-owned paths (the message gives
  the manual route: `git remote add template`, `git fetch`, `git merge`, resolve,
  then `mise run init:refresh`);
- lint fails after the merge.

When your history shares no commit with the template (a workbench created by
copying files), pass `--base` so the update can graft the two histories.

It ends in one local merge commit, which is the record of the update, and never
pushes. Run `mise run doctor` afterwards to check the result.

## Verification and CI

```sh
mise run test    # every test family
mise run lint    # every lint family
```

Lint includes the personal-reference lint, which fails on names, hosts or paths
that belong to one person, and secret scanning. The template is public, so
gitleaks runs in three places: as a pre-commit hook, in `mise run lint`
(`lint:secrets` over the tracked tree), and as the CI `secrets` job over the
full tree and history. There is no allowlist; secret-shaped test fixtures are
built at run time.

The GitHub workflow `.github/workflows/ci.yml` runs the `check` job (test and
lint) and the `secrets` job on every push.

## Worktree caveat

The plugin loads from the main checkout, not from a git worktree. Edits to
`plugins/` made in a task or integration tree take effect in Claude Code only
after the epic merges to main and you run `/reload-plugins`.

## License

See [LICENSE](LICENSE).
