"""Writer-prompt citation contract (hardened).

Regression for the live 2026-06-14 deep-research run (task 925b89e3): the report
had a complete ``## Sources`` list but the body prose carried almost no inline
``[finding_id]`` markers — the Writer batched its citations into a self-authored
"Primary Sources and Secondary Reporting" section instead of citing inline. The
deterministic renderer (executor/supervisor/citations.py) only numbers and lists
the markers the Writer actually places, so a citation-sparse body yields a
sources list disconnected from the narrative.

The fix hardens ``WRITER_PROMPT``: inline per-claim citation is MANDATORY, and the
Writer must NOT author its own sources/references/bibliography section (the
runtime appends ``## Sources`` automatically). These are pure string assertions on
the prompt builder — no model, no infra.
"""

from __future__ import annotations

from executor.supervisor.prompts import build_writer_prompt


_FINDINGS = [
    {
        "finding_id": "1.0-abcd1234",
        "claim": "claim one",
        "supporting_quote": "quote one",
        "source_url": "https://example.com/a",
    }
]


def test_writer_prompt_requires_inline_citation_per_claim() -> None:
    prompt = build_writer_prompt("the brief", _FINDINGS)
    # Mandatory (MUST), not aspirational (should) — the soft "should" let the
    # model treat inline citation as optional.
    assert "MUST carry at least one inline finding id" in prompt
    assert "should be backed by at least one finding id" not in prompt
    # Citations belong in the body, not gathered at the end.
    assert "Put the citations IN THE BODY" in prompt
    assert "Do NOT gather, list, or repeat them at the end." in prompt


def test_writer_prompt_forbids_self_authored_sources_section() -> None:
    prompt = build_writer_prompt("the brief", _FINDINGS)
    # The runtime appends ``## Sources``; a self-authored references section
    # duplicates it and is what produced the disconnected sources list.
    for forbidden in ("Sources", "References", "Bibliography", "Primary"):
        assert forbidden in prompt, f"forbidden-section list must name {forbidden!r}"
    assert "appends the numbered source list automatically" in prompt


def test_writer_prompt_keeps_finding_id_only_binding() -> None:
    prompt = build_writer_prompt("the brief", _FINDINGS)
    # Binding stays by id only (never an inline URL) — the anti-fabrication
    # invariant the renderer relies on is unchanged.
    assert "Cite by `finding_id` ONLY" in prompt
    assert "do NOT write source URLs" in prompt
    # The example id is the first finding's id (existing substitution contract).
    assert "[1.0-abcd1234]" in prompt
    # Role opener the fake-model routers key on must survive.
    assert "You are the writer" in prompt
