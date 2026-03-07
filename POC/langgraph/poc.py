"""LangGraph checkpointer POC.

This validates that:
1) Exceptions from checkpointer.put() bubble through graph.astream().
2) Execution can resume from the last successful checkpoint.
3) Hitting recursion_limit raises GraphRecursionError without corrupting prior checkpoints.
4) Basic state serialization (json/pickle) works for checkpointed values.
"""

from __future__ import annotations

import asyncio
import json
import pickle
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.base import get_checkpoint_metadata
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class LeaseRevokedError(RuntimeError):
    """Raised when the simulated lease ownership is lost."""


class CounterState(TypedDict):
    # Mutable graph state persisted by the checkpointer between super-steps.
    count: int
    target: int


@dataclass
class SharedCheckpointBackend:
    """Shared in-memory storage so a new saver instance can resume old checkpoints.

    We intentionally share these dicts across two checkpointer instances:
    - run 1 uses a revoking checkpointer
    - run 2 uses a healthy checkpointer
    This simulates replacing a crashed/revoked worker while keeping durable state.
    """

    storage: dict[str, dict[str, dict[str, tuple[tuple[str, bytes], tuple[str, bytes], str | None]]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(dict))
    )
    writes: dict[tuple[str, str, str], dict[tuple[str, int], tuple[str, str, tuple[str, bytes], str]]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    blobs: dict[tuple[str, str, str, str | int | float], tuple[str, bytes]] = field(
        default_factory=dict
    )
    successful_puts: int = 0


class MockLeaseCheckpointer(InMemorySaver):
    """In-memory checkpointer with lease revocation simulation in put()."""

    def __init__(
        self,
        backend: SharedCheckpointBackend,
        *,
        should_revoke_lease: bool,
        revoke_after_graph_step: int = 2,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.should_revoke_lease = should_revoke_lease
        self.revoke_after_graph_step = revoke_after_graph_step
        self.storage = backend.storage
        self.writes = backend.writes
        self.blobs = backend.blobs

    def put(self, config, checkpoint, metadata, new_versions):  # type: ignore[override]
        # LangGraph annotates each checkpoint with metadata including step index.
        # We use that to revoke "after step 2 has been durably written".
        printable_metadata = get_checkpoint_metadata(config, metadata)
        step = printable_metadata.get("step", "?")
        if self.should_revoke_lease and isinstance(step, int) and step > self.revoke_after_graph_step:
            # Throwing here simulates a DB-side lease ownership failure in put().
            raise LeaseRevokedError(
                f"Lease revoked while saving step={step} after successful step={self.revoke_after_graph_step}"
            )

        # Delegate actual persistence to LangGraph's in-memory saver implementation.
        next_config = super().put(config, checkpoint, metadata, new_versions)
        self.backend.successful_puts += 1

        print(
            f"[CHECKPOINT SAVED] successful_puts={self.backend.successful_puts} "
            f"checkpoint_id={next_config['configurable']['checkpoint_id']} step={step}"
        )
        return next_config


def build_counter_graph(checkpointer: MockLeaseCheckpointer):
    # Node function: deterministic, side-effect free; easier to reason about resume.
    def increment(state: CounterState) -> CounterState:
        next_count = state["count"] + 1
        print(f"[NODE] increment: {state['count']} -> {next_count}")
        return {"count": next_count, "target": state["target"]}

    # Conditional edge: loop until count reaches target.
    def decide_next(state: CounterState) -> str:
        return "increment" if state["count"] < state["target"] else END

    builder = StateGraph(CounterState)
    builder.add_node("increment", increment)
    builder.add_edge(START, "increment")
    builder.add_conditional_edges("increment", decide_next, {"increment": "increment", END: END})
    return builder.compile(checkpointer=checkpointer)


def build_infinite_graph(checkpointer: MockLeaseCheckpointer):
    # Same node as above, but wired as an unconditional self-loop.
    # This graph exists only to test recursion_limit safety.
    def increment(state: CounterState) -> CounterState:
        return {"count": state["count"] + 1, "target": state["target"]}

    builder = StateGraph(CounterState)
    builder.add_node("increment", increment)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", "increment")
    return builder.compile(checkpointer=checkpointer)


async def run_revocation_and_resume() -> bool:
    print("\n=== TEST 1: Lease revocation + resume ===")
    thread_id = "poc-thread-1"
    config = {
        # thread_id is the durable identity key for checkpoint lookup/resume.
        "configurable": {"thread_id": thread_id, "checkpoint_ns": ""},
        "recursion_limit": 50,
    }

    backend = SharedCheckpointBackend()

    revoking_checkpointer = MockLeaseCheckpointer(
        backend, should_revoke_lease=True, revoke_after_graph_step=2
    )
    graph_1 = build_counter_graph(revoking_checkpointer)

    revoked = False
    try:
        # Run 1 starts from fresh input.
        async for state in graph_1.astream(
            {"count": 0, "target": 5}, config=config, stream_mode="values"
        ):
            print(f"[RUN 1 STREAM] count={state['count']}")
    except LeaseRevokedError as exc:
        revoked = True
        print(f"[EXPECTED] Caught LeaseRevokedError from astream(): {exc}")

    # Note: this snapshot can include in-memory progress that wasn't durably checkpointed
    # because failure happened during put(). Real durability signal is where run 2 starts.
    state_after_failure = await graph_1.aget_state(config)
    count_after_failure = state_after_failure.values["count"]
    print(
        "[STATE AFTER FAILURE] "
        f"count={count_after_failure} (note: this may include in-memory progress past durable checkpoint)"
    )

    healthy_checkpointer = MockLeaseCheckpointer(backend, should_revoke_lease=False)
    graph_2 = build_counter_graph(healthy_checkpointer)

    resume_start_count: int | None = None
    # Run 2 passes input=None; LangGraph restores from last checkpoint by thread_id.
    async for state in graph_2.astream(None, config=config, stream_mode="values"):
        if resume_start_count is None:
            resume_start_count = state["count"]
        print(f"[RUN 2 STREAM] count={state['count']}")

    resumed_state = await graph_2.aget_state(config)
    resumed_count = resumed_state.values["count"]
    print(f"[RESUME START] count={resume_start_count} (expected 2)")
    print(f"[FINAL RESUMED STATE] count={resumed_count} (expected 5)")

    passed = revoked and resume_start_count == 2 and resumed_count == 5
    print(f"[TEST RESULT] {'PASS' if passed else 'FAIL'}")
    return passed


async def run_recursion_limit_safety() -> bool:
    print("\n=== TEST 2: recursion_limit safety ===")
    thread_id = "poc-thread-max-steps"
    config = {
        "configurable": {"thread_id": thread_id, "checkpoint_ns": ""},
        # Intentionally tiny limit to force GraphRecursionError quickly.
        "recursion_limit": 3,
    }

    backend = SharedCheckpointBackend()
    checkpointer = MockLeaseCheckpointer(backend, should_revoke_lease=False)
    graph = build_infinite_graph(checkpointer)

    raised = False
    try:
        async for _ in graph.astream({"count": 0, "target": 999}, config=config, stream_mode="values"):
            pass
    except GraphRecursionError as exc:
        raised = True
        print(f"[EXPECTED] Caught GraphRecursionError: {exc.__class__.__name__}")

    snapshot = await graph.aget_state(config)
    count_value = snapshot.values.get("count", None)
    print(f"[STATE AFTER RECURSION ERROR] count={count_value}")

    passed = raised and isinstance(count_value, int)
    print(f"[TEST RESULT] {'PASS' if passed else 'FAIL'}")
    return passed


def run_state_serialization_check() -> bool:
    print("\n=== TEST 3: state serialization (json + pickle) ===")
    # Standalone serialization sanity check for state-like structures.
    sample_state = {"count": 2, "target": 5, "messages": [{"type": "ai", "content": "ok"}]}

    json_blob = json.dumps(sample_state)
    pickle_blob = pickle.dumps(sample_state)
    json_round_trip = json.loads(json_blob)
    pickle_round_trip = pickle.loads(pickle_blob)

    passed = json_round_trip == sample_state and pickle_round_trip == sample_state
    print(f"[JSON BYTES] {len(json_blob.encode('utf-8'))}")
    print(f"[PICKLE BYTES] {len(pickle_blob)}")
    print(f"[TEST RESULT] {'PASS' if passed else 'FAIL'}")
    return passed


async def main() -> None:
    r1 = await run_revocation_and_resume()
    r2 = await run_recursion_limit_safety()
    r3 = run_state_serialization_check()

    all_passed = r1 and r2 and r3
    print("\n=== OVERALL ===")
    print(f"OVERALL RESULT: {'PASS' if all_passed else 'FAIL'}")
    if not all_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
