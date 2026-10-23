---
name: pullrequests-watch
description: "Wait for code/ submodule PR (GitHub) or MR (GitLab) pipelines and report success, failure with log tail, or 'no pipeline launched'; --no-wait gives a status and mergeability snapshot. Host picked per repo from origin, so one run can mix GitHub and GitLab. Use to watch a PR/MR pipeline or find why a PR/MR is blocked."
---

# pullrequests-watch

By default, **watches** each target PR or MR: blocks until its pipeline reaches a
terminal state, then reports. Reports a passing pipeline the moment it goes
green, captures the failed check or job name plus a bounded log tail on failure,
and flags `no pipeline launched` if nothing registers within the grace window.
`--no-wait` skips waiting and prints a single snapshot of state, checks, and
mergeability. The script never invents interpretation - it emits a structured
report that you then narrate.

## Host per repo

Each repo's host comes from its `origin` url (`skills/_lib/scm.py`,
`detect_host`), checked in this order:

1. `WORKBENCH_SCM_HOSTS`, a comma-separated host map of `hostname=github` or
   `hostname=gitlab` entries (e.g. `git.corp.example=gitlab,ghe.example=github`),
   matched case-insensitively on the exact hostname. Use it for self-hosted
   instances.
2. Otherwise a hostname containing `github` is GitHub (gh), one containing
   `gitlab` is GitLab (glab).
3. Otherwise, or with no origin, the repo is **local-only**: it is reported
   `skipped` with `local-only: no PR/MR host for <origin>` and does not fail the
   run.

A malformed map entry (no `=`, or a value other than `github`/`gitlab`) is a
usage error naming the entry: the script exits 2 before polling anything.

## Procedure

1. Pick the input form based on what the user gave you:
   - Explicit repo/number pairs (a PR number on GitHub, an MR iid on GitLab):
     ```
     mise exec -- python3 ${CLAUDE_PLUGIN_ROOT}/skills/pullrequests-watch/scripts/watch.py --prs repo1#123,repo2#45
     ```
   - A branch name, resolved to the open PR/MR per repo:
     ```
     mise exec -- python3 ${CLAUDE_PLUGIN_ROOT}/skills/pullrequests-watch/scripts/watch.py --branch <branch> --repos repo1,repo2
     ```
   - After a merge, the **deploy** pipeline (GitHub Actions or GitLab), with `--deploy`:
     ```
     mise exec -- python3 ${CLAUDE_PLUGIN_ROOT}/skills/pullrequests-watch/scripts/watch.py --deploy --prs repo1#123
     mise exec -- python3 ${CLAUDE_PLUGIN_ROOT}/skills/pullrequests-watch/scripts/watch.py --deploy --repos repo1,repo2 [--branch main] [--sha repo1=SHA,repo2=SHA]
     ```
   These two forms are mutually exclusive - pass `--prs` alone, or `--branch`
   together with `--repos`, never a mix. Either form may name GitHub and GitLab
   repos in the same run.
2. Default runs in **watch** mode: it polls every `--interval` seconds (default
   10s) until every target resolves or `--timeout` (default 1200s / 20m) fires.
   A target showing no checks is held pending until `--grace` seconds (default
   30s) pass; if still none, it resolves to `no pipeline launched`. Pass
   `--no-wait` for a single immediate snapshot instead.
3. Watching is edge-triggered per target, not batch-gated: the instant a target
   resolves it prints one line to stdout, before the other targets in the same
   call have resolved and before the final table prints:
   ```
   RESOLVED <repo> <status> <ref>: state=<S> checks=<C> mergeable=<M> url=<U> waited=<N> [reasons: ...]
   ```
   `<ref>` is `pr #N` on GitHub and `mr !N` on GitLab. Act on each `RESOLVED`
   line as it arrives - if it says `ok` and mergeable, merge that PR/MR then, do
   not hold it waiting for its siblings.
4. Read the report table (`REPO`, `STATUS`, `DETAIL`) printed to stdout once
   every target has resolved. Each row's detail is `<ref>: ` followed by
   `state=` (`OPEN`, `MERGED`, `CLOSED`), `checks=` (`passed`, `failed`,
   `pending`, `none`), `mergeable=` (`mergeable`, `conflict`, `unknown`,
   `blocked`), `reviews=<N> unresolved` (right after `mergeable=`, open PRs/MRs only),
   `url=`, in watch mode `waited=` (seconds spent before resolving),
   and when relevant `reasons:` joined by ` ; `:
   - `checks=failed` - `check '<name>' failed:` (GitHub) or `job '<name>'
     failed:` (GitLab) and up to 100 lines of its log tail.
   - `mergeable=conflict` - GitHub lists the files (`merge conflict in: a.py,
     b.py`); GitLab has no per-file conflict API, so it reports `merge conflict
     (detailed_merge_status=conflict)`.
   - `mergeable=blocked` - the raw host field: `not mergeable
     (mergeStateStatus=BEHIND|BLOCKED|DRAFT|...)` on GitHub, `not mergeable
     (detailed_merge_status=ci_must_pass|discussions_not_resolved|not_approved|...)`
     on GitLab.
   - `<N> unresolved review discussion(s)` - when N > 0 on an open PR/MR
     (singular for 1). A flag, never a gate: it does not flip `STATUS` to
     `failed`; whether to merge past it is your call.
   - `no pipeline launched` - no checks registered within the grace window.
   - `timed out after <N>s` - still pending when the overall timeout fired.
5. Narrate the report in plain language: which PRs/MRs went green and are
   mergeable, which had no pipeline, which repos were skipped as local-only,
   and - for anything failed or blocked - the specific reason from `reasons:`,
   not just "not mergeable". `mergeStateStatus=DIRTY`, `CONFLICTING`, or
   `BEHIND` is actionable by you right now (rebase the branch) - it is not a
   state to wait out.
6. `STATUS` is `ok` for merged/closed targets, for `no pipeline launched`, and
   for open targets that are neither failing checks nor conflicted (including
   pending in `--no-wait` mode). `STATUS` is `skipped` for a local-only repo.
   `STATUS` is `failed` for an unreachable PR/MR or branch, an unknown repo, a
   timeout, or an open target failing checks or conflicted. The script exits 1
   iff any repo is `failed`, and 2 on a usage error (bad flags, a malformed
   `--prs` entry or host map) - treat 1 as a signal that something needs
   attention, not a script bug.

## GitLab review discussions

On GitLab, every poll (watch and `--no-wait`) also reads the MR's discussions
(`glab api projects/<slug>/merge_requests/<iid>/discussions?per_page=100`,
through `scm.GitLab.discussions`). A discussion is unresolved when it holds a
non-system note that is resolvable and not resolved; each discussion counts
once. That count is the `reviews=<N> unresolved` field and the
`<N> unresolved review discussion(s)` reason.

While waiting, each new non-system note is printed once, as it appears:

```
[<repo> !<iid>] new review comment from <author>: <body, 200 chars max>
```

Notes already seen on an earlier poll are not repeated, and system notes
(`changed the description`) are never shown. Reviews are informational: if the
discussions endpoint errors, the poll degrades to zero reviews and still
reports checks and mergeability.

## GitHub review threads

Every GitHub call is `gh api repos/{owner}/{repo}/...` REST (PR via `pulls/<n>`,
checks via `commits/<sha>/check-runs` and `/status`), never `gh pr`, `gh repo view`
or `gh label`: those are GraphQL, which Claude Code cloud sessions block (HTTP 403).

On GitHub, every poll (watch and `--no-wait`) of an OPEN PR also counts its
unresolved review threads through `scm.GitHub.unresolved_reviews`: one
`gh api graphql` call over `reviewThreads` (first 100, further calls only while
`pageInfo.hasNextPage`), counting every thread with `isResolved: false`,
outdated ones included (as GitLab does). In a Claude Code cloud session
(`CLAUDE_CODE_REMOTE=true`) the count comes from the proxy's REST route
`gh api repos/{owner}/{repo}/pulls/<n>/ccr/review_threads` instead. The count is the same
`reviews=<N> unresolved` field and `<N> unresolved review discussion(s)` reason
as GitLab, and likewise never flips `STATUS` to `failed`, so the merge gate
`reviews=0` holds on both hosts. A merged or closed PR emits no `reviews=`
field, and if the graphql call errors the field is omitted while checks and
mergeability still report.

## Post-merge deploy watch (`--deploy`, `--sha`)

`--deploy` watches what runs after a merge instead of the PR/MR head checks, on
both hosts. It takes `--prs` or `--repos`, never `--branch/--repos` mixed with `--prs`.

- `--deploy --prs repo#N`: resolves the merged PR/MR to its target branch and merge
  commit (`resolve_deploy`) and watches that sha. An unmerged one fails with
  `pr #N not merged yet - deploy-watch is a post-merge action` (GitHub) or
  `mr !N not merged yet (no merge_commit_sha) - deploy-watch is a post-merge action`
  (GitLab). Detail prefix: `pr #N deploy:` / `mr !N deploy:`.
- `--deploy --repos a,b [--branch B]`: watches branch `--branch`, default the
  repo's `.gitmodules` branch (`common.tracked_branch`); on GitHub the repo's
  default branch (`gh api repos/<o>/<r>`, `.default_branch`) only when no
  `.gitmodules` branch is set. Detail prefix: `deploy:`.
- `--sha repo1=SHA,repo2=SHA`: pins a repo to a commit (only with
  `--deploy --repos`; every repo named must be in `--repos`). Using `--sha`
  without `--deploy`, or with `--prs`, or naming a repo not in `--repos`, exits 2.

**GitLab:** the newest pipeline on the branch (pinned to the sha when given).
**GitHub:** without `--sha`, the branch tip sha is pinned from
`gh api repos/<o>/<r>/branches/<b>`; then every push-event Actions run of that
sha is polled (`gh run list --branch <b> --commit <sha> --event push --json
databaseId,status,conclusion,headSha,url,workflowName`), keeping runs whose
`headSha` equals it. Per run: failed = conclusion `failure`, `cancelled`,
`timed_out`, `action_required`, `startup_failure`; pending = status `queued`,
`in_progress`, `waiting`, `requested`, `pending`, `stale` (keep polling for a
newer run); passed = conclusion `success`, `skipped`, `neutral`. Reduced: any
failed -> `failed`, any pending -> `pending`, all passed -> `passed`, no runs ->
`none` (`no pipeline launched` after `--grace`).

Detail is `branch=<B> sha=<first 8> status=<passed|failed|pending|none> url=<U>`
plus `waited=<N>` in watch mode and `reasons:`: `job '<name>' failed:` (GitLab) or
`run '<workflowName>' failed:` (GitHub, with the `gh run view <id> --log-failed`
tail) with up to 100 log lines per failure, `no pipeline launched` (nothing
registered within `--grace`), or `timed out after <N>s`. `--no-wait` gives one
snapshot. Deploy runs print the same `RESOLVED` line per target.
