# af-lite specification

af-lite chains separately installed [Matt Pocock skills](https://github.com/mattpocock/skills) using POSIX bash and Python 3 standard library. It owns flow worktrees and records, candidate selection and dispatch, test and commit gates, cleanup, and a merge decision. It does not implement the skills or replace project CI and release processes.

## Threat model

Workers are trusted but fallible: they are the owner's coding agents working in the owner's repositories. af-lite protects against mistakes: work outside the flow worktree, unintended commits, secrets in commits, same-company reviews, untested or high-risk merges, and lost work during failover. Deliberately malicious-worker defenses are out of scope, including hook injection, symlink escapes, FIFO/socket traps, and crafted encodings. Worker CLI sandboxes and human review cover that threat.

## Flow and worktree

- Each flow has a validated ID and an ordered chain of skill steps. State is one JSON record per flow under `<git-common-dir>/.af/flows/`; each record stores the resolved config path.
- `state start` requires the flow worktree top level on branch `af/<id>` and refuses to create a record otherwise.
- A running step cannot be advanced or resumed by a second runner until `stale_after_minutes` elapses. `advance` claims the next step, `complete` records its result and moves forward, `resume` releases stale claims, `stop` ends a flow, and `add-steps` inserts pending steps after a named step.
- `worktree create` makes a sibling worktree on branch `af/<id>` without modifying the primary checkout. `remove` refuses tracked changes, untracked non-ignored files, and branches not merged into the base recorded at creation unless `--force` is supplied.
- Config resolution is `--config`, `$AF_CONFIG`, the primary checkout's `af.config.json`, then `~/.config/af/af.config.json`. Any resolved config path inside a linked worktree on an `af/*` branch is refused. Flow commands use the recorded path.

## Selection and dispatch

- Each role has ordered `pi`, `codex`, `claude`, or `host` candidates. Candidates with a missing `pi`, `codex`, or `claude` CLI are skipped; a `host` candidate with `model` and `company` is always available.
- `dispatch` takes `--worktree`, with `--repo` accepted as an alias for consistency with the other commands; passing both with different values is an error. It always uses the config recorded by `state start` and rejects `--config`.
- `AF_NO_HOST=1` makes `host` candidates unavailable so a non-Claude host, or a host without quota for the configured host model, falls through to the next `pi`/`codex`/`claude` candidate.
- Reviewer roles require a known company different from the writer's when the role has any candidate from a different company. Same-company review is allowed only when every configured candidate has the writer's company. If every independent reviewer is quota-exhausted, dispatch emits `waiting_for_quota` and exits 75.
- A selected `host` candidate starts no process. `dispatch` returns JSON with `via`, `role`, `model`, `company`, `worktree`, the prompt with its `ROLE` or `REVIEWER` preamble, the instruction to start a fresh host subagent, and `attempts`. Quota failover may select a host candidate unless `AF_NO_HOST=1`.
- Every CLI worker receives a fixed role preamble and the task on stdin. Codex runs as `codex exec -p <profile> -s workspace-write -C <worktree> -`; Claude runs as `claude -p --model <model>` with optional `--tools ... --permission-mode acceptEdits --strict-mcp-config`; Pi and Claude also receive the prompt on stdin. stdout and stderr stream to `.af/logs/<flow-id>/` under the Git common directory. Timeout kills the worker process group.
- Quota stops are configured exit statuses or case-insensitive stderr patterns after removing the prompt. Failover is allowed only when both `HEAD` and `git status --porcelain=v1 -z --ignored` are unchanged. A worker-created commit is soft-reset to the attempt's starting commit and reported.

## Gates

- `gate test` runs the configured focused or full command in the flow worktree with a timeout.
- `gate commit` requires branch `af/<id>` and explicit changed file paths. It rejects absolute paths, `..` path components, absolute symlink targets, and symlinks that resolve outside the worktree. Relative symlink targets may contain `..` when they resolve inside the worktree. It snapshots and restores the index if checks refuse the commit. Secret detection uses `gitleaks protect --staged` when installed; otherwise it checks added lines with the built-in regex set.
- `gate decide` requires passing tests and reviews, a clean worktree, and a base commit. High-risk patterns are checked with `git check-ignore --no-index` in a throwaway repository initialized with `git init --template=` and a created `.git/info` directory. High-risk paths or `auto_merge: false` return `ask`; missing prerequisites return `blocked`; otherwise the result is `merge`. `blocked` exits 2; `merge` and `ask` exit 0.

## Configuration

Unknown fields are rejected. Supported fields are:

| Field | Use |
|---|---|
| `roles` | Ordered candidates. Candidate keys: `via`, `company`, and `provider` plus `model` for Pi, `profile` for Codex, or `model` for Claude and host. Pi also accepts optional `tools` and `env_allow`; Claude accepts the same, with `tools` passed as `--tools ... --permission-mode acceptEdits --strict-mcp-config`. |
| `quota_stop.exit_statuses`, `quota_stop.patterns` | Quota exit codes and stderr regex patterns. |
| `quota_retry_minutes` | Retry delay reported when eligible candidates are quota-exhausted. |
| `worker_timeout_minutes`, `test_timeout_minutes` | Worker and test timeouts. |
| `stale_after_minutes` | Age after which a running step can be claimed again. |
| `skill_dirs` | Roots containing required Matt skill folders. |
| `auto_merge` | Whether an otherwise clear decision may return `merge`. |
| `high_risk_paths` | Git ignore patterns checked against committed paths from base through flow branch. |
| `tests.focused`, `tests.full` | Shell command strings. |
| `record_retention.archive_after_days`, `record_retention.archive_dir`, `record_retention.delete_after_days` | Archive and deletion ages and destination for completed or stopped flow records and logs. |

For Pi candidates, `tools` is a non-empty tool list passed with MCP disabled. Without a shell tool, the worker cannot run `wrangler`, `gh`, `git`, or `curl`; Pi's `write` and `edit` tools are not path-confined. On Claude candidates, `tools` removes the shell tool (the CLI is started with `--tools`, `--permission-mode acceptEdits` and `--strict-mcp-config`), but af passes no flag that turns off user or project settings or hooks, so hooks and settings `env` entries still apply, and the file tools are not confined to the worktree. `env_allow` may be empty; it keeps standard runtime, locale, proxy, and CA variables plus listed names set in the parent. It removes tokens from the environment, not credential files on disk or Pi's own credential file.

Defaults and field details are in the root [README](../README.md) and [example config](../af.config.example.json).

## Cleanup and doctor

`cleanup` archives or deletes only completed or stopped flow records and their logs after configured ages. The README shows how to schedule cleanup with cron or launchd. `doctor` checks Git, supported config, required Matt skills by folder name, optional CLIs, and warns when setup-matt-pocock-skills documents are missing.

## Commands and validation

Commands: `doctor`; `worktree create|remove`; `state start|advance|complete|stop|status|resume|add-steps`; `select`; `dispatch`; `gate test|commit|decide`; and `cleanup`. Run the offline suite with `bash tests/run.sh`.
