"""THROWAWAY spike for Agent Modes E1/E2/E3. Not shipped. langgraph 1.0.5.

Mirrors the real worker: astream(stream_mode="updates", durability="sync"),
cost loop gates on `if "agent" in event:` (graph.py:3166), ReAct node named
"agent" (graph.py:1556). Sub-agents fan out via Send.
"""
import asyncio
import operator
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send, interrupt, Command
from langgraph.checkpoint.memory import MemorySaver

EXEC_LOG: list = []  # side-effect counter to detect recompute


def make_ai(tok_in=100, tok_out=50):
    return AIMessage(
        content="subagent output",
        response_metadata={"model_name": "fake-model"},
        usage_metadata={"input_tokens": tok_in, "output_tokens": tok_out,
                        "total_tokens": tok_in + tok_out},
    )


def merge_by_subtask(left, right):
    out = dict(left or {})
    out.update(right or {})
    return out


# ─────────────────────────────────────────────────────────────────────────
# E1: does sub-agent token usage surface under the "agent" event key?
# ─────────────────────────────────────────────────────────────────────────
class PState(TypedDict, total=False):
    subtasks: list
    messages: Annotated[list, operator.add]
    subagent_results: Annotated[dict, merge_by_subtask]


def supervisor_node(s):
    return {"subtasks": ["1.0", "1.1"]}


def route_fanout(s):
    # structural fan-out, exactly like the planned Supervisor Send
    return [Send("subagent", {"subtask": t}) for t in s["subtasks"]]


def subagent_node(s):
    # a sub-agent does LLM work: produces an AIMessage carrying usage_metadata,
    # then returns a structured summary into subagent_results keyed by subtask
    sid = s["subtask"]
    ai = make_ai()
    return {"messages": [ai],
            "subagent_results": {sid: {"usage": ai.usage_metadata}}}


def build_parent(checkpointer=None):
    g = StateGraph(PState)
    g.add_node("supervisor", supervisor_node)
    g.add_node("subagent", subagent_node)   # fan-out node — NOT named "agent"
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", route_fanout, ["subagent"])
    g.add_edge("subagent", END)
    return g.compile(checkpointer=checkpointer)


async def e1():
    print("\n========== E1: cost capture under 'agent' key ==========")
    parent = build_parent(MemorySaver())
    cfg = {"configurable": {"thread_id": "e1a"}}

    cost_loop_saw_agent = False
    seen_keys = []
    # EXACT prod conditions: stream_mode='updates', durability='sync', NO subgraphs
    async for event in parent.astream({}, cfg, stream_mode="updates", durability="sync"):
        seen_keys.append(list(event.keys()))
        if "agent" in event:                       # <-- the real cost-loop gate
            cost_loop_saw_agent = True
    print("  event keys seen (no subgraphs):", seen_keys)
    print(f"  cost loop `if \"agent\" in event` matched? -> {cost_loop_saw_agent}")
    print("  EXPECT False  => sub-agent usage is INVISIBLE to the cost loop")

    # Now model the sub-agent as a real ReAct SUBGRAPH whose internal node IS
    # named 'agent', added as a node, to test the subgraphs=True fix path.
    class SubState(TypedDict, total=False):
        messages: Annotated[list, operator.add]
        subtask: str

    def sub_agent_llm(s):
        return {"messages": [make_ai()]}

    sg = StateGraph(SubState)
    sg.add_node("agent", sub_agent_llm)
    sg.add_edge(START, "agent")
    sg.add_edge("agent", END)
    sub_compiled = sg.compile()

    g2 = StateGraph(PState)
    g2.add_node("supervisor", supervisor_node)
    g2.add_node("subagent", sub_compiled)   # subgraph-as-node with inner 'agent'
    g2.add_edge(START, "supervisor")
    g2.add_conditional_edges("supervisor", route_fanout, ["subagent"])
    g2.add_edge("subagent", END)
    parent2 = g2.compile(checkpointer=MemorySaver())

    saw_agent_plain = False
    saw_agent_subgraphs = False
    async for event in parent2.astream({"messages": []}, {"configurable": {"thread_id": "e1b"}},
                                       stream_mode="updates", durability="sync"):
        if "agent" in event:
            saw_agent_plain = True
    async for ns, event in parent2.astream({"messages": []}, {"configurable": {"thread_id": "e1c"}},
                                           stream_mode="updates", durability="sync", subgraphs=True):
        if "agent" in event:
            saw_agent_subgraphs = True
            print(f"  subgraphs=True surfaced inner 'agent' under namespace {ns}")
    print(f"  inner-'agent' visible WITHOUT subgraphs? -> {saw_agent_plain}  (EXPECT False)")
    print(f"  inner-'agent' visible WITH subgraphs=True? -> {saw_agent_subgraphs}  (EXPECT True = fix path)")


# ─────────────────────────────────────────────────────────────────────────
# E2: resume-forward — does a COMPLETED sibling re-run after a pause?
# E3: two sub-agents interrupting in one super-step — scalar resume.
# ─────────────────────────────────────────────────────────────────────────
class IState(TypedDict, total=False):
    subtasks: list
    subagent_results: Annotated[dict, merge_by_subtask]


def isup(s):
    return {"subtasks": ["1.0", "1.1"]}


def iroute(s):
    return [Send("subagent", {"subtask": t}) for t in s["subtasks"]]


def build_interrupting(node):
    g = StateGraph(IState)
    g.add_node("supervisor", isup)
    g.add_node("subagent", node)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", iroute, ["subagent"])
    g.add_edge("subagent", END)
    return g.compile(checkpointer=MemorySaver())


async def e2():
    print("\n========== E2: resume-forward (completed sibling re-run?) ==========")
    EXEC_LOG.clear()

    def mixed(s):
        sid = s["subtask"]
        EXEC_LOG.append(sid)
        if sid == "1.1":
            ans = interrupt({"q": f"clarify {sid}"})   # only ONE branch interrupts
            return {"subagent_results": {sid: {"answer": ans}}}
        return {"subagent_results": {sid: {"done": True}}}   # the other completes

    graph = build_interrupting(mixed)
    cfg = {"configurable": {"thread_id": "e2"}}
    res = await graph.ainvoke({}, cfg, durability="sync")
    print("  after first run, EXEC_LOG:", EXEC_LOG, " (both branches executed once)")
    st = graph.get_state(cfg)
    print("  pending interrupts:", len(st.interrupts), " next:", st.next)

    before = list(EXEC_LOG)
    final = await graph.ainvoke(Command(resume="ok"), cfg, durability="sync")  # scalar resume
    after = list(EXEC_LOG)
    completed_reran = after.count("1.0") > before.count("1.0")
    print("  EXEC_LOG after resume:", after)
    print(f"  completed sibling '1.0' re-ran on resume? -> {completed_reran}  (EXPECT False = resume-forward)")
    print("  final subagent_results keys:", sorted(final.get("subagent_results", {}).keys()))


async def e3():
    print("\n========== E3: two interrupts in one super-step + scalar resume ==========")
    EXEC_LOG.clear()

    def both_interrupt(s):
        sid = s["subtask"]
        EXEC_LOG.append(sid)
        ans = interrupt({"q": f"clarify {sid}"})   # BOTH branches interrupt
        return {"subagent_results": {sid: {"answer": ans}}}

    graph = build_interrupting(both_interrupt)
    cfg = {"configurable": {"thread_id": "e3"}}
    await graph.ainvoke({}, cfg, durability="sync")
    st = graph.get_state(cfg)
    print("  pending interrupts after fan-out:", len(st.interrupts))
    try:
        await graph.ainvoke(Command(resume="ok"), cfg, durability="sync")  # scalar resume
        print("  scalar resume SUCCEEDED  (no multi-interrupt error)")
    except Exception as ex:
        print(f"  scalar resume RAISED -> {type(ex).__name__}: {ex}")
        print("  => E3 confirmed: scalar Command(resume=...) cannot resume multiple interrupts")

    # E3 fix check: headless sub-agents (no interrupt tool) fan out cleanly
    EXEC_LOG.clear()

    def headless(s):
        sid = s["subtask"]
        EXEC_LOG.append(sid)
        return {"subagent_results": {sid: {"done": True}}}

    g2 = build_interrupting(headless)
    cfg2 = {"configurable": {"thread_id": "e3fix"}}
    final = await g2.ainvoke({}, cfg2, durability="sync")
    print(f"  FIX: headless sub-agents complete cleanly, results={sorted(final.get('subagent_results',{}).keys())}")


async def main():
    await e1()
    await e2()
    await e3()
    print("\n========== spike complete ==========")


if __name__ == "__main__":
    asyncio.run(main())
