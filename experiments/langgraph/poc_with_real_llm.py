"""LangGraph checkpointer POC with real OpenAI calls.

Reads OPENAI_API_KEY from .env and runs a looping graph where each step
calls the model, then increments a counter. It validates:
1) checkpointer.put() can revoke lease and raise,
2) astream() bubbles that exception,
3) second run with same thread_id resumes from durable checkpoint.
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from langgraph.checkpoint.base import get_checkpoint_metadata
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from typing_extensions import TypedDict


class LeaseRevokedError(RuntimeError):
    """Raised when the simulated lease ownership is lost."""


class LLMState(TypedDict):
    # Graph state plus the last model output for observability.
    count: int
    target: int
    prompt: str
    last_model_text: str


@dataclass
class SharedCheckpointBackend:
    # Shared memory backend so run 2 can resume from run 1 checkpoints.
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
        # Reuse the same lease-revocation model as poc.py, but now with real LLM calls.
        printable_metadata = get_checkpoint_metadata(config, metadata)
        step = printable_metadata.get("step", "?")
        if self.should_revoke_lease and isinstance(step, int) and step > self.revoke_after_graph_step:
            raise LeaseRevokedError(
                f"Lease revoked while saving step={step} after successful step={self.revoke_after_graph_step}"
            )

        next_config = super().put(config, checkpoint, metadata, new_versions)
        self.backend.successful_puts += 1
        print(
            f"[CHECKPOINT SAVED] successful_puts={self.backend.successful_puts} "
            f"checkpoint_id={next_config['configurable']['checkpoint_id']} step={step}"
        )
        return next_config


async def build_and_run_graph() -> None:
    # Load .env from current working directory (experiments/langgraph).
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing. Put it in experiments/langgraph/.env")

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    client = AsyncOpenAI(api_key=api_key)

    async def llm_and_increment(state: LLMState) -> LLMState:
        # This node is where a real external side-effect happens (OpenAI API call).
        # In production, this is exactly why durable checkpoint/resume matters.
        next_count = state["count"] + 1
        prompt = (
            f"{state['prompt']}\\n"
            f"Return exactly one short line: step={next_count}"
        )
        response = await client.responses.create(
            model=model,
            input=prompt,
            max_output_tokens=40,
        )
        text = (response.output_text or "").strip()
        print(f"[LLM NODE] count {state['count']} -> {next_count}, text={text!r}")
        return {
            "count": next_count,
            "target": state["target"],
            "prompt": state["prompt"],
            "last_model_text": text,
        }

    def decide_next(state: LLMState) -> str:
        # Loop until target is reached.
        return "llm_and_increment" if state["count"] < state["target"] else END

    def compile_graph(checkpointer: MockLeaseCheckpointer):
        # Build a fresh compiled graph bound to the supplied checkpointer.
        builder = StateGraph(LLMState)
        builder.add_node("llm_and_increment", llm_and_increment)
        builder.add_edge(START, "llm_and_increment")
        builder.add_conditional_edges(
            "llm_and_increment", decide_next, {"llm_and_increment": "llm_and_increment", END: END}
        )
        return builder.compile(checkpointer=checkpointer)

    thread_id = "poc-thread-real-llm"
    config = {
        # Same thread_id across runs = same durable execution stream.
        "configurable": {"thread_id": thread_id, "checkpoint_ns": ""},
        "recursion_limit": 30,
    }

    backend = SharedCheckpointBackend()

    print("\\n=== RUN 1 (revoking) ===")
    revoking_checkpointer = MockLeaseCheckpointer(
        backend, should_revoke_lease=True, revoke_after_graph_step=2
    )
    graph_1 = compile_graph(revoking_checkpointer)

    revoked = False
    try:
        # Run 1 starts with explicit input and intentionally revokes in put().
        async for state in graph_1.astream(
            {
                "count": 0,
                "target": 4,
                "prompt": "You are a concise assistant.",
                "last_model_text": "",
            },
            config=config,
            stream_mode="values",
        ):
            print(f"[RUN 1 STREAM] count={state['count']}")
    except LeaseRevokedError as exc:
        revoked = True
        print(f"[EXPECTED] Caught LeaseRevokedError from astream(): {exc}")

    print("\\n=== RUN 2 (resume, healthy) ===")
    healthy_checkpointer = MockLeaseCheckpointer(backend, should_revoke_lease=False)
    graph_2 = compile_graph(healthy_checkpointer)

    resume_start_count: int | None = None
    # Run 2 resumes with input=None; first streamed value reveals checkpoint start point.
    async for state in graph_2.astream(None, config=config, stream_mode="values"):
        if resume_start_count is None:
            resume_start_count = state["count"]
        print(f"[RUN 2 STREAM] count={state['count']} last_model_text={state['last_model_text']!r}")

    final_state = await graph_2.aget_state(config)
    final_count = final_state.values["count"]

    print("\\n=== RESULT ===")
    print(f"revoked={revoked}")
    print(f"resume_start_count={resume_start_count} (expected 2)")
    print(f"final_count={final_count} (expected 4)")
    passed = revoked and resume_start_count == 2 and final_count == 4
    print(f"OVERALL RESULT: {'PASS' if passed else 'FAIL'}")
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(build_and_run_graph())
