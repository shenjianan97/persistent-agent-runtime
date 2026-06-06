"""THROWAWAY spike #5: per-turn sub-agent resume against REAL Postgres, across a
simulated process boundary (the caveat on spikes #2-#4 that used MemorySaver).

Block 1 (worker A): run a Send fan-out; one sub-agent crashes at inner step w2.
Block 2 (worker B): a FRESH graph + FRESH Postgres connection (no shared in-memory
state) resumes the same thread. If it resumes per-inner-step, durability holds on
the real backend across a process boundary.
"""
import asyncio
import operator
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.types import Send
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

DB = "postgresql://postgres:spike@localhost:55999/spike"
LOG: list = []
FAIL = {"t1": True}


class SubState(TypedDict, total=False):
    work: Annotated[list, add_messages]
    subtask: str


def w1(s): LOG.append(f"{s['subtask']}:w1"); return {"work": [AIMessage(content="w1")]}
def w2(s):
    t = s["subtask"]; LOG.append(f"{t}:w2")
    if FAIL.get(t):
        FAIL[t] = False
        raise RuntimeError(f"SIMULATED crash mid-sub-agent {t} at w2")
    return {"work": [AIMessage(content="w2")]}
def w3(s): LOG.append(f"{s['subtask']}:w3"); return {"work": [AIMessage(content="w3")]}


def build_sub():
    g = StateGraph(SubState)
    g.add_node("w1", w1); g.add_node("w2", w2); g.add_node("w3", w3)
    g.add_edge(START, "w1"); g.add_edge("w1", "w2"); g.add_edge("w2", "w3"); g.add_edge("w3", END)
    return g.compile()


class P(TypedDict, total=False):
    subtasks: list


def build_parent(saver):
    sub = build_sub()

    def supervisor(s): return {"subtasks": ["t0", "t1"]}
    def route(s): return [Send("subagent", {"subtask": t}) for t in s["subtasks"]]

    g = StateGraph(P)
    g.add_node("supervisor", supervisor)
    g.add_node("subagent", sub)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", route, ["subagent"])
    g.add_edge("subagent", END)
    return g.compile(checkpointer=saver)


async def main():
    cfg = {"configurable": {"thread_id": "pg-crash-1"}}

    # ---- worker A: run until crash, then "die" (close connection) ----
    async with AsyncPostgresSaver.from_conn_string(DB) as saverA:
        await saverA.setup()
        gA = build_parent(saverA)
        try:
            await gA.ainvoke({}, cfg, durability="sync")
        except Exception as e:
            print(f"worker A crashed as expected: {type(e).__name__}: {e}")
    print(f"LOG after worker A crash:  {sorted(LOG)}")
    print("  (worker A connection closed — state now lives only in Postgres)")

    # ---- worker B: brand-new graph + brand-new connection, resume the thread ----
    async with AsyncPostgresSaver.from_conn_string(DB) as saverB:
        gB = build_parent(saverB)          # fresh graph object, shares nothing in-memory
        await gB.ainvoke(None, cfg, durability="sync")
    print(f"LOG after worker B resume: {sorted(LOG)}")

    print()
    for t in ("t0", "t1"):
        w1n, w2n, w3n = (LOG.count(f"{t}:w1"), LOG.count(f"{t}:w2"), LOG.count(f"{t}:w3"))
        if t == "t0":
            ok = (w1n, w2n, w3n) == (1, 1, 1)
            print(f"  {t}: w1={w1n} w2={w2n} w3={w3n}  -> {'untouched (completed branch restored)' if ok else 'RECOMPUTED'}")
        else:
            ok = w1n == 1 and w2n == 2
            print(f"  {t}: w1={w1n} w2={w2n} w3={w3n}  -> {'resumed at w2 across process boundary (w1 NOT recomputed)' if ok else 'RECOMPUTED FROM START'}")


if __name__ == "__main__":
    asyncio.run(main())
