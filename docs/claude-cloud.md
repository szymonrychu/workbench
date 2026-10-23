# Runbook: the workbench in Claude Code cloud sessions

How to set up a Claude Code cloud environment (claude.ai/code, the Claude app, `claude --cloud`)
so a workbench works there: the workbench plugins load, the pinned tools install, the PR skills
reach GitHub and the agent can push to the component submodules.

## Why a cloud session needs this

A cloud session runs on a fresh VM behind two proxies. Four things differ from a local run:

| Local behaviour | Cloud behaviour | What fixes it |
|---|---|---|
| `.claude/settings.json` enables the workbench plugins after the trust dialog; `/reload-plugins` picks up changes | Plugins a repo enables are never installed, there is no trust dialog and no `/reload-plugins` | The SessionStart hook installs them at user scope; a plugin installed by a SessionStart hook loads in that same session |
| `mise install` fetches from GitHub releases and verifies attestations through sigstore | `tuf-repo-cdn.sigstore.dev` is blocked, and `github.com/<o>/<r>/archive` and `codeload.github.com` are refused for repositories not attached to the session | The hook writes `~/.config/mise/conf.d/workbench-cloud.toml`: no aqua attestation check (release checksums are still verified) and bats from npm |
| `git push` uses your own credentials for any repo | The git proxy only credentials repositories attached to the session, so pushing in `code/<component>` is refused | The agent attaches each component repo with the `add_repo` tool; the hook lists the exact calls |
| `gh pr ...` works | GitHub GraphQL is blocked, so `gh pr`, `gh issue`, `gh repo view --json` fail | The `pullrequests-*` skills use `gh api repos/{owner}/{repo}/...` REST; review threads come from the proxy's `pulls/{n}/ccr/review_threads` route |

The hook is `scripts/cloud_session_start.py`, registered in `.claude/settings.json` for
`startup` and `resume`. It does nothing unless `CLAUDE_CODE_REMOTE=true`, so local sessions are
unchanged. It never fails the session: problems are reported to the agent as
`Setup problem: ...` lines in the session-start context.

## One-time setup

1. **GitHub access.** Connect GitHub at claude.ai (Settings, Connectors, or
   https://claude.ai/connect-github) and install the Claude GitHub App on the workbench repo
   and on every component repo under `code/`. `add_repo` can only attach a repository the
   app (or your `/web-setup` token) can reach; private submodules also need it to check out.
2. **Cloud environment.** Open the environment menu in a session's title bar, then Edit (or
   create a dedicated environment for the project):
   - Network access: **Trusted** is enough for the workbench itself. Add every host your
     components' toolchains download from that the Trusted list does not cover, with
     **Custom** access. Known example: `dl.google.com` for a Go toolchain installed by mise
     (the template's own end-to-end test scaffolds a Go component and needs it).
   - Environment variables: none are required. Do not put tokens here; use network
     secrets for API keys components need.
   - Setup script: not required. The hook does the work on every start and is fast once the
     environment cache holds the tools.
3. **Default branch.** The hook must be on the branch the session starts from. For an
   instance created before this runbook, pull the template update (`workbench:template-update`)
   or copy `scripts/cloud_session_start.py` and the `hooks` block of `.claude/settings.json`.

## Starting a session

- Select **exactly one repository: the workbench repo.** A cloud session with several
  repositories does not run any repository's hooks, so the plugins would not load. Do not add
  the component repos at session start; the agent attaches them mid-session (next section),
  which keeps the hook.
- Cloud threads inside a claude.ai *project* do not run repository hooks either; use a plain
  session.
- Check it worked: ask "which workbench skills do you have?". The answer lists
  `workbench:start-development`, `workbench:plan-execute` and the rest. The session-start
  context also names each submodule's `add_repo` call.

## Working in components (submodules)

- The hook checks out every submodule that is not checked out yet; it never touches one that
  is, so resuming a session does not move a submodule off the branch the agent was on.
- Before pushing in `code/<component>` (or checking out a private one), the agent calls
  `add_repo(owner=..., repo=..., access="push")` once per repository. Approve the prompt.
  The agent keeps working in `code/<component>`; it does not clone the repo a second time.
- If `add_repo` is refused, the Claude GitHub App is not installed on that repository, or
  your organisation has not enabled it for Claude; fix step 1 and retry.
- A private submodule the hook could not check out is reported in the context with the
  `git submodule update --init --recursive -- <path>` to run after `add_repo`.

## Pull requests

- `gh pr ...` and other GraphQL-backed `gh` subcommands fail in the cloud by design. The
  `pullrequests-*` skills and the `scm-shepherd` agent use REST (`gh api repos/...`).
- Review threads: `GET repos/{owner}/{repo}/pulls/{n}/ccr/review_threads`; resolve with
  `POST .../pulls/{n}/ccr/comments/{comment_id}/resolve`; auto-merge with
  `PUT|DELETE .../pulls/{n}/ccr/auto_merge`. These routes exist only behind the cloud proxy.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No `workbench:*` skills | Session has several repositories, is a project thread, or the hook is not on the starting branch | Start a single-repo session from a branch that has the hook; read the `Setup problem:` lines |
| `Setup problem: plugin ...` | The marketplace in `.claude-plugin/marketplace.json` and `enabledPlugins` disagree | `mise run doctor` locally, fix, push, start a new session |
| `Setup problem: mise install: ...` | A tool download host is blocked | Add the host from the error to the environment's Custom network access |
| `access denied by the git proxy: <repo> is not in this session's authorized repository set` | Repo not attached | `add_repo(..., access="push")` for that repo |
| `HTTP 403: GitHub GraphQL is not available from Claude Code sessions` | A raw `gh pr` command | Use the skill, or `gh api repos/...` |
| Changed a plugin and want it live | No `/reload-plugins` in the cloud | Commit, push, start a new session |
