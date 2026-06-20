"""THROWAWAY spike #3: dynamic N sub-agents via Send, each a subgraph node.
Does each parallel branch persist its INNER progress independently, and does a
crash in ONE branch leave the others' completed work intact?

Supervisor emits 3 subtasks at runtime -> Send x3 to ONE subagent subgraph node.
Branch "t1" crashes at its inner step C once, then succeeds on resume.
Expected if per-branch persistence works:
  t0: A=1 B=1 C=1   (untouched)
  t1: A=1 B=1 C=2   (only the failed inner step re-ran)
  t2: A=1 B=1 C=1   (untouched)
"""
import asyncio
import operator
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from langgraph.checkpoint.memory import MemorySaver

LOG: list = []
FAIL_ONCE = {"t1": True}


class SubState(TypedDict, total=False):
    steps: Annotated[list, operator.add]
    subtask: str


def sA(s): LOG.append(f"{s['subtask']}:A"); return {"steps": ["A"]}
def sB(s): LOG.append(f"{s['subtask']}:B"); return {"steps": ["B"]}
def sC(s):
    t = s["subtask"]
    LOG.append(f"{t}:C")
    if FAIL_ONCE.get(t):
        FAIL_ONCE[t] = False
        raise RuntimeError(f"SIMULATED crash mid-sub-agent {t} at inner step C")
    return {"steps": ["C"]}


def build_subagent():
    g = StateGraph(SubState)
    g.add_node("sA", sA); g.add_node("sB", sB); g.add_node("sC", sC)
    g.add_edge(START, "sA"); g.add_edge("sA", "sB")
    g.add_edge("sB", "sC"); g.add_edge("sC", END)
    return g.compile()


class P(TypedDict, total=False):
    steps: Annotated[list, operator.add]
    subtasks: list


async def main():
    sub = build_subagent()

    def supervisor(s):
        return {"subtasks": ["t0", "t1", "t2"]}   # count decided AT RUNTIME

    def route(s):
        return [Send("subagent", {"subtask": t}) for t in s["subtasks"]]  # dynamic N

    g = StateGraph(P)
    g.add_node("supervisor", supervisor)
    g.add_node("subagent", sub)                    # ONE node; Send spawns N instances
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", route, ["subagent"])
    g.add_edge("subagent", END)
    parent = g.compile(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "dynN"}}

    try:
        await parent.ainvoke({}, cfg, durability="sync")
    except Exception as e:
        print(f"invoke 1 crashed as expected: {type(e).__name__}: {e}")
    print(f"LOG after crash:  {sorted(LOG)}")
    await parent.ainvoke(None, cfg, durability="sync")
    print(f"LOG after resume: {sorted(LOG)}")
    print()
    for t in ("t0", "t1", "t2"):
        a, b, c = LOG.count(f"{t}:A"), LOG.count(f"{t}:B"), LOG.count(f"{t}:C")
        verdict = "resumed mid-branch" if (t == "t1" and a == 1 and b == 1 and c == 2) \
            else ("untouched (not recomputed)" if a == 1 and b == 1 and c == 1 else "RECOMPUTED")
        print(f"  {t}: A={a} B={b} C={c}   -> {verdict}")


if __name__ == "__main__":
    asyncio.run(main())
