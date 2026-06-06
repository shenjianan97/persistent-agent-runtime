"""THROWAWAY spike #2: does a nested subgraph persist its INTERNAL progress?

Question: a sub-agent = a multi-step subgraph added as a node of the master
graph, sharing the parent's checkpointer. If the worker crashes partway through
the sub-agent, does resume re-run only the failed inner step, or the whole
sub-agent from scratch?

Method: inner subgraph sA -> sB -> sC; sC raises ONCE (simulated crash), then
succeeds on resume. Count how many times each inner step runs.
  - persisted   => sA=1, sB=1, sC=2  (only the failed step re-runs)
  - atomic      => sA=2, sB=2, sC=2  (whole sub-agent recomputed)
"""
import asyncio
import operator
from typing import Annotated, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from langgraph.checkpoint.memory import MemorySaver

LOG: list = []
FAIL_ONCE = {"sC": True}


# ---- inner sub-agent: a 3-step subgraph (stands in for a multi-turn ReAct loop)
class SubState(TypedDict, total=False):
    steps: Annotated[list, operator.add]
    subtask: str


def sA(s):
    LOG.append("sA")
    return {"steps": ["A"]}


def sB(s):
    LOG.append("sB")
    return {"steps": ["B"]}


def sC(s):
    LOG.append("sC")
    if FAIL_ONCE["sC"]:
        FAIL_ONCE["sC"] = False
        raise RuntimeError("SIMULATED worker crash mid-sub-agent (at inner step C)")
    return {"steps": ["C"]}


def build_subagent():
    g = StateGraph(SubState)
    g.add_node("sA", sA)
    g.add_node("sB", sB)
    g.add_node("sC", sC)
    g.add_edge(START, "sA")
    g.add_edge("sA", "sB")
    g.add_edge("sB", "sC")
    g.add_edge("sC", END)
    return g.compile()  # NO own checkpointer -> inherits parent's


# ---- master graph: sub-agent added as a NODE (the user's "graph inside graph")
class Parent(TypedDict, total=False):
    steps: Annotated[list, operator.add]


async def test_nested_as_node():
    print("\n=== A) sub-agent as a nested subgraph NODE (shares parent checkpointer) ===")
    LOG.clear(); FAIL_ONCE["sC"] = True
    sub = build_subagent()
    g = StateGraph(Parent)
    g.add_node("subagent", sub)          # nested graph as a node
    g.add_edge(START, "subagent")
    g.add_edge("subagent", END)
    parent = g.compile(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "nested"}}

    try:
        await parent.ainvoke({}, cfg, durability="sync")
    except Exception as e:
        print(f"  invoke 1 crashed as expected: {type(e).__name__}: {e}")
    print(f"  LOG after crash: {LOG}")

    # resume: re-invoke with None input, same thread_id (LangGraph resume-after-failure)
    final = await parent.ainvoke(None, cfg, durability="sync")
    print(f"  LOG after resume: {LOG}")
    a, b, c = LOG.count("sA"), LOG.count("sB"), LOG.count("sC")
    print(f"  counts: sA={a} sB={b} sC={c}  final steps={final.get('steps')}")
    if a == 1 and b == 1:
        print("  => PERSISTED: inner steps A,B were NOT recomputed; only the failed step re-ran.")
    else:
        print("  => ATOMIC: the whole sub-agent recomputed from scratch.")


async def test_send_fanout():
    print("\n=== B) same, but reached via Send fan-out (the Supervisor shape) ===")
    LOG.clear(); FAIL_ONCE["sC"] = True
    sub = build_subagent()

    class PFan(TypedDict, total=False):
        steps: Annotated[list, operator.add]
        subtasks: list

    def supervisor(s):
        return {"subtasks": ["t1"]}

    def route(s):
        return [Send("subagent", {"subtask": t}) for t in s["subtasks"]]

    g = StateGraph(PFan)
    g.add_node("supervisor", supervisor)
    g.add_node("subagent", sub)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", route, ["subagent"])
    g.add_edge("subagent", END)
    parent = g.compile(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "fan"}}

    try:
        await parent.ainvoke({}, cfg, durability="sync")
    except Exception as e:
        print(f"  invoke 1 crashed as expected: {type(e).__name__}: {e}")
    print(f"  LOG after crash: {LOG}")
    final = await parent.ainvoke(None, cfg, durability="sync")
    print(f"  LOG after resume: {LOG}")
    a, b, c = LOG.count("sA"), LOG.count("sB"), LOG.count("sC")
    print(f"  counts: sA={a} sB={b} sC={c}")
    print("  => " + ("PERSISTED mid-sub-agent (A,B not recomputed)" if a == 1 and b == 1
                      else "ATOMIC (whole sub-agent recomputed)"))


async def main():
    await test_nested_as_node()
    await test_send_fanout()
    print("\n=== done ===")


if __name__ == "__main__":
    asyncio.run(main())
