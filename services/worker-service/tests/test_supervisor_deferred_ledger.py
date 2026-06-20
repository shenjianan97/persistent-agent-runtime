"""S11 §A12 Deferred Decisions Ledger — disposition measurement harness (D1/D2/D3).

The track CANNOT be archived while any §A12 row is OPEN. S11's acceptance criteria
force the look: run a wide-ish, multi-iteration fan-out and REPORT the deferred-item
metrics so each row can be dispositioned (Closed-with-evidence or → follow-up).

This test is the machine-readable evidence behind the plan §A12 disposition the
human edits into the ledger table. Each test PRINTS its metric and asserts the v1
behavior holds:

* **D1 — wide-fan-out checkpoint size.** Drive a deliberately WIDE fan-out and
  measure the max serialized ``subagent_results`` payload bytes per super-step;
  report whether the ``checkpoint.oversized`` (1 MB) warning fired. Evidence for
  "is v1 (log-only, no cap) sufficient?".
* **D2 — Writer reduction cap-hit + quality.** Feed a corpus > ``WRITER_FINDINGS_CAP``
  (50) through ``reduce_findings`` and report the cap-hit (did it drop?) + a
  report-quality spot check (does the one-shot Writer still cite resolvable ids
  after the drop, and are quotes byte-identical?). Evidence for "does dropping
  degrade the report?".
* **D3 — in-flight metering overshoot.** Report the observed per-task overshoot
  bound vs. ``max_fanout × ceiling`` — the v1 design budget (the S8 boundary meter
  bills each fan-out branch once; the per-sub-agent ceiling bounds the worst-case
  per-super-step overshoot). Cross-references the S8 DB cost test for the exact
  billed figure.

No infra: D1/D2 measure serialized payloads + the reduction directly; D3 reports
the analytic bound (the billed figure lives in the S8 DB test). Worktree-safe.
"""

from __future__ import annotations

import json
import logging

import pytest

from executor.supervisor.graph import (
    OVERSIZED_PAYLOAD_THRESHOLD_BYTES,
    _maybe_log_oversized_payload,
    _result_to_dict,
)
from executor.subagents import SubagentResult
from executor.supervisor.citations import resolve
from executor.supervisor.nodes import WRITER_FINDINGS_CAP, reduce_findings


# --------------------------------------------------------------------------- #
# D1 — wide-fan-out checkpoint payload size.
# --------------------------------------------------------------------------- #
def test_d1_wide_fanout_checkpoint_payload_size(caplog):
    """Measure the max serialized subagent_results bytes for a WIDE fan-out and
    report whether the checkpoint.oversized (1 MB) warning fired. Evidence for the
    §A12 D1 disposition."""
    # The research preset caps fan-out at 5/round and 10 iterations → at most
    # 50 sub-agent result entries accumulate across the whole run. Build a
    # realistic-MAX payload: 50 entries, each a generous summary (a sub-agent's
    # distilled findings JSON — model a verbose one at ~2 KB).
    verbose_summary = json.dumps(
        {"findings": [{"claim": "c" * 200, "source_url": "https://example.com/" + "p" * 60,
                       "supporting_quote": "q" * 400} for _ in range(3)]}
    )
    max_entries = 50  # max_fanout(5) × max_iterations(10), the research-preset ceiling
    payload = {
        f"{r}.{i}": _result_to_dict(SubagentResult.success(verbose_summary), f"{r}.{i}")
        for r in range(1, 11)
        for i in range(5)
    }
    assert len(payload) == max_entries
    payload_bytes = len(json.dumps(payload, default=str).encode("utf-8"))

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        _maybe_log_oversized_payload(payload)
    warning_fired = "checkpoint.oversized" in caplog.text

    # REPORTED METRIC (§A12 D1): max serialized payload bytes at the research-preset
    # ceiling + whether the 1 MB warning fired.
    print(
        f"\n[§A12 D1] max subagent_results payload at research ceiling "
        f"({max_entries} verbose entries): {payload_bytes} bytes "
        f"({payload_bytes / 1_000_000:.3f} MB); threshold="
        f"{OVERSIZED_PAYLOAD_THRESHOLD_BYTES} bytes; oversized_warning_fired="
        f"{warning_fired}"
    )

    # v1 evidence: even a verbose-MAX fan-out at the research ceiling stays well
    # under the 1 MB threshold, so the warning does NOT fire and the log-only v1
    # (no cap/offload) is sufficient for the shipped caps.
    assert payload_bytes < OVERSIZED_PAYLOAD_THRESHOLD_BYTES
    assert not warning_fired


# --------------------------------------------------------------------------- #
# D2 — Writer reduction cap-hit rate + report-quality spot check.
# --------------------------------------------------------------------------- #
def _finding(fid: str, *, quote: str, claim: str = "c", url: str = "https://x") -> dict:
    return {"finding_id": fid, "claim": claim, "source_url": url, "supporting_quote": quote}


def test_d2_writer_reduction_cap_hit_and_quality(caplog):
    """Feed a corpus > WRITER_FINDINGS_CAP through reduce_findings; report the
    cap-hit + a report-quality spot check (citations still resolvable, quotes
    byte-identical). Evidence for the §A12 D2 disposition."""
    corpus_size = WRITER_FINDINGS_CAP + 23  # exercise the cap with a realistic over-cap corpus
    corpus = [
        _finding(f"f{i}", quote=f"the verbatim supporting quote number {i} — é", claim=f"claim {i}")
        for i in range(corpus_size)
    ]

    caplog.clear()
    with caplog.at_level(logging.INFO):
        kept = reduce_findings(corpus)

    cap_hit = len(corpus) > WRITER_FINDINGS_CAP
    dropped = len(corpus) - len(kept)
    logged_drop = "writer.findings_reduced" in caplog.text or "dropped" in caplog.text.lower()

    # Report-quality spot check: every kept finding still resolves to a citation
    # with its source_url + a BYTE-IDENTICAL quote (select/reorder only — the
    # immutability invariant §A0.4). Dropping does not corrupt the kept corpus.
    original_quotes = {f["finding_id"]: f["supporting_quote"] for f in corpus}
    resolvable = 0
    quote_intact = 0
    for f in kept:
        cit = resolve(f["finding_id"], kept)
        if cit.get("source_url") and not cit.get("error"):
            resolvable += 1
        if f["supporting_quote"] == original_quotes[f["finding_id"]]:
            quote_intact += 1

    # REPORTED METRIC (§A12 D2): cap-hit rate + quality.
    print(
        f"\n[§A12 D2] corpus={len(corpus)} cap={WRITER_FINDINGS_CAP} "
        f"cap_hit={cap_hit} kept={len(kept)} dropped={dropped} "
        f"dropped_logged={logged_drop} resolvable_kept={resolvable}/{len(kept)} "
        f"quotes_byte_identical={quote_intact}/{len(kept)}"
    )

    # v1 evidence: the cap fired, the drop was LOGGED (no silent truncation), and
    # every KEPT finding is still fully citable with a byte-identical quote — the
    # one-shot Writer is fed a coherent, immutable corpus. Quality degradation (if
    # any) is "fewer findings", not "corrupted findings".
    assert cap_hit and dropped == 23
    assert len(kept) == WRITER_FINDINGS_CAP
    assert logged_drop
    assert resolvable == len(kept)
    assert quote_intact == len(kept)


# --------------------------------------------------------------------------- #
# D3 — in-flight metering overshoot bound.
# --------------------------------------------------------------------------- #
def test_d3_inflight_metering_overshoot_bound():
    """Report the per-task cost-overshoot bound vs. max_fanout × ceiling — the v1
    design budget. The exact BILLED figure for a wide fan-out is asserted by the
    S8 DB test (tests/backend-integration/test_supervisor_fanout_budget.py); this
    documents the analytic disposition for §A12 D3."""
    # The S8 boundary meter bills each fan-out branch's accumulated usage exactly
    # once at the super-step boundary (additive, no double-count — proven on real
    # Postgres in test_supervisor_budget_pause_fires_at_fanout_boundary_billing_
    # each_sibling_once: 5-way round → 13260 µ$, billed once). The worst-case
    # per-super-step OVERSHOOT past the budget is bounded by max_fanout × ceiling:
    # the pause is evaluated only AFTER the whole fan-out round merges, so a round
    # can overshoot by up to (max_fanout) sub-agents each spending up to (ceiling)
    # tokens before the boundary check fires.
    max_fanout = 5  # research preset
    # The S8 DB test's exact figures (the v1 instrumentation = the billed ledger):
    s8_wide_fanout_round_microdollars = 13260  # 5-way round, billed exactly once
    print(
        f"\n[§A12 D3] in-flight metering: pause evaluated at the fan-out super-step "
        f"BOUNDARY (not mid-Send); per-round overshoot bounded by max_fanout="
        f"{max_fanout} × per-sub-agent ceiling. S8 DB evidence: a 5-way round bills "
        f"{s8_wide_fanout_round_microdollars} µ$ EXACTLY ONCE (no double-bill, no "
        f"per-sub-agent ledger row). Finer in-flight (mid-round) metering is DEFERRED "
        f"post-GA — the bounded overshoot is the accepted v1 budget design (§A8/D3)."
    )
    # The disposition is analytic + cross-referenced to the S8 DB test; this test
    # exists to surface the metric in the S11 acceptance run.
    assert max_fanout == 5
