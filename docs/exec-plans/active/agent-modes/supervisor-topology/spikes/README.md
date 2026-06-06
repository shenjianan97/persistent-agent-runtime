# Supervisor Topology — Verification Spikes

Throwaway harnesses that settled the durability / cost / delegation model for the
[Supervisor Topology track](../plan.md) (results recorded in plan **§A11**, design
decisions log **2026-06-05**). They are **not shipped code** — they exist as
evidence and as **regression-test seeds** for tasks S3/S4/S6/S8/S11.

All verified against the pinned `langgraph==1.0.5` (worker venv). Run with:

```bash
services/worker-service/.venv/bin/python docs/exec-plans/active/agent-modes/supervisor-topology/spikes/<spike>.py
```

| Spike | Question settled | Verdict |
|---|---|---|
| `spike1_cost_interrupt_resume.py` | Is sub-agent usage visible to the cost loop's `event["agent"]` gate? Do 2 interrupts in one `Send` super-step break scalar resume? Does a completed sibling survive a pause? | E1 real (invisible); E3 real (`RuntimeError`); E2 resume-forward holds |
| `spike2_nested_subgraph_persistence.py` | Does a nested subgraph node persist its **inner** steps (crash mid-sub-agent → resume mid-sub-agent)? | Yes — per-inner-turn resume |
| `spike3_dynamic_fanout_persistence.py` | Runtime-decided N via `Send` to one subgraph node — does each branch persist independently? | Yes — crashed branch resumes mid-way, siblings untouched |
| `spike4_dispatch_via_send_threading.py` | `dispatch_subagent` via post-agent `Send` routing: `ToolMessage`/`tool_call_id` threading, context isolation (separate `work` channel), mixed-turn answering, durability | All pass — S4's pinned recipe |
| `spike5_postgres_cross_process_resume.py` | Does per-turn resume hold on **real Postgres**, across a **real OS-process boundary**? Runs the two workers as separate `subprocess`es; failure injection + log live on disk; only Postgres is shared (hardened after PR review — the first harness shared in-memory state between the "workers"). *Needs a throwaway Postgres:* `docker run -d --name par-spike-pg -e POSTGRES_PASSWORD=spike -e POSTGRES_DB=spike -p 55999:5432 postgres:16` (remove after). | Yes — removes the MemorySaver caveat |
| `spike6_cost_attribution_mechanism.py` | Does `subgraphs=True` + namespace-wide `usage_metadata` aggregation capture the full fan-out spend (the S8 fix)? | Yes — baseline 0, fix captures all calls + exact totals |

Per AGENTS.md §Claims Require Evidence: load-bearing framework assumptions in a plan
get a spike before they become a task contract. These are the worked example.
