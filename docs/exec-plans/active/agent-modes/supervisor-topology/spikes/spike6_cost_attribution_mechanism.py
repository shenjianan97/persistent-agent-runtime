"""THROWAWAY spike #6: S8 cost-attribution mechanism.

Spike #1 proved sub-agent usage is INVISIBLE under the cost loop's `event["agent"]`
gate (no subgraphs). This proves the FIX: stream the fan-out with subgraphs=True
and aggregate usage_metadata from every LLM-bearing node across all namespaces, so
S8 can attribute the full fan-out spend to the parent task.

3 sub-agents, each doing 2 internal 'agent' LLM turns @ (in=100,out=50).
Expected collectible total: in=600, out=300 across 6 calls.
"""
import asyncio
import operator
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.types import Send
from langgraph.checkpoint.memory import MemorySaver

PER_CALL = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}


def ai():
    return AIMessage(content="llm turn", response_metadata={"model_name": "fake"},
                     usage_metadata=dict(PER_CALL))


class SubState(TypedDict, total=False):
    work: Annotated[list, add_messages]
    turns: Annotated[int, operator.add]
    subtask: str


def agent(s):
    # the inner ReAct "agent" node — emits an AIMessage carrying usage_metadata
    return {"work": [ai()], "turns": 1}


def route_inner(s):
    return "agent" if s.get("turns", 0) < 2 else END   # 2 turns then stop


def build_sub():
    g = StateGraph(SubState)
    g.add_node("agent", agent)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route_inner, ["agent", END])
    return g.compile()


class P(TypedDict, total=False):
    subtasks: list


def build_parent():
    sub = build_sub()
    def supervisor(s): return {"subtasks": ["t0", "t1", "t2"]}
    def route(s): return [Send("subagent", {"subtask": t}) for t in s["subtasks"]]
    g = StateGraph(P)
    g.add_node("supervisor", supervisor)
    g.add_node("subagent", sub)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", route, ["subagent"])
    g.add_edge("subagent", END)
    return g.compile(checkpointer=MemorySaver())


def collect_usage(update_dict):
    """Mimic an S8 cost-extractor: pull usage_metadata from any node's messages."""
    found = []
    for node_key, payload in update_dict.items():
        if not isinstance(payload, dict):
            continue
        for ch in ("messages", "work"):
            for m in payload.get(ch, []) or []:
                um = getattr(m, "usage_metadata", None)
                if um:
                    found.append((node_key, um))
    return found


async def main():
    parent = build_parent()
    cfg = {"configurable": {"thread_id": "cost1"}}

    # ----- BROKEN baseline: prod loop, gate on event["agent"], NO subgraphs -----
    seen_keys, base_in = [], 0
    async for ev in parent.astream({}, cfg, stream_mode="updates", durability="sync"):
        seen_keys.append(list(ev.keys()))
        if "agent" in ev:
            for m in ev["agent"].get("work", []):
                base_in += (getattr(m, "usage_metadata", {}) or {}).get("input_tokens", 0)
    print("=== BROKEN baseline (no subgraphs, gate on event['agent']) ===")
    print(f"  event keys: {seen_keys}")
    print(f"  collected input tokens: {base_in}   (EXPECT 0 — sub-agent spend invisible)")

    # ----- FIX: stream with subgraphs=True, aggregate across namespaces -----
    fix_in = fix_out = calls = 0
    async for ns, ev in parent.astream({}, {"configurable": {"thread_id": "cost2"}},
                                       stream_mode="updates", durability="sync", subgraphs=True):
        for node_key, um in collect_usage(ev):
            fix_in += um["input_tokens"]; fix_out += um["output_tokens"]; calls += 1
    print("\n=== FIX (subgraphs=True, aggregate usage across namespaces) ===")
    print(f"  LLM calls captured: {calls}   (EXPECT 6 = 3 sub-agents x 2 turns)")
    print(f"  total tokens captured: in={fix_in} out={fix_out}   (EXPECT in=600 out=300)")
    ok = calls == 6 and fix_in == 600 and fix_out == 300
    print(f"\n  => S8 mechanism {'WORKS: full fan-out spend is collectible for parent attribution' if ok else 'FAILED'}")


if __name__ == "__main__":
    asyncio.run(main())
