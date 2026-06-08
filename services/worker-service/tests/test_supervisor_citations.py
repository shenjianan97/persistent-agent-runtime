"""Unit tests for the Subagent findings contract, one-shot Writer, deterministic
citation binding, and the v1 Writer-context reduction (Task S7).

All tests here are **fake-model, no-network, no-DB** — they exercise
``citations.resolve`` / ``citations.verify``, ``writer_node``, the finding parse
in ``parse_findings`` / ``_fanout_node``, and the v1 reduction directly. No
Postgres, no TCP ports, no server subprocess → worktree-concurrency-safe.

Coverage maps 1:1 to the S7 acceptance criteria:

* the subagent finding parse yields structured
  ``{finding_id, claim, source_url, supporting_quote}`` records, with stable +
  unique ids minted by the runtime (never trusted from the model);
* each parsed finding emits a ``subagent_finding`` event
  ``{iteration, subtask, finding_id, source_url}`` via the S9 emit helper;
* **immutability** — a corpus > cap fed through the v1 reduction leaves every
  surviving ``supporting_quote`` byte-identical to its input; no S7 path rewrites
  a quote;
* ``citations.resolve`` resolves a valid id → citation with **no LLM**; an
  unknown id surfaces a **render error flag** (sentinel), never a fabricated
  source;
* ``citations.verify`` flags a cited sentence whose finding quote does NOT
  support it and passes one that does — with a fake verify model; it never
  rewrites the report or invents a source;
* the one-shot Writer cites by ``finding_id`` only (no inline source URLs in the
  model output) and respects ``writer_style`` (``formal_report`` vs.
  ``annotated_bullets``);
* the v1 reduction (``reduce_findings``) drops past ``WRITER_FINDINGS_CAP``,
  selects/reorders only, ``log()``s the dropped ids + count, and leaves the kept
  quotes unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from executor.supervisor import citations
from executor.supervisor.citations import (
    RENDER_ERROR_FLAG,
    resolve,
    verify,
)
from executor.supervisor.nodes import (
    WRITER_FINDINGS_CAP,
    parse_findings,
    reduce_findings,
    writer_node,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class ScriptedModel:
    """A chat model returning canned ``AIMessage`` bodies in call order.

    The Writer / verify nodes make exactly one ``ainvoke`` per call; the verify
    pass may make one per cited sentence depending on the implementation. The
    ``calls`` list records every prompt seen so a test can assert the model was
    invoked (and how often)."""

    def __init__(self, *bodies: str):
        self._bodies = list(bodies)
        self.calls: list[Any] = []

    async def ainvoke(self, messages, *args, **kwargs):
        self.calls.append(messages)
        body = self._bodies.pop(0) if self._bodies else ""
        return AIMessage(content=body)


def _finding(fid: str, *, quote: str, claim: str = "c", url: str = "https://x") -> dict:
    return {
        "finding_id": fid,
        "claim": claim,
        "source_url": url,
        "supporting_quote": quote,
    }


# --------------------------------------------------------------------------- #
# parse_findings — structured findings contract
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_parse_findings_yields_structured_records_with_minted_ids():
    summary = json.dumps(
        {
            "findings": [
                {"claim": "Sky is blue", "source_url": "https://a", "supporting_quote": "the sky is blue"},
                {"claim": "Grass is green", "source_url": "https://b", "supporting_quote": "grass is green"},
            ]
        }
    )
    out = await parse_findings(summary, iteration=1, subtask="1.0")
    assert len(out) == 2
    for f in out:
        assert set(f) >= {"finding_id", "claim", "source_url", "supporting_quote"}
    # Ids are minted by the runtime, stable + unique within the run, NOT trusted
    # from the model (the model output above carried no id).
    ids = [f["finding_id"] for f in out]
    assert len(set(ids)) == 2
    assert out[0]["claim"] == "Sky is blue"
    assert out[0]["supporting_quote"] == "the sky is blue"


@pytest.mark.asyncio
async def test_parse_findings_ids_are_stable_and_subtask_scoped():
    summary = json.dumps({"findings": [{"claim": "c", "source_url": "u", "supporting_quote": "q"}]})
    a = await parse_findings(summary, iteration=2, subtask="2.3")
    b = await parse_findings(summary, iteration=2, subtask="2.4")
    # Different subtasks must not collide on id (run-unique).
    assert a[0]["finding_id"] != b[0]["finding_id"]
    # Same inputs reproduce the same id (deterministic / stable).
    again = await parse_findings(summary, iteration=2, subtask="2.3")
    assert again[0]["finding_id"] == a[0]["finding_id"]


@pytest.mark.asyncio
async def test_parse_findings_tolerates_unparseable_summary():
    # A sub-agent that returned freeform prose (no JSON) yields zero findings —
    # never raises into the graph.
    out = await parse_findings("I could not find anything useful.", iteration=1, subtask="1.0")
    assert out == []


@pytest.mark.asyncio
async def test_parse_findings_emits_subagent_finding_event_per_finding():
    summary = json.dumps(
        {
            "findings": [
                {"claim": "c1", "source_url": "https://a", "supporting_quote": "q1"},
                {"claim": "c2", "source_url": "https://b", "supporting_quote": "q2"},
            ]
        }
    )
    events: list[tuple[str, dict]] = []

    async def fake_emit(event_type: str, details: dict) -> None:
        events.append((event_type, details))

    out = await parse_findings(summary, iteration=3, subtask="3.1", emit=fake_emit)
    assert len(out) == 2
    assert len(events) == 2
    for (etype, details), f in zip(events, out):
        assert etype == "subagent_finding"
        assert details["iteration"] == 3
        assert details["subtask"] == "3.1"
        assert details["finding_id"] == f["finding_id"]
        assert details["source_url"] == f["source_url"]
        # claim / quote ride the span, not the row (§A7) — not in the event.
        assert "supporting_quote" not in details
        assert "claim" not in details


# --------------------------------------------------------------------------- #
# citations.resolve — deterministic, no LLM, no fabrication
# --------------------------------------------------------------------------- #
def test_resolve_hit_returns_citation_no_llm():
    findings = [_finding("f1", quote="the quote", url="https://src")]
    cit = resolve("f1", findings)
    assert cit["finding_id"] == "f1"
    assert cit["source_url"] == "https://src"
    assert cit["supporting_quote"] == "the quote"
    assert cit.get("error") in (None, False)


def test_resolve_miss_returns_render_error_flag_never_fabricates():
    findings = [_finding("f1", quote="q")]
    cit = resolve("does-not-exist", findings)
    assert cit["error"] == RENDER_ERROR_FLAG
    assert cit["finding_id"] == "does-not-exist"
    # No fabricated source / quote.
    assert not cit.get("source_url")
    assert not cit.get("supporting_quote")


# --------------------------------------------------------------------------- #
# citations.verify — flags-without-rewriting, no fabrication, one LLM pass
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_verify_flags_unsupported_passes_supported():
    findings = [
        _finding("f1", quote="The Eiffel Tower is in Paris."),
        _finding("f2", quote="The Eiffel Tower is 330 metres tall."),
    ]
    # Report cites f1 for a sentence the quote supports, f2 for one it does not.
    report = (
        "The Eiffel Tower is located in Paris [f1]. "
        "It was the tallest building until 1930 [f2]."
    )

    # verify runs the per-citation calls CONCURRENTLY, so a call-order-keyed fake
    # would be flaky — key the reply on the quote in the prompt instead.
    class ContentKeyedModel:
        async def ainvoke(self, messages, *args, **kwargs):
            supported = "330 metres" not in str(messages)
            return AIMessage(content=json.dumps({"supported": supported}))

    flags = await verify(report, findings, model=ContentKeyedModel())
    by_id = {f["finding_id"]: f for f in flags}
    assert by_id["f1"]["supported"] is True
    assert by_id["f2"]["supported"] is False
    # verify never rewrites the report — it only returns flags.
    assert isinstance(flags, list)


@pytest.mark.asyncio
async def test_verify_does_not_invent_source_for_unknown_id():
    findings = [_finding("f1", quote="known quote")]
    report = "An unsupported sentence [f99]."
    model = ScriptedModel(json.dumps({"supported": True}))
    flags = await verify(report, findings, model=model)
    by_id = {f["finding_id"]: f for f in flags}
    # Unknown id is surfaced as a render error flag, not fabricated / verified.
    assert by_id["f99"]["error"] == RENDER_ERROR_FLAG
    assert by_id["f99"].get("supported") is not True


@pytest.mark.asyncio
async def test_verify_call_failure_degrades_gracefully():
    """One per-citation verify call raising (timeout / rate-limit / transport)
    must NOT sink the already-generated report: that one citation is flagged
    fail-safe (``supported: True``, matching ``_parse_supported``'s contract —
    verify only flags, never gates), and the other citations + the report
    survive."""
    findings = [
        _finding("f1", quote="quote one"),
        _finding("f2", quote="quote two"),
        _finding("f3", quote="quote three"),
    ]
    report = "Alpha [f1]. Beta [f2]. Gamma [f3]."

    class FlakyModel:
        async def ainvoke(self, messages, *args, **kwargs):
            # The f2 verify call raises; f1 / f3 succeed.
            if "quote two" in str(messages):
                raise RuntimeError("verify model transport error")
            return AIMessage(content=json.dumps({"supported": True}))

    flags = await verify(report, findings, model=FlakyModel())
    by_id = {f["finding_id"]: f for f in flags}
    # All three citations are represented — the raise did not abort verify.
    assert set(by_id) == {"f1", "f2", "f3"}
    assert by_id["f1"]["supported"] is True
    assert by_id["f3"]["supported"] is True
    # The failed call degrades fail-safe (assume supported); never raises out.
    assert by_id["f2"]["supported"] is True


# --------------------------------------------------------------------------- #
# reduce_findings — v1 hard cap, select/reorder only, IMMUTABLE quotes
# --------------------------------------------------------------------------- #
def test_reduce_findings_caps_and_logs_dropped(caplog):
    corpus = [
        _finding(f"f{i}", quote=f"quote number {i}", claim=f"claim {i}")
        for i in range(WRITER_FINDINGS_CAP + 5)
    ]
    with caplog.at_level(logging.INFO):
        kept = reduce_findings(corpus)
    assert len(kept) == WRITER_FINDINGS_CAP
    # Dropped ids + count are logged (no silent truncation, §A7).
    text = caplog.text
    assert "writer.findings_reduced" in text or "dropped" in text.lower()


def test_reduce_findings_under_cap_is_identity():
    corpus = [_finding(f"f{i}", quote=f"q{i}") for i in range(3)]
    kept = reduce_findings(corpus)
    assert kept == corpus


def test_reduce_findings_never_mutates_supporting_quote():
    # Feed a corpus > cap through the reduction; assert every surviving quote is
    # byte-identical to its input (the core S7 immutability invariant, §A0 inv.4).
    corpus = [
        _finding(f"f{i}", quote=f"‘immutable quote {i} with unicode é’")
        for i in range(WRITER_FINDINGS_CAP + 10)
    ]
    original_by_id = {f["finding_id"]: f["supporting_quote"] for f in corpus}
    kept = reduce_findings(corpus)
    assert len(kept) == WRITER_FINDINGS_CAP
    for f in kept:
        assert f["supporting_quote"] == original_by_id[f["finding_id"]]
        # byte-identical
        assert f["supporting_quote"].encode("utf-8") == original_by_id[
            f["finding_id"]
        ].encode("utf-8")


# --------------------------------------------------------------------------- #
# writer_node — one-shot, cites by finding_id only, writer_style switch
# --------------------------------------------------------------------------- #
def _writer_config(model, *, writer_style: str, verify_model=None) -> dict:
    return {
        "configurable": {
            "writer_model": model,
            "verify_model": verify_model or model,
            "agent_config": {"supervisor": {"writer_style": writer_style}},
        }
    }


@pytest.mark.asyncio
async def test_writer_node_one_shot_cites_by_id_only():
    findings = [
        _finding("f1", quote="quote one", url="https://a"),
        _finding("f2", quote="quote two", url="https://b"),
    ]
    state = {"brief": "Research the thing.", "findings": findings}
    # Writer emits prose citing by finding_id only, no inline source URLs.
    writer_model = ScriptedModel("The thing is real [f1]. It is large [f2].")
    verify_model = ScriptedModel(
        json.dumps({"supported": True}), json.dumps({"supported": True})
    )
    out = await writer_node(
        state, _writer_config(writer_model, writer_style="formal_report", verify_model=verify_model)
    )
    report = out["report"]
    assert "[f1]" in report and "[f2]" in report
    # No inline source URL leaked from the model output.
    assert "https://a" not in report and "https://b" not in report
    # Exactly ONE writer call (one-shot, not parallel section-writing).
    assert len(writer_model.calls) == 1
    # verify flags + resolved citations attached for downstream render.
    assert "verify_flags" in out
    assert "citations" in out
    resolved = {c["finding_id"]: c for c in out["citations"]}
    assert resolved["f1"]["source_url"] == "https://a"


@pytest.mark.asyncio
async def test_writer_node_writer_style_switches_template():
    findings = [_finding("f1", quote="q")]
    state = {"brief": "b", "findings": findings}
    formal_model = ScriptedModel("Report body [f1].")
    bullets_model = ScriptedModel("- point [f1]")
    verify_ok = lambda: ScriptedModel(json.dumps({"supported": True}))

    await writer_node(state, _writer_config(formal_model, writer_style="formal_report", verify_model=verify_ok()))
    await writer_node(state, _writer_config(bullets_model, writer_style="annotated_bullets", verify_model=verify_ok()))

    formal_prompt = str(formal_model.calls[0])
    bullets_prompt = str(bullets_model.calls[0])
    # The two styles render distinct instructions to the Writer.
    assert formal_prompt != bullets_prompt
    assert "report" in formal_prompt.lower()
    assert "bullet" in bullets_prompt.lower()


# --------------------------------------------------------------------------- #
# Findings-into-state wiring — _fanout_node parses a sub-agent summary into the
# append-only ``findings`` channel (the S6/S7 integration seam).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fanout_node_routes_findings_into_state(monkeypatch):
    """A successful sub-agent's structured summary flows into ``state['findings']``
    alongside the ``subagent_results`` marker, with a ``subagent_finding`` event
    per finding."""
    from unittest.mock import AsyncMock

    from langgraph.checkpoint.memory import MemorySaver

    from executor.subagents import SubagentCeiling, SubagentResult
    from executor.supervisor import graph as sgraph

    summary = json.dumps(
        {
            "findings": [
                {"claim": "c1", "source_url": "https://a", "supporting_quote": "q1"},
                {"claim": "c2", "source_url": "https://b", "supporting_quote": "q2"},
            ]
        }
    )

    async def fake_run(prompt, tools, **kwargs):
        # The wrapped Subagent findings template must reach the sub-agent.
        assert "supporting_quote" in prompt
        return SubagentResult.success(summary)

    events: list[tuple[str, dict]] = []

    async def emit(event_type, details):
        events.append((event_type, details))

    monkeypatch.setattr(sgraph, "run_subagent", AsyncMock(side_effect=fake_run))

    deps = {
        "model": object(),
        "checkpointer": MemorySaver(),
        "ceiling": SubagentCeiling(max_turns=4, max_tokens=10_000),
        "tools": [],
        "emit": emit,
    }
    config = {
        "configurable": {
            "supervisor_fanout_deps": deps,
            "thread_id": "t",
            "iteration": 2,
        }
    }
    out = await sgraph._fanout_node({"subtask": "2.0", "prompt": "find things"}, config)

    # subagent_results marker still written (S6 contract intact).
    assert out["subagent_results"]["2.0"]["ok"] is True
    # findings flow into the append-only channel (S7).
    findings = out["findings"]
    assert len(findings) == 2
    assert {f["claim"] for f in findings} == {"c1", "c2"}
    # subagent_finding event per finding, carrying iteration + subtask.
    finding_events = [e for e in events if e[0] == "subagent_finding"]
    assert len(finding_events) == 2
    assert all(d["iteration"] == 2 and d["subtask"] == "2.0" for _, d in finding_events)


@pytest.mark.asyncio
async def test_fanout_node_failed_subagent_contributes_no_findings(monkeypatch):
    """A failed sub-agent writes a failure marker and contributes zero findings —
    the failure does not strand or fabricate the ``findings`` channel."""
    from unittest.mock import AsyncMock

    from langgraph.checkpoint.memory import MemorySaver

    from executor.subagents import SubagentCeiling, SubagentResult
    from executor.supervisor import graph as sgraph

    async def fake_run(prompt, tools, **kwargs):
        return SubagentResult.failure("ceiling")

    monkeypatch.setattr(sgraph, "run_subagent", AsyncMock(side_effect=fake_run))
    deps = {
        "model": object(),
        "checkpointer": MemorySaver(),
        "ceiling": SubagentCeiling(max_turns=4, max_tokens=10_000),
        "tools": [],
        "emit": None,
    }
    config = {"configurable": {"supervisor_fanout_deps": deps, "thread_id": "t"}}
    out = await sgraph._fanout_node({"subtask": "1.0", "prompt": "p"}, config)
    assert out["subagent_results"]["1.0"]["ok"] is False
    assert out["findings"] == []
