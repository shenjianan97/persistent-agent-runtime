"""Unit tests for executor.compaction.caps — Per-tool-result byte cap.

Test-first (TDD): written before implementation to specify correct behaviour.
"""
from __future__ import annotations

import pytest

# Will fail until caps.py is implemented — that is the expected RED state.
from executor.compaction.caps import CapEvent, cap_tool_result
from executor.compaction.defaults import PER_TOOL_RESULT_CAP_BYTES


# ---------------------------------------------------------------------------
# 1. Under-cap: pass-through
# ---------------------------------------------------------------------------

class TestUnderCap:
    def test_short_string_unchanged(self):
        result, event = cap_tool_result("hello", "web_search")
        assert result == "hello"
        assert event is None

    def test_exact_cap_boundary_not_truncated(self):
        """A string whose UTF-8 encoding is exactly PER_TOOL_RESULT_CAP_BYTES passes through."""
        raw = "a" * PER_TOOL_RESULT_CAP_BYTES
        result, event = cap_tool_result(raw, "sandbox_exec")
        assert result == raw
        assert event is None

    def test_empty_string_passes_through(self):
        result, event = cap_tool_result("", "web_search")
        assert result == ""
        assert event is None

    def test_ascii_1kb_unchanged(self):
        raw = "x" * 1000
        result, event = cap_tool_result(raw, "sandbox_read_file")
        assert result == raw
        assert event is None


# ---------------------------------------------------------------------------
# 2. Over-cap: head+tail truncation
# ---------------------------------------------------------------------------

class TestOverCap:
    def test_30k_string_is_capped(self):
        """30 000 bytes > 25 000 cap → should return capped result and CapEvent."""
        raw = "x" * 30_000
        result, event = cap_tool_result(raw, "web_search")
        assert event is not None
        assert event.orig_bytes == 30_000
        assert event.tool == "web_search"

    def test_capped_result_shorter_than_original(self):
        raw = "y" * 30_000
        result, event = cap_tool_result(raw, "sandbox_read_file")
        assert len(result.encode("utf-8")) < len(raw.encode("utf-8"))

    def test_hard_cap_invariant_30k(self):
        """Capped output MUST NOT exceed PER_TOOL_RESULT_CAP_BYTES."""
        raw = "z" * 30_000
        result, event = cap_tool_result(raw, "sandbox_exec")
        assert len(result.encode("utf-8")) <= PER_TOOL_RESULT_CAP_BYTES

    def test_hard_cap_invariant_500k(self):
        """500 KB input still respects the hard cap."""
        raw = "a" * 500_000
        result, event = cap_tool_result(raw, "web_search")
        assert len(result.encode("utf-8")) <= PER_TOOL_RESULT_CAP_BYTES

    def test_cap_event_fields(self):
        raw = "m" * 30_000
        result, event = cap_tool_result(raw, "memory_search")
        assert isinstance(event, CapEvent)
        assert event.tool == "memory_search"
        assert event.orig_bytes == 30_000
        assert event.capped_bytes == len(result.encode("utf-8"))

    def test_cap_event_capped_bytes_matches_result(self):
        raw = "n" * 50_000
        result, event = cap_tool_result(raw, "task_history_get")
        assert event.capped_bytes == len(result.encode("utf-8"))

    def test_marker_contains_orig_bytes(self):
        """Middle marker must mention the original byte count."""
        raw = "p" * 40_000
        result, event = cap_tool_result(raw, "web_search")
        assert str(event.orig_bytes) in result

    def test_marker_contains_dropped_bytes(self):
        """Middle marker must mention how many bytes were dropped."""
        raw = "q" * 40_000
        result, event = cap_tool_result(raw, "web_search")
        assert "truncated" in result

    def test_head_preserved(self):
        """First bytes of original appear at the start of result."""
        head_marker = "HEAD_CONTENT_UNIQUE_PREFIX"
        raw = head_marker + ("x" * 50_000)
        result, event = cap_tool_result(raw, "web_search")
        assert result.startswith(head_marker)

    def test_tail_preserved(self):
        """Last bytes of original appear at the end of result."""
        tail_marker = "TAIL_CONTENT_UNIQUE_SUFFIX"
        raw = ("x" * 50_000) + tail_marker
        result, event = cap_tool_result(raw, "web_search")
        assert result.endswith(tail_marker)


# ---------------------------------------------------------------------------
# 3. Byte-exact sizes
# ---------------------------------------------------------------------------

class TestByteExactSizes:
    def test_head_and_tail_roughly_equal_on_500k(self):
        """On a 500 KB input, head ≈ tail ≈ (cap − marker_bytes) / 2."""
        raw = "a" * 500_000
        result, event = cap_tool_result(raw, "web_search")

        # Find marker boundaries
        marker_start = result.find("\n[... truncated")
        # Everything before the marker is the head; find end of marker
        marker_end_text = "...]\n"
        marker_end_pos = result.find(marker_end_text) + len(marker_end_text)

        head_part = result[:marker_start]
        tail_part = result[marker_end_pos:]

        head_bytes = len(head_part.encode("utf-8"))
        tail_bytes = len(tail_part.encode("utf-8"))

        # Allow ±100 bytes tolerance for marker size variance
        assert abs(head_bytes - tail_bytes) <= 100, (
            f"head ({head_bytes}) and tail ({tail_bytes}) should be roughly equal"
        )

    def test_total_is_at_most_cap(self):
        for size in [25_001, 30_000, 100_000, 500_000]:
            raw = "b" * size
            result, event = cap_tool_result(raw, "web_search")
            encoded_len = len(result.encode("utf-8"))
            assert encoded_len <= PER_TOOL_RESULT_CAP_BYTES, (
                f"input size {size}: output {encoded_len} > cap {PER_TOOL_RESULT_CAP_BYTES}"
            )


# ---------------------------------------------------------------------------
# 4. UTF-8 boundary safety
# ---------------------------------------------------------------------------

class TestUtf8BoundarySafety:
    def test_multibyte_chars_no_exception(self):
        """Payload with Japanese chars near the cut point must not raise."""
        # "日" is 3 bytes in UTF-8. Repeat enough to fill past the cap.
        raw = "日" * 15_000   # 45_000 bytes — over the 25_000 cap
        result, event = cap_tool_result(raw, "web_search")
        assert event is not None
        # Must not crash and must still respect cap
        assert len(result.encode("utf-8")) <= PER_TOOL_RESULT_CAP_BYTES

    def test_multibyte_at_exact_cut_no_crash(self):
        """Cut point may land mid-codepoint; errors='replace' must handle it."""
        # Create a string that places "日" right at the half-budget boundary.
        half = PER_TOOL_RESULT_CAP_BYTES // 2
        # Build raw so that a 3-byte sequence straddles the half boundary.
        # half // 3 * "日" = exactly fills 3*(half//3) bytes, then add one more
        # "日" that straddles the boundary.
        raw = "日" * (half // 3 + 1) + "x" * 30_000
        result, event = cap_tool_result(raw, "web_search")
        # No exception and hard cap holds
        assert len(result.encode("utf-8")) <= PER_TOOL_RESULT_CAP_BYTES

    def test_emoji_chars_near_cut(self):
        """4-byte emoji (e.g., 🔥) near the cut must not crash."""
        raw = "🔥" * 10_000  # 40_000 bytes
        result, event = cap_tool_result(raw, "web_search")
        assert event is not None
        assert len(result.encode("utf-8")) <= PER_TOOL_RESULT_CAP_BYTES


# ---------------------------------------------------------------------------
# 5. Pathological / edge cases
# ---------------------------------------------------------------------------

class TestPathologicalCases:
    def test_just_over_cap(self):
        """One byte over cap triggers truncation."""
        raw = "a" * (PER_TOOL_RESULT_CAP_BYTES + 1)
        result, event = cap_tool_result(raw, "web_search")
        assert event is not None
        assert len(result.encode("utf-8")) <= PER_TOOL_RESULT_CAP_BYTES

    def test_tool_name_in_cap_event(self):
        raw = "x" * 30_000
        _, event = cap_tool_result(raw, "my_custom_tool")
        assert event.tool == "my_custom_tool"

    def test_idempotent_under_cap(self):
        """Calling cap on an already-capped (under-cap) string returns it unchanged."""
        raw = "a" * 1000
        result1, _ = cap_tool_result(raw, "web_search")
        result2, event2 = cap_tool_result(result1, "web_search")
        assert result1 == result2
        assert event2 is None

    def test_different_tool_names_same_result_size(self):
        """tool_name doesn't affect the capping algorithm."""
        raw = "z" * 40_000
        result_a, event_a = cap_tool_result(raw, "tool_a")
        result_b, event_b = cap_tool_result(raw, "tool_b")
        assert len(result_a) == len(result_b)
        assert event_a.orig_bytes == event_b.orig_bytes
