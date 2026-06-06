"""Spike #5: per-turn sub-agent resume against REAL Postgres, across a REAL
OS-process boundary.

Hardened after PR review: the first version ran "worker A" and "worker B" in one
interpreter, sharing the module-level failure flag and execution log — so the
process boundary was simulated, not real. This version runs each worker as a
separate OS process (`subprocess`); the failure injection and the execution log
live on disk, and the only state shared between the workers is Postgres itself.

  orchestrator (no arg):
    1. arms the crash sentinel, runs `worker` phase 1 as a subprocess -> crashes
       mid-sub-agent (branch t1, inner step w2)
    2. disarms the sentinel (the transient fault is gone on retry), runs phase 2
       as a fresh subprocess -> resumes the same thread_id from Postgres
    3. parses the on-disk log and asserts per-inner-turn resume

Needs a throwaway Postgres (see spikes/README.md):
  docker run -d --name par-spike-pg -e POSTGRES_PASSWORD=spike \
    -e POSTGRES_DB=spike -p 55999:5432 postgres:16
"""
import asyncio
import pathlib
import subprocess
import sys
from typing import Annotated, TypedDict

DB = "postgresql://postgres:spike@localhost:55999/spike"
TMP = pathlib.Path("/tmp")
CRASH_ARMED = TMP / "spike5_crash_armed"
LOG_FILE = TMP / "spike5_exec_log.txt"
THREAD_ID = "pg-crash-two-process"


def log(entry: str) -> None:
    with LOG_FILE.open("a") as f:
        f.write(entry + "\n")


# ─────────────────────────── worker (runs in its own OS process) ──────────────
async def worker(phase: str) -> None:
    from langchain_core.messages import AIMessage
    from langgraph.graph import StateGraph, START, END
    from langgraph.graph.message import add_messages
    from langgraph.types import Send
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    class SubState(TypedDict, total=False):
        work: Annotated[list, add_messages]
        subtask: str

    def w1(s):
        log(f"{s['subtask']}:w1")
        return {"work": [AIMessage(content="w1")]}

    def w2(s):
        t = s["subtask"]
        log(f"{t}:w2")
        if t == "t1" and CRASH_ARMED.exists():
            # transient system fault: present in phase 1, gone in phase 2.
            # NOTE: the sentinel is NOT touched here — a fresh process sees the
            # same disk state; only the orchestrator disarms it between phases.
            raise RuntimeError("SIMULATED crash mid-sub-agent t1 at w2")
        return {"work": [AIMessage(content="w2")]}

    def w3(s):
        log(f"{s['subtask']}:w3")
        return {"work": [AIMessage(content="w3")]}

    sub = StateGraph(SubState)
    sub.add_node("w1", w1); sub.add_node("w2", w2); sub.add_node("w3", w3)
    sub.add_edge(START, "w1"); sub.add_edge("w1", "w2")
    sub.add_edge("w2", "w3"); sub.add_edge("w3", END)
    sub_compiled = sub.compile()

    class P(TypedDict, total=False):
        subtasks: list

    def supervisor(s): return {"subtasks": ["t0", "t1"]}
    def route(s): return [Send("subagent", {"subtask": t}) for t in s["subtasks"]]

    g = StateGraph(P)
    g.add_node("supervisor", supervisor)
    g.add_node("subagent", sub_compiled)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", route, ["subagent"])
    g.add_edge("subagent", END)

    async with AsyncPostgresSaver.from_conn_string(DB) as saver:
        await saver.setup()
        graph = g.compile(checkpointer=saver)
        cfg = {"configurable": {"thread_id": THREAD_ID}}
        payload = {} if phase == "1" else None   # phase 2 = resume (no new input)
        await graph.ainvoke(payload, cfg, durability="sync")


# ─────────────────────────── orchestrator ─────────────────────────────────────
def orchestrate() -> int:
    LOG_FILE.unlink(missing_ok=True)
    CRASH_ARMED.touch()                                   # arm the fault

    p1 = subprocess.run([sys.executable, __file__, "1"], capture_output=True, text=True)
    print(f"phase 1 (worker A, separate process) exit={p1.returncode} "
          f"{'(crashed as expected)' if p1.returncode != 0 else '(UNEXPECTED success)'}")
    after_crash = LOG_FILE.read_text().split()
    print(f"  on-disk log after crash:  {sorted(after_crash)}")

    CRASH_ARMED.unlink()                                  # fault is gone on retry

    p2 = subprocess.run([sys.executable, __file__, "2"], capture_output=True, text=True)
    if p2.returncode != 0:
        print(p2.stdout, p2.stderr)
        print("phase 2 FAILED — resume did not complete")
        return 1
    entries = LOG_FILE.read_text().split()
    print(f"  on-disk log after resume: {sorted(entries)}")

    print()
    failures = 0
    for t, expect in (("t0", {"w1": 1, "w2": 1, "w3": 1}),
                      ("t1", {"w1": 1, "w2": 2, "w3": 1})):
        counts = {s: entries.count(f"{t}:{s}") for s in ("w1", "w2", "w3")}
        ok = counts == expect
        failures += 0 if ok else 1
        label = ("untouched (completed branch restored)" if t == "t0"
                 else "resumed at w2 across a REAL process boundary (w1 NOT recomputed)")
        print(f"  {t}: {counts}  -> {label if ok else f'FAILED (expected {expect})'}")
    return failures


if __name__ == "__main__":
    if len(sys.argv) > 1:
        asyncio.run(worker(sys.argv[1]))
    else:
        sys.exit(orchestrate())
