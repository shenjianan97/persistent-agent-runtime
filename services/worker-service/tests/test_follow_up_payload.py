"""Tests for follow-up payload decoding in run_astream().

Verifies that kind='follow_up' injects a HumanMessage rather than a Command(resume=...).
"""

import json
import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import Command


def decode_human_response(human_response_json: str):
    """Replicate the payload decoding logic from run_astream() for unit testing.

    NOTE: This duplicates decode logic from GraphExecutor.run_astream() because
    the production code is embedded in a method and not independently importable.
    """
    payload = json.loads(human_response_json)
    if payload.get("kind") == "follow_up":
        initial_input = {"messages": [HumanMessage(content=payload.get("message", ""))]}
    elif payload.get("kind") == "input":
        resume_value = payload.get("message", "")
        initial_input = Command(resume=resume_value)
    else:
        resume_value = payload
        initial_input = Command(resume=resume_value)
    return initial_input


class TestFollowUpPayloadDecoding:
    """Verify that kind='follow_up' injects HumanMessage, not Command(resume=...)."""

    def test_follow_up_injects_human_message(self):
        """kind='follow_up' should produce a messages dict with a HumanMessage."""
        payload = json.dumps({"kind": "follow_up", "message": "What happened next?"})
        result = decode_human_response(payload)

        assert isinstance(result, dict), "Expected a dict with 'messages' key"
        assert "messages" in result
        assert len(result["messages"]) == 1
        msg = result["messages"][0]
        assert isinstance(msg, HumanMessage)
        assert msg.content == "What happened next?"

    def test_follow_up_empty_message_uses_empty_string(self):
        """kind='follow_up' with missing message defaults to empty string."""
        payload = json.dumps({"kind": "follow_up"})
        result = decode_human_response(payload)

        assert isinstance(result, dict)
        assert result["messages"][0].content == ""

    def test_input_kind_produces_command_resume(self):
        """kind='input' should produce a Command(resume=message) — existing behavior unchanged."""
        payload = json.dumps({"kind": "input", "message": "blue"})
        result = decode_human_response(payload)

        assert isinstance(result, Command)
        assert result.resume == "blue"

    def test_approval_kind_produces_command_resume_with_payload(self):
        """kind='approval' should pass the whole payload as Command(resume=payload) — existing behavior unchanged."""
        approval_payload = {"kind": "approval", "approved": True}
        payload = json.dumps(approval_payload)
        result = decode_human_response(payload)

        assert isinstance(result, Command)
        assert result.resume == approval_payload

    def test_follow_up_is_not_command(self):
        """Ensure follow_up never produces a Command — would break the conversation flow."""
        payload = json.dumps({"kind": "follow_up", "message": "continue"})
        result = decode_human_response(payload)

        assert not isinstance(result, Command), "follow_up must not produce Command(resume=...)"
