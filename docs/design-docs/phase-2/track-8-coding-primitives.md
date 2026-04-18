# Track 8 Design — Coding-Agent Primitives

**Status: Proposed — design complete, implementation plan TBD.**

Relocated from Agent Capabilities Track 3 on 2026-04-18. Coding primitives are platform surface area, not a cross-cutting capability add-on, and belong alongside the other Phase 2 runtime tracks.

## Context

Extends the sandbox tool surface so agents doing real iterative coding (edit → run test → read stack trace → edit again) can work without blowing through the context window on every turn. Agent Capabilities Track 2 shipped tools sufficient to "run a script"; Track 8 is what makes "iterate on a codebase" practical. Depends on Agent Capabilities Track 2 (E2B Sandbox & File Input).

**Scope boundary:** Strictly in-sandbox tooling. Phase 2 Track 6 (GitHub Integration) owns the repo ingress/egress story — `git clone` at task start, branch push, PR creation. Track 8 does not duplicate any of that; agents can still `sandbox_exec("git clone ...")` in the meantime.

## Motivation

Observed gaps with the Track 2 tool set, each exercised many times per coding turn:

1. **No surgical edit.** Every file change requires `sandbox_write_file` with the full new contents. For a 2k-line file, that's ~8k tokens per change, and long edit sessions drift — the LLM silently mutates unrelated code.
2. **No partial file read.** `sandbox_read_file` returns the whole file. Reading a 10k-line file to locate one stack frame costs ~40k tokens.
3. **No first-class search.** Agents fall back to `sandbox_exec("rg ...")`, which wraps the search in shell scaffolding, loses structured output, and has no cap on how much ripgrep can dump into the context.
4. **No background processes.** `sandbox_exec` is blocking. Agents can't start a dev server and then run tests against it, tail logs while work continues, or run a long-running suite without holding the tool slot.
5. **No output truncation.** A failing webpack build emits 50 MB of stderr. One bad run can evict the entire recent context.

Each of these is individually cheap to close; the impact is multiplicative because coding agents hit every one of them within a single turn.

## New tools

| Tool | Description |
|------|-------------|
| `sandbox_edit` | Exact-string replacement in a file. Inputs: `path`, `old_string`, `new_string`, optional `replace_all` (default false). Fails when `old_string` is non-unique and `replace_all` is false, when `old_string` is absent, or when `path` doesn't exist. Mirrors Claude Code's Edit. |
| `sandbox_grep` | Ripgrep-backed content search. Inputs: `pattern`, optional `path`, `glob`, `output_mode` (one of `files_with_matches` \| `content` \| `count`; default `files_with_matches`), `head_limit` (default ~250). Output always capped by `head_limit`. |
| `sandbox_glob` | Glob-style file path match, sorted by mtime (most recent first). Inputs: `pattern`, optional `path`, `head_limit`. |
| `sandbox_process_read` | Read accumulated stdout/stderr from a backgrounded process by `process_id`. Returns new output since the last read and the process's current running/exited state. Output is truncated to a per-call byte cap. |
| `sandbox_process_kill` | Terminate a backgrounded process by `process_id`. |

## Modified tools

| Tool | Change |
|------|--------|
| `sandbox_exec` | Add optional `run_in_background` (default false). When true, returns `{process_id, started_at}` immediately instead of blocking; agent retrieves output via `sandbox_process_read`. Add `max_output_bytes` with head/tail/mixed truncation so one noisy command can't destroy the context. |
| `sandbox_read_file` | Add optional `offset` (1-based line number, default 1) and `limit` (lines, default whole file but capped at a configurable maximum). Response includes total line count so the agent knows when more remains. |

## Explicitly out of scope

- **`git clone` at task start / GitHub PR tool** — owned by Phase 2 Track 6.
- **`apply_patch` (multi-file diff apply)** — redundant once `sandbox_edit` ships. Revisit only if agents show need for multi-file atomic edits.
- **Todo/plan tracking tool** — handled separately by **Phase 2 Track 9 (Planning Primitive)**, which is a cross-cutting primitive not coupled to the sandbox.
- **HTTP/fetch tool with POST/PUT/DELETE** — `sandbox_exec("curl ...")` is adequate. Upgrade only if logs show it's a hot path.
- **Interactive stdin / TTY / debugger attach** — E2B's process API doesn't cleanly support interactive sessions. Out of scope; revisit if needed.

## Task sketch

| # | Task | Service | Description |
|---|------|---------|-------------|
| 1 | `sandbox_edit` tool | Worker | String-replace with uniqueness check, via `sbx.files.read` / `sbx.files.write` |
| 2 | `sandbox_read_file` offset/limit | Worker | Extend Track 2 tool with line-range params |
| 3 | `sandbox_grep` tool | Worker | Wrap `rg` in the sandbox with structured output and `head_limit` truncation |
| 4 | `sandbox_glob` tool | Worker | Wrap glob + stat, sort by mtime |
| 5 | `sandbox_exec` output truncation | Worker | Add `max_output_bytes` with head/tail/mixed modes |
| 6 | `sandbox_exec` background mode | Worker | Add `run_in_background`; return `process_id`; integrate with E2B process API |
| 7 | `sandbox_process_read` + `sandbox_process_kill` | Worker | Tools that reattach to backgrounded processes by `process_id` |
| 8 | Sandbox template: install `ripgrep` | Infrastructure | Add `rg` to the platform's E2B template so `sandbox_grep` can rely on it |
| 9 | Crash-recovery test for backgrounded processes | Cross-service | Verify that on worker crash + reconnect, an agent can still read output from a process launched before the crash (E2B keeps it alive) |
| 10 | Integration tests | Cross-service | End-to-end coding loop: clone (via exec), edit, grep, run tests, read output |

## Crash-recovery implications

Background processes introduce per-sandbox state **not** captured in Postgres checkpoints:

- The E2B sandbox persists across worker crashes, and processes inside it keep running. On reconnect, the worker re-attaches to the sandbox by `sandbox_id` (already implemented in Agent Capabilities Track 2) and to individual processes by their E2B-assigned `process_id`.
- If the sandbox itself was destroyed (TTL expired during outage), the backgrounded processes die with it — same failure mode as the Track 2 sandbox-loss path; the task dead-letters with `sandbox_lost`.
- **Idempotency:** if a checkpoint is re-executed after a crash (Phase 1 allows re-running an interrupted in-flight node), an agent's `sandbox_exec(..., run_in_background=True)` call could double-launch the same process. This is the same non-idempotent-tool concern Phase 1 already flags; `sandbox_exec` is already non-idempotent, and background mode does not make it worse. A follow-up in Phase 3 non-idempotent-tool guards would address it alongside the existing foreground case.

## Template dependency

Ripgrep is not in every E2B template. Options:

- **(A) Pre-install `rg` in the platform's custom E2B template.** Simple, matches the Claude-Code-cloud pattern of pre-baked templates. Preferred.
- **(B) Auto-fallback to `grep -r` when `rg` is absent.** Keeps the tool usable on third-party templates at the cost of worse output quality and performance.

Track 8 adopts (A) as the default; (B) is a considered fallback if customers pin arbitrary templates.
