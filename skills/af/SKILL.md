---
name: af
description: Chain Matt Pocock skills in a dedicated Git worktree with explicit test, review, and commit gates.
---

# af

Use af-lite to run an ordered Matt Pocock skill flow. The skills and worker CLIs are installed separately. af-lite creates the sibling worktree, records steps, selects optional `pi` or `codex` workers or a host handoff, runs configured tests, scans host commits, and checks the final decision.

## Start a flow

1. Check prerequisites and the repository:

   ```sh
   af doctor --repo /path/to/repository
   ```

   Doctor checks the ten flow skills: `grill-with-docs`, `to-spec`, `to-tickets`, `implement-spec`, `implement`, `tdd`, `code-review`, `diagnosing-bugs`, `pr`, and `retro`. It warns if `docs/agents/issue-tracker.md` or `docs/agents/domain.md` is missing.

2. Create a unique flow ID. Keep the returned sibling worktree path and `base_sha`:

   ```sh
   af worktree create --repo /path/to/repository --flow-id <id>
   ```

3. Start a flow from its worktree using the chosen ordered skills:

   ```sh
   af state start --repo /path/to/repository-<id> --flow-id <id> --base-sha <base_sha> --chain <skill> [<skill> ...]
   ```

   Suggested chains: feature `grill-with-docs to-spec to-tickets code-review pr retro`; bug `diagnosing-bugs tdd code-review pr retro`; small `tdd code-review pr retro` or `implement code-review pr retro`.

## Run ordered skills

Advance each step before running it. Follow interactive skills in the host. A writer can be dispatched when a configured candidate is available. Supply `--writer-company` when selecting or dispatching reviewers.

```sh
af state advance --repo /path/to/repository-<id> --flow-id <id>
af select --repo /path/to/repository-<id> --flow-id <id> --role writer
af dispatch --worktree /path/to/repository-<id> --flow-id <id> --role writer --prompt "<task>"
```

Dispatch passes a fixed writer or read-only reviewer role preamble and the prompt to the worker on stdin. stdout and stderr are logged under `<git-common-dir>/.af/logs/<id>/`; use the returned log paths to follow output. A timeout kills the process group. Failover occurs only when HEAD and `git status --porcelain=v1 -z --ignored` are unchanged. If a worker creates commits, dispatch soft-resets them to the starting commit and reports their SHAs.

Claude Code adapter: when dispatch returns `via: host`, start a fresh subagent with the selected `model` using the `Agent` tool, set `worktree` as its working directory, and pass the returned `prompt`.

For a ticket flow, insert one step per ticket after `to-tickets`, then run them in order:

```sh
af state add-steps --repo /path/to/repository-<id> --flow-id <id> --after to-tickets --steps implement-spec:T1 implement-spec:T2
```

## Test, commit, and review

Run focused tests after each writer step. The host commits only explicit changed paths, on branch `af/<id>`:

```sh
af gate test --repo /path/to/repository-<id> --flow-id <id> --suite focused
af gate commit --repo /path/to/repository-<id> --flow-id <id> --message "<step summary>" --paths <changed-file> [<changed-file> ...]
af state complete --repo /path/to/repository-<id> --flow-id <id> --result completed
```

After implementation, run `code-review` against `base_sha...HEAD`. Collect findings and fix them in one writer run; ordinary repair rounds need focused tests but no repeated review. Changes matching configured high-risk paths need two reviews on every round. Run the full test suite and review any final repair diff before the gate decision.

Reviewer selection prefers a different company when the role has any configured candidate from another company. Same-company review is allowed only when every candidate for that role has the writer's company. If all independent candidates stop for quota, dispatch returns `waiting_for_quota` and exits 75; wait `retry_after_minutes` and retry. A missing `pi` or `codex` CLI is skipped; `host` candidates are always available.

```sh
af select --repo /path/to/repository-<id> --flow-id <id> --role small_review --writer-company <writer-company>
af dispatch --worktree /path/to/repository-<id> --flow-id <id> --role small_review --writer-company <writer-company> --prompt "Review the flow changes."
```

```sh
af gate test --repo /path/to/repository-<id> --flow-id <id> --suite full
af gate decide --repo /path/to/repository-<id> --flow-id <id> --tests pass --reviews pass
```

Only continue to the project's normal PR process when the decision is `merge`. A high-risk path or disabled `auto_merge` returns `ask`; missing config, failed checks, or a dirty worktree returns `blocked`.

`gate decide` exits 2 for every `blocked` decision and 0 for `merge` or `ask`.

## Config and cleanup

Config resolves from `--config`, `$AF_CONFIG`, the primary checkout's `af.config.json`, then `~/.config/af/af.config.json`. Any resolved config path inside a linked worktree on an `af/*` branch is refused. `state start` records the resolved config path.

```sh
af state status --repo /path/to/repository-<id> --flow-id <id>
af state stop --repo /path/to/repository-<id> --flow-id <id>
af worktree remove --repo /path/to/repository --flow-id <id>
af cleanup --repo /path/to/repository
```

Worktree removal refuses dirty trees and commits not merged into the recorded base; use `--force` only after reviewing the work. Cleanup archives or deletes only completed or stopped records and their logs according to `record_retention`.
