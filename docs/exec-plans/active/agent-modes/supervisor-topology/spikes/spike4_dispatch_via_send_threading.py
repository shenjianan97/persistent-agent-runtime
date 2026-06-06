"""THROWAWAY spike #4: dispatch_subagent via Send + ToolMessage threading.

Proves the S4 contract end-to-end:
 (1) LLM emits a dispatch_subagent tool call -> a post-agent routing edge Sends it
     to the shared subagent NODE (not the ToolNode).
 (2) The subagent threads its summary back as a ToolMessage keyed to tool_call_id.
 (3) Isolation: the sub-agent's internal working turns do NOT leak into the
     parent's messages channel (only the summary ToolMessage appears).
 (4) The agent loop continues correctly (every tool_call answered exactly once).
 (5) Mixed turn: dispatch_subagent + a normal tool call -> dispatch goes via Send,
     normal goes via the tools node, BOTH answered before the next LLM call.
 (6) Durability: a crash inside the sub-agent resumes per-inner-step.
"""
import asyncio
import operator
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.types import Send
from langgraph.checkpoint.memory import MemorySaver

WORK_LOG: list = []
FAIL = {"w2": False}


# ---------- shared sub-agent: a multi-step subgraph (isolated internal channel) ----------
class SubState(TypedDict, total=False):
    messages: Annotated[list, add_messages]   # OUTPUT ONLY: the final ToolMessage -> parent
    work: Annotated[list, add_messages]        # internal working turns (NOT in parent schema)
    tool_call_id: str
    prompt: str


def w1(s):
    WORK_LOG.append("w1")
    return {"work": [AIMessage(content=f"[sub] turn1 on {s['prompt']}")]}


def w2(s):
    WORK_LOG.append("w2")
    if FAIL["w2"]:
        FAIL["w2"] = False
        raise RuntimeError("SIMULATED crash inside sub-agent at inner step w2")
    return {"work": [AIMessage(content="[sub] turn2")]}


def finalize(s):
    WORK_LOG.append("finalize")
    summary = f"SUMMARY({s['prompt']}; {len(s.get('work', []))} internal turns)"
    # the ONE message that crosses back to the parent, keyed to the tool call
    return {"messages": [ToolMessage(content=summary, tool_call_id=s["tool_call_id"])]}


def build_subagent():
    g = StateGraph(SubState)
    g.add_node("w1", w1); g.add_node("w2", w2); g.add_node("finalize", finalize)
    g.add_edge(START, "w1"); g.add_edge("w1", "w2")
    g.add_edge("w2", "finalize"); g.add_edge("finalize", END)
    return g.compile()


# ---------- parent ReAct graph ----------
class Parent(TypedDict, total=False):
    messages: Annotated[list, add_messages]


def make_parent(scenario):
    subagent = build_subagent()

    def agent(state):
        msgs = state["messages"]
        answered = any(isinstance(m, ToolMessage) for m in msgs)
        if not answered:
            # first turn: emit tool call(s)
            tcs = [{"name": "dispatch_subagent", "args": {"prompt": "investigate X"}, "id": "call_1"}]
            if scenario == "mixed":
                tcs.append({"name": "echo", "args": {"text": "hi"}, "id": "call_2"})
            return {"messages": [AIMessage(content="", tool_calls=tcs)]}
        return {"messages": [AIMessage(content="FINAL ANSWER")]}

    def tools_node(state):
        # handles ONLY non-dispatch tool calls (the dispatch ones went via Send)
        last = state["messages"][-1]
        out = []
        for tc in last.tool_calls:
            if tc["name"] != "dispatch_subagent":
                out.append(ToolMessage(content=f"echoed:{tc['args']['text']}", tool_call_id=tc["id"]))
        return {"messages": out}

    def route(state):
        last = state["messages"][-1]
        if not getattr(last, "tool_calls", None):
            return END
        targets = []
        has_normal = False
        for tc in last.tool_calls:
            if tc["name"] == "dispatch_subagent":
                targets.append(Send("subagent",
                                    {"tool_call_id": tc["id"], "prompt": tc["args"]["prompt"]}))
            else:
                has_normal = True
        if has_normal:
            targets.append("tools")
        return targets

    g = StateGraph(Parent)
    g.add_node("agent", agent)
    g.add_node("subagent", subagent)     # shared subgraph as a NODE (Send target)
    g.add_node("tools", tools_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, ["subagent", "tools", END])
    g.add_edge("subagent", "agent")
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=MemorySaver())


def dump(msgs):
    return [f"{type(m).__name__}({(m.content or '')[:40]}"
            + (f" tc={[t['id'] for t in m.tool_calls]}" if getattr(m, 'tool_calls', None) else "")
            + (f" ->{m.tool_call_id}" if isinstance(m, ToolMessage) else "") + ")"
            for m in msgs]


async def test_single():
    print("\n=== 1) single dispatch_subagent: Send -> ToolMessage threading + isolation ===")
    WORK_LOG.clear(); FAIL["w2"] = False
    p = make_parent("single")
    out = await p.ainvoke({"messages": [HumanMessage(content="do it")]},
                          {"configurable": {"thread_id": "s1"}}, durability="sync")
    for line in dump(out["messages"]):
        print("   ", line)
    tms = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    leaked = any("[sub] turn" in (m.content or "") for m in out["messages"])
    print(f"  ToolMessage keyed to call_1? {any(m.tool_call_id=='call_1' for m in tms)}")
    print(f"  internal sub-turns leaked into parent? {leaked}  (EXPECT False)")
    print(f"  ends with FINAL ANSWER? {out['messages'][-1].content == 'FINAL ANSWER'}")


async def test_mixed():
    print("\n=== 2) mixed turn: dispatch (Send) + echo (tools node) both answered ===")
    WORK_LOG.clear(); FAIL["w2"] = False
    p = make_parent("mixed")
    out = await p.ainvoke({"messages": [HumanMessage(content="do both")]},
                          {"configurable": {"thread_id": "s2"}}, durability="sync")
    ids = {m.tool_call_id for m in out["messages"] if isinstance(m, ToolMessage)}
    print(f"  answered tool_call_ids: {sorted(ids)}  (EXPECT call_1 and call_2)")
    print(f"  completed without provider-style 'unanswered tool_call' error? {out['messages'][-1].content=='FINAL ANSWER'}")


async def test_crash():
    print("\n=== 3) crash inside the dispatched sub-agent at w2 -> resume per inner step ===")
    WORK_LOG.clear(); FAIL["w2"] = True
    p = make_parent("single")
    cfg = {"configurable": {"thread_id": "s3"}}
    try:
        await p.ainvoke({"messages": [HumanMessage(content="do it")]}, cfg, durability="sync")
    except Exception as e:
        print(f"  crashed as expected: {type(e).__name__}: {e}")
    print(f"  WORK_LOG after crash:  {WORK_LOG}")
    out = await p.ainvoke(None, cfg, durability="sync")
    print(f"  WORK_LOG after resume: {WORK_LOG}")
    w1n = WORK_LOG.count("w1")
    print(f"  inner step w1 ran {w1n}x  ({'PERSISTED: not recomputed' if w1n==1 else 'RECOMPUTED'})")
    print(f"  final threaded + loop done? {out['messages'][-1].content=='FINAL ANSWER' and any(isinstance(m,ToolMessage) for m in out['messages'])}")


async def main():
    await test_single()
    await test_mixed()
    await test_crash()
    print("\n=== done ===")


if __name__ == "__main__":
    asyncio.run(main())
