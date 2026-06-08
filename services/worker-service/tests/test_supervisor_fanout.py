"""Unit tests for the Supervisor node + structural Send fan-out + iteration loop
+ the ``subagent_results`` keyed reducer (Task S6).

All tests here are **fake-model, fake-``run_subagent``, no-network, no-DB** — they
exercise ``supervisor_node`` directly and drive the Supervisor↔fan-out↔Supervisor
sub-wiring through a tiny in-process ``MemorySaver`` graph (``durability="sync"``,
matching the worker runtime — never async). No Postgres, no TCP ports, no server
subprocess → worktree-concurrency-safe.

Coverage maps 1:1 to the S6 acceptance criteria:

* ``supervisor_node`` emits a **parsed** ``subtasks: [{subtask, prompt}]`` list,
  clamped to ``max_fanout_per_iteration``;
* **deterministic id minting + within-iteration collision (§A11-E8)** — a fake
  model emitting two distinct subtasks with a colliding (or absent) LLM-chosen
  id yields two distinct ``f"{iteration}.{index}"`` ids and BOTH results survive
  in ``subagent_results`` (distinct from the cross-round idempotency test);
* the keyed reducer is idempotent (same result twice → one entry) and a
  re-dispatched failed subtask overwrites its marker in place under a
  carried-forward id (no duplicate);
* the Send fan-out dispatches N sub-agents in parallel through ``run_subagent``;
* the iteration loop is bounded by ``max_iterations``; hitting the cap forces
  ``stop`` and emits a ``supervisor_iteration`` cap-reason event;
* clamp past ``max_fanout_per_iteration`` truncates + emits a cap-reason event
  (no silent truncation);
* partial-failure: with ≥1 success the run proceeds; with zero returns and no
  progress the graph fails;
* ``supervisor_iteration`` event payload shape ``{iteration, subtasks_emitted,
  decision, reason}``;
* crash resume-forward restores completed siblings (not recomputed), only
  unfinished branches re-run;
* the oversized-payload log fires past a threshold and not below.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from executor.subagents import SubagentCeiling, SubagentResult
from executor.supervisor import SupervisorState, supervisor_node
from executor.supervisor.graph import (
    SUPERVISOR_NODE_NAME,
    SupervisorFanoutConfigError,
    add_supervisor_fanout,
)
from executor.supervisor.nodes import (
    DECISION_CONTINUE,
    DECISION_STOP,
)
from executor.supervisor.state import _merge_subagent_results

# A fan-out node reaches ``run_subagent`` via the module symbol — tests patch
# ``executor.supervisor.graph.run_subagent`` (repo convention, mirroring
# ``test_dispatch_subagent_tool.py``'s ``executor.graph.run_subagent`` patch),
# never a config-injected seam. The deps below are the minimal real-shaped
# objects the node's fail-fast (model / checkpointer non-``None``) requires; the
# patched ``run_subagent`` ignores them.
_FANOUT_DEPS = {
    "model": object(),
    "checkpointer": MemorySaver(),
    "ceiling": SubagentCeiling(max_turns=4, max_tokens=10_000),
    "tools": [],
    "emit": None,
}

# ``asyncio_mode = "auto"`` (pyproject.toml) runs async tests without a marker.


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class ScriptedSupervisorModel:
    """Chat model that returns a scripted decision JSON per ``ainvoke`` call.

    Each item in ``scripts`` is the *raw string* the Supervisor LLM emits for
    one round (the structured decision contract). On a replayed super-step the
    node re-invokes; routing by call index would desync, so this model returns
    the script for the round implied by its own call counter but clamps at the
    last script (idempotent on replay).
    """

    def __init__(self, scripts: list[str]):
        self._scripts = scripts
        self.invocations = 0
        self.seen: list[Any] = []

    async def ainvoke(self, messages, *args, **kwargs):
        self.seen.append(messages)
        idx = min(self.invocations, len(self._scripts) - 1)
        self.invocations += 1
        return AIMessage(content=self._scripts[idx])


def _decision(
    *,
    decision: str,
    subtasks: list[dict] | None = None,
    reason: str = "",
) -> str:
    return json.dumps(
        {"decision": decision, "subtasks": subtasks or [], "reason": reason}
    )


class RecordingEmit:
    """Captures ``supervisor_iteration`` payloads the node emits."""

    def __init__(self):
        self.events: list[dict] = []

    async def __call__(self, event_type: str, details: dict):
        self.events.append({"event_type": event_type, "details": details})


def _supervisor_config(
    *,
    model: ScriptedSupervisorModel,
    max_fanout: int = 5,
    max_iterations: int = 3,
    source_allowlist: list[str] | None = None,
    emit=None,
    fanout_deps: dict | None = None,
) -> dict:
    return {
        "configurable": {
            "supervisor_model": model,
            "agent_config": {
                "supervisor": {
                    "max_fanout_per_iteration": max_fanout,
                    "max_iterations": max_iterations,
                    "source_allowlist": source_allowlist or ["web_search"],
                }
            },
            "supervisor_emit": emit or RecordingEmit(),
            # S8 injects the real fan-out deps (model / checkpointer / ceiling /
            # tools / emit) at graph-build. Tests pass the minimal real-shaped
            # ``_FANOUT_DEPS`` so the node's fail-fast is satisfied; the actual
            # sub-agent run is exercised through a patched ``run_subagent``.
            "supervisor_fanout_deps": fanout_deps,
            "thread_id": "test-thread",
        }
    }


# --------------------------------------------------------------------------- #
# 1. supervisor_node emits a parsed subtask list (structured, not freeform)
# --------------------------------------------------------------------------- #
async def test_supervisor_node_emits_parsed_subtasks():
    model = ScriptedSupervisorModel(
        [
            _decision(
                decision="continue",
                subtasks=[
                    {"prompt": "research A"},
                    {"prompt": "research B"},
                ],
            )
        ]
    )
    cfg = _supervisor_config(model=model)
    state: dict = {"brief": "north star", "iteration": 0, "subagent_results": {}}
    out = await supervisor_node(state, cfg)

    assert out["supervisor_decision"] == DECISION_CONTINUE
    assert out["iteration"] == 1
    subtasks = out["subtasks"]
    assert [s["prompt"] for s in subtasks] == ["research A", "research B"]
    # Every entry carries the parsed contract keys.
    assert all(set(s.keys()) >= {"subtask", "prompt"} for s in subtasks)


# --------------------------------------------------------------------------- #
# 2. Deterministic id minting — ids are f"{iteration}.{index}", NOT the LLM's
# --------------------------------------------------------------------------- #
async def test_subtask_ids_minted_deterministically_ignoring_llm():
    model = ScriptedSupervisorModel(
        [
            _decision(
                decision="continue",
                subtasks=[
                    {"subtask": "evil-id", "prompt": "a"},
                    {"subtask": "evil-id", "prompt": "b"},  # LLM tries to collide
                ],
            )
        ]
    )
    cfg = _supervisor_config(model=model)
    state = {"brief": "b", "iteration": 0, "subagent_results": {}}
    out = await supervisor_node(state, cfg)

    ids = [s["subtask"] for s in out["subtasks"]]
    # Minted by S6, NOT trusted from the LLM emission.
    assert ids == ["1.0", "1.1"]
    assert "evil-id" not in ids


# --------------------------------------------------------------------------- #
# 3. WITHIN-ITERATION collision test (§A11-E8) — distinct from cross-round
#    idempotency. Two subtasks the LLM gave a colliding id BOTH survive.
# --------------------------------------------------------------------------- #
async def test_within_iteration_collision_both_results_survive():
    """Two distinct subtasks emitted in the SAME round, with a colliding (or
    absent) LLM-chosen id, get distinct minted ids and BOTH results land in
    ``subagent_results``. Proves deterministic minting prevents the silent
    reducer overwrite that a trusted-LLM-id would cause."""
    model = ScriptedSupervisorModel(
        [
            _decision(
                decision="continue",
                subtasks=[
                    {"subtask": "dup", "prompt": "first"},
                    {"subtask": "dup", "prompt": "second"},
                ],
            ),
            _decision(decision="stop"),  # round 2 → stop, so the loop ends
        ]
    )

    async def fake_run(prompt, tools, **kwargs):
        # S7 wraps the raw subtask prompt in the Subagent findings template, so
        # the raw subtask ("first" / "second") is now EMBEDDED in ``prompt`` —
        # assert containment, then echo it back so the per-key result is distinct.
        which = "first" if "first" in prompt else "second"
        return SubagentResult.success(f"summary for {which}")

    emit = RecordingEmit()
    cfg = _supervisor_config(model=model, emit=emit, fanout_deps=_FANOUT_DEPS)
    graph = _one_round_graph(cfg)
    with patch(
        "executor.supervisor.graph.run_subagent",
        AsyncMock(side_effect=fake_run),
    ):
        out = await graph.ainvoke(
            {"brief": "b", "iteration": 0, "subagent_results": {}},
            config={"configurable": cfg["configurable"]},
            durability="sync",
        )

    results = out["subagent_results"]
    # Both minted ids present — no silent overwrite.
    assert set(results.keys()) == {"1.0", "1.1"}
    summaries = {k: v["summary"] for k, v in results.items()}
    assert summaries["1.0"] == "summary for first"
    assert summaries["1.1"] == "summary for second"


# --------------------------------------------------------------------------- #
# 4. Clamp to max_fanout_per_iteration + cap-reason event (no silent truncation)
# --------------------------------------------------------------------------- #
async def test_clamp_to_max_fanout_emits_cap_event():
    model = ScriptedSupervisorModel(
        [
            _decision(
                decision="continue",
                subtasks=[{"prompt": f"t{i}"} for i in range(7)],
            )
        ]
    )
    emit = RecordingEmit()
    cfg = _supervisor_config(model=model, max_fanout=3, emit=emit)
    out = await supervisor_node(
        {"brief": "b", "iteration": 0, "subagent_results": {}}, cfg
    )

    assert len(out["subtasks"]) == 3  # truncated to the cap
    # A cap-reason event recorded the truncation — not silent.
    iter_events = [e for e in emit.events if e["event_type"] == "supervisor_iteration"]
    assert any("fanout" in (e["details"].get("reason") or "") for e in iter_events)
    assert any(e["details"]["subtasks_emitted"] == 3 for e in iter_events)


# --------------------------------------------------------------------------- #
# 5. Iteration bound — hitting max_iterations forces stop + cap-reason event
# --------------------------------------------------------------------------- #
async def test_max_iterations_forces_stop():
    # The model would keep saying "continue", but the cap must override.
    model = ScriptedSupervisorModel(
        [_decision(decision="continue", subtasks=[{"prompt": "x"}])]
    )
    emit = RecordingEmit()
    cfg = _supervisor_config(model=model, max_iterations=2, emit=emit)
    # Already at the cap round.
    out = await supervisor_node(
        {"brief": "b", "iteration": 2, "subagent_results": {"1.0": {"ok": True}}},
        cfg,
    )

    assert out["supervisor_decision"] == DECISION_STOP
    iter_events = [e for e in emit.events if e["event_type"] == "supervisor_iteration"]
    assert any(
        e["details"]["decision"] == DECISION_STOP
        and "iteration" in (e["details"].get("reason") or "").lower()
        for e in iter_events
    )


# --------------------------------------------------------------------------- #
# 6. supervisor_iteration event payload shape
# --------------------------------------------------------------------------- #
async def test_supervisor_iteration_event_payload_shape():
    model = ScriptedSupervisorModel(
        [_decision(decision="continue", subtasks=[{"prompt": "a"}, {"prompt": "b"}])]
    )
    emit = RecordingEmit()
    cfg = _supervisor_config(model=model, emit=emit)
    await supervisor_node({"brief": "b", "iteration": 0, "subagent_results": {}}, cfg)

    events = [e for e in emit.events if e["event_type"] == "supervisor_iteration"]
    assert events, "supervisor_iteration must be emitted"
    payload = events[-1]["details"]
    assert set(payload.keys()) >= {"iteration", "subtasks_emitted", "decision", "reason"}
    assert payload["iteration"] == 1
    assert payload["subtasks_emitted"] == 2
    assert payload["decision"] == DECISION_CONTINUE


# --------------------------------------------------------------------------- #
# 7. Partial failure: ≥1 success → proceed; zero returns + no progress → fail
# --------------------------------------------------------------------------- #
async def test_partial_failure_with_one_success_proceeds():
    model = ScriptedSupervisorModel([_decision(decision="stop")])
    cfg = _supervisor_config(model=model)
    # One success + one failure marker already in results from the prior round.
    out = await supervisor_node(
        {
            "brief": "b",
            "iteration": 1,
            "subagent_results": {
                "1.0": {"ok": True, "summary": "found"},
                "1.1": {"ok": False, "reason": "timeout"},
            },
        },
        cfg,
    )
    # ≥1 success → the run proceeds (here the model elects to stop → Writer).
    assert out["supervisor_decision"] == DECISION_STOP


async def test_zero_returns_no_progress_fails():
    model = ScriptedSupervisorModel([_decision(decision="continue")])
    cfg = _supervisor_config(model=model)
    with pytest.raises(SupervisorAllFailedError):
        await supervisor_node(
            {
                "brief": "b",
                "iteration": 1,
                "subagent_results": {
                    "1.0": {"ok": False, "reason": "error"},
                    "1.1": {"ok": False, "reason": "timeout"},
                },
            },
            cfg,
        )


# --------------------------------------------------------------------------- #
# 8. Reducer idempotency + re-dispatch overwrite (carry-forward id)
# --------------------------------------------------------------------------- #
def test_reducer_idempotent_same_result_twice_one_entry():
    base = {"1.0": {"ok": True, "summary": "a"}}
    delta = {"1.0": {"ok": True, "summary": "a"}}
    merged = _merge_subagent_results(base, delta)
    assert merged == {"1.0": {"ok": True, "summary": "a"}}


def test_reducer_redispatch_overwrites_failure_marker_in_place():
    base = {"1.0": {"ok": False, "reason": "timeout"}}
    # Re-dispatch carries the SAME id → overwrites in place, no duplicate.
    delta = {"1.0": {"ok": True, "summary": "recovered"}}
    merged = _merge_subagent_results(base, delta)
    assert merged == {"1.0": {"ok": True, "summary": "recovered"}}
    assert len(merged) == 1


async def test_redispatch_carries_forward_failed_id():
    """A re-dispatch round carries forward the prior failed subtask's id; new
    subtasks get freshly-minted round-keyed ids."""
    model = ScriptedSupervisorModel(
        [
            _decision(
                decision="continue",
                subtasks=[
                    {"subtask": "1.1", "prompt": "retry the failed one", "redispatch": True},
                    {"prompt": "a brand new subtask"},
                ],
            )
        ]
    )
    cfg = _supervisor_config(model=model)
    out = await supervisor_node(
        {
            "brief": "b",
            "iteration": 1,
            "subagent_results": {
                "1.0": {"ok": True, "summary": "ok"},
                "1.1": {"ok": False, "reason": "timeout"},
            },
        },
        cfg,
    )
    ids = [s["subtask"] for s in out["subtasks"]]
    # Carried-forward id for the re-dispatch; minted id for the new one.
    assert ids[0] == "1.1"  # carry-forward
    assert ids[1] == "2.1"  # minted f"{iteration}.{index}"


# --------------------------------------------------------------------------- #
# 9. Send fan-out dispatches N sub-agents through run_subagent
# --------------------------------------------------------------------------- #
async def test_send_fanout_dispatches_n_subagents():
    model = ScriptedSupervisorModel(
        [
            _decision(
                decision="continue",
                subtasks=[{"prompt": "p0"}, {"prompt": "p1"}, {"prompt": "p2"}],
            ),
            _decision(decision="stop"),  # round 2 → stop, so the loop ends
        ]
    )
    dispatched: list[str] = []

    async def fake_run(prompt, tools, **kwargs):
        dispatched.append(prompt)
        # depth=1 is passed by the Supervisor Send (Supervisor is depth 0).
        assert kwargs["depth"] == 1
        return SubagentResult.success(f"done {prompt}")

    cfg = _supervisor_config(model=model, fanout_deps=_FANOUT_DEPS)
    graph = _one_round_graph(cfg)
    with patch(
        "executor.supervisor.graph.run_subagent",
        AsyncMock(side_effect=fake_run),
    ):
        out = await graph.ainvoke(
            {"brief": "b", "iteration": 0, "subagent_results": {}},
            config={"configurable": cfg["configurable"]},
            durability="sync",
        )
    # S7 wraps each raw subtask prompt in the Subagent findings template, so each
    # raw subtask ("p0".."p2") is EMBEDDED in the dispatched prompt (not equal).
    # All three branches still fan out (the S6 structural assertion).
    assert len(dispatched) == 3
    assert sorted(p.split("Sub-task:\n")[1].split("\n")[0] for p in dispatched) == [
        "p0",
        "p1",
        "p2",
    ]
    assert set(out["subagent_results"].keys()) == {"1.0", "1.1", "1.2"}


# --------------------------------------------------------------------------- #
# 10. Crash resume-forward — completed siblings restored, only unfinished re-run
# --------------------------------------------------------------------------- #
async def test_crash_resume_forward_restores_completed_siblings():
    model = ScriptedSupervisorModel(
        [
            _decision(
                decision="continue",
                subtasks=[{"prompt": "p0"}, {"prompt": "p1"}],
            ),
            _decision(decision="stop"),
        ]
    )
    run_counts: dict[str, int] = {}
    crash = {"on": True}

    async def fake_run(prompt, tools, **kwargs):
        # S7 wraps the raw subtask in the Subagent template, so key the counters
        # on the embedded raw subtask ("p0" / "p1"), not the full prompt string.
        raw = prompt.split("Sub-task:\n")[1].split("\n")[0]
        run_counts[raw] = run_counts.get(raw, 0) + 1
        if crash["on"] and raw == "p1":
            raise RuntimeError("worker crash mid-fan-out")
        return SubagentResult.success(f"done {raw}")

    cfg = _supervisor_config(model=model, fanout_deps=_FANOUT_DEPS)
    checkpointer = MemorySaver()
    graph = _full_loop_graph(cfg, checkpointer)
    config = {"configurable": cfg["configurable"]}

    with patch(
        "executor.supervisor.graph.run_subagent",
        AsyncMock(side_effect=fake_run),
    ):
        crashed = False
        try:
            await graph.ainvoke(
                {"brief": "b", "iteration": 0, "subagent_results": {}},
                config=config,
                durability="sync",
            )
        except Exception:
            crashed = True
        assert crashed

        # The completed sibling p0 is persisted in the checkpoint.
        snap = await graph.aget_state(config)
        assert "1.0" in snap.values["subagent_results"]

        # Resume forward: only the unfinished branch re-runs.
        crash["on"] = False
        out = await graph.ainvoke(None, config=config, durability="sync")

    # p0 ran exactly once (restored, not recomputed); p1 ran twice (crash + resume).
    assert run_counts["p0"] == 1
    assert run_counts["p1"] == 2
    assert set(out["subagent_results"].keys()) == {"1.0", "1.1"}


# --------------------------------------------------------------------------- #
# 11. Oversized fan-out payload logs its byte size past threshold, not below
# --------------------------------------------------------------------------- #
def test_oversized_payload_logs_past_threshold(caplog):
    from executor.supervisor.graph import _maybe_log_oversized_payload

    small = {"1.0": {"ok": True, "summary": "x"}}
    with caplog.at_level("WARNING"):
        _maybe_log_oversized_payload(small, threshold_bytes=10_000)
    assert "checkpoint.oversized" not in caplog.text

    big = {f"k{i}": {"ok": True, "summary": "y" * 1000} for i in range(50)}
    caplog.clear()
    with caplog.at_level("WARNING"):
        _maybe_log_oversized_payload(big, threshold_bytes=10_000)
    assert "checkpoint.oversized" in caplog.text


# --------------------------------------------------------------------------- #
# 12. Fail-fast on missing fan-out deps — a clear config error, NOT a phantom
#     all-failed run (S8 is the next task to wire these deps).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "deps",
    [
        None,  # injection missing entirely
        {"checkpointer": MemorySaver()},  # model missing
        {"model": object()},  # checkpointer missing
    ],
)
async def test_missing_fanout_deps_raises_clear_config_error(deps):
    from executor.supervisor.graph import _fanout_node

    cfg = _supervisor_config(model=ScriptedSupervisorModel([]), fanout_deps=deps)
    with pytest.raises(SupervisorFanoutConfigError) as exc:
        await _fanout_node({"subtask": "1.0", "prompt": "p"}, cfg)
    # The error names the missing key (model / checkpointer) — not a misleading
    # "all sub-agents failed". ``run_subagent`` is NOT reached.
    msg = str(exc.value)
    assert "supervisor fan-out deps not injected" in msg
    assert ("model" in msg) or ("checkpointer" in msg)


# --------------------------------------------------------------------------- #
# Test graph builders — exercise the S6 sub-wiring through a real compiled graph
# --------------------------------------------------------------------------- #
def _one_round_graph(cfg: dict):
    """The real Supervisor sub-wiring (START → supervisor → Send(fanout) →
    gather → iteration edge), ending at ``END`` (no Writer seam wired).

    The model scripts a ``stop`` for round 2, so the loop runs exactly one
    fan-out round and then routes ``gather → supervisor → END``; the test reads
    the accumulated ``subagent_results`` from the final state.
    """
    g = StateGraph(SupervisorState)
    add_supervisor_fanout(g)
    g.add_edge(START, SUPERVISOR_NODE_NAME)
    # ``durability="sync"`` (the worker runtime convention) requires a
    # checkpointer; a MemorySaver keeps the test in-process / DB-free.
    return g.compile(checkpointer=MemorySaver())


def _full_loop_graph(cfg: dict, checkpointer):
    """The real Supervisor sub-wiring ending at a stub Writer seam (S7). Used for
    the crash-resume test (needs a checkpointer)."""
    g = StateGraph(SupervisorState)
    add_supervisor_fanout(g, writer_node_name="writer")

    async def writer_stub(state):
        return {}

    g.add_node("writer", writer_stub)
    g.add_edge(START, SUPERVISOR_NODE_NAME)
    g.add_edge("writer", END)
    return g.compile(checkpointer=checkpointer)


# Imported lazily at module scope so a collection error surfaces the missing
# symbol clearly during RED.
from executor.supervisor.nodes import SupervisorAllFailedError  # noqa: E402
