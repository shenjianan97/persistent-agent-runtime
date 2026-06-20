"""Deterministic citation binding for the Supervisor topology (Task S7).

Citations are bound **deterministically**, not left to the Writer's discretion
(design "Citation binding" — a deliberate divergence from Anthropic's
CitationAgent, which still lets the model *choose* a source and so leaves
misattribution possible). Three steps:

1. Sub-agents emit structured ``{finding_id, claim, source_url, supporting_quote}``
   findings (parsed in ``nodes.py::parse_findings``) — not freeform prose with
   inline links.
2. The **Writer cites by ``finding_id`` only**; it never emits an inline source
   URL and so cannot invent a source.
3. This module resolves those ids → citations and verifies the cited quotes:

   * :func:`resolve` — a **deterministic** ``finding_id`` → citation lookup with
     **no LLM**. An unknown id is surfaced as a structured **render error flag**
     (:data:`RENDER_ERROR_FLAG`), NEVER a fabricated source (§A5).
   * :func:`verify` — **one verify LLM call per distinct cited finding, run
     concurrently** (bounded by a small semaphore): for each cited finding it
     asks the verify model whether the referenced finding's *immutable*
     ``supporting_quote`` supports the sentence, and **flags** the ones that
     don't. It NEVER rewrites the report and NEVER invents a source (an unknown
     id is flagged, not verified), and a single call failing degrades fail-safe
     for that one citation rather than sinking the report. This catches the
     subtler "real source, wrong claim" that binding by id alone cannot.

The load-bearing invariant (§A0 inv. 4): findings — and especially their
``supporting_quote`` — are immutable and addressable by ``finding_id``. Both
:func:`resolve` and :func:`verify` read the quote verbatim; if any node had
rewritten it, both would break. Nothing here mutates a finding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from executor.text import flatten_text as _flatten_text

logger = logging.getLogger(__name__)

# Sentinel surfaced on an unresolvable citation. The Console / render path shows
# this as a broken-citation marker; it is NEVER a fabricated source (§A5).
RENDER_ERROR_FLAG = "unresolved_finding_id"

# Bound on concurrent verify LLM calls. ``verify`` issues one call per distinct
# cited finding (≤ WRITER_FINDINGS_CAP, deduped by ``extract_citations``) and
# runs them concurrently; the semaphore caps in-flight calls so a 30-40-citation
# report doesn't open 40 simultaneous provider connections (matching the repo's
# bounded async fan-out convention). Tunable; not yet a config knob.
_VERIFY_MAX_CONCURRENCY = 8

# A citation reference in the Writer's prose: ``[<finding_id>]``. The Writer is
# instructed to cite by ``finding_id`` ONLY in exactly this form, so the runtime
# can extract references deterministically at render / verify time. ``finding_id``
# values are minted by ``parse_findings`` from a hex digest, so the character
# class is intentionally narrow (word chars, dot, hyphen) — broad enough for the
# minted ids without swallowing surrounding prose.
_CITATION_RE = re.compile(r"\[([\w.\-:]+)\]")


def _index(findings: list[dict]) -> dict[str, dict]:
    """Build a ``finding_id`` → finding index (deterministic, no LLM).

    Last-write-wins on a duplicate id, but ids are minted unique within the run
    (``parse_findings``), so a collision indicates a contract violation upstream
    rather than an expected merge.
    """
    return {
        str(f.get("finding_id")): f
        for f in findings
        if isinstance(f, dict) and f.get("finding_id")
    }


def resolve(finding_id: str, findings: list[dict]) -> dict:
    """Resolve ``finding_id`` → a citation. Deterministic; **no LLM**.

    A hit returns ``{finding_id, source_url, supporting_quote, claim}`` (the quote
    read verbatim — never mutated; ``claim`` carried for the render surface, S9).
    A miss returns a structured **render error flag**
    ``{finding_id, error: RENDER_ERROR_FLAG}`` with no ``source_url`` /
    ``supporting_quote`` — the render path surfaces it as a broken citation,
    NEVER a fabricated source (§A5).
    """
    finding = _index(findings).get(str(finding_id))
    if finding is None:
        return {"finding_id": str(finding_id), "error": RENDER_ERROR_FLAG}
    return {
        "finding_id": str(finding_id),
        "source_url": finding.get("source_url", ""),
        "supporting_quote": finding.get("supporting_quote", ""),
        "claim": finding.get("claim", ""),
    }


def extract_citations(report: str) -> list[str]:
    """Extract the ordered list of ``finding_id`` references from the report.

    Deterministic regex over the ``[<finding_id>]`` form the Writer is instructed
    to use. Returns ids in first-appearance order, de-duplicated (one verify pass
    per distinct cited id)."""
    seen: list[str] = []
    for match in _CITATION_RE.finditer(report or ""):
        fid = match.group(1)
        if fid not in seen:
            seen.append(fid)
    return seen


def _sentence_for_citation(report: str, finding_id: str) -> str:
    """Return the sentence containing ``[finding_id]`` (best-effort).

    Splits on sentence terminators and returns the first sentence that carries
    the ``[finding_id]`` reference, so the verify model judges the quote against
    the actual claim it backs (not the whole report). Falls back to the whole
    report if no sentence boundary is found."""
    marker = f"[{finding_id}]"
    # Split keeping it simple — sentence terminators followed by whitespace.
    sentences = re.split(r"(?<=[.!?])\s+", report or "")
    for sentence in sentences:
        if marker in sentence:
            return sentence.strip()
    return (report or "").strip()


def _parse_supported(content: Any) -> bool:
    """Parse the verify model's ``{"supported": bool}`` reply.

    Fail **safe to supported** on an unparseable reply: the verify pass only
    *flags* (it never gates publication), and a verify node that cannot read its
    own output must not raise a false "unsupported" alarm on every citation."""
    text = _flatten_text(content).strip()
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                obj = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return True
        else:
            return True
    if not isinstance(obj, dict):
        return True
    return bool(obj.get("supported", True))


# Verify prompt: a thin, single-purpose check — does the quote support the
# sentence? Returns a single JSON object. It NEVER rewrites prose or names a
# source — it answers one yes/no per cited sentence.
_VERIFY_PROMPT = """\
You are a citation verifier. Decide whether the SOURCE QUOTE supports the \
CLAIM SENTENCE — i.e. whether a careful reader would accept the sentence as \
backed by that quote. Do not rewrite anything; do not suggest a different \
source. Answer only whether the quote supports the sentence.

CLAIM SENTENCE:
{sentence}

SOURCE QUOTE:
{quote}

Respond with a single JSON object and nothing else:
  {{"supported": true}}   — the quote supports the sentence
  {{"supported": false}}  — the quote does NOT support the sentence"""


async def verify(report: str, findings: list[dict], *, model: Any) -> list[dict]:
    """Verify each cited sentence against its finding's quote.

    Issues **one verify LLM call per distinct cited finding, run concurrently**
    (bounded by :data:`_VERIFY_MAX_CONCURRENCY`). For each distinct
    ``[finding_id]`` reference in ``report``:

    * an **unknown** id → ``{finding_id, error: RENDER_ERROR_FLAG}`` (flagged, not
      verified — the verify pass never invents a source for a dangling id, §A5);
    * a **known** id → ``{finding_id, supported: bool}`` from the verify model
      judging whether the finding's *immutable* ``supporting_quote`` supports the
      sentence that cites it.

    Returns a list of per-citation flags in first-appearance order. It **never**
    rewrites the report and **never** fabricates a source — it only flags. A
    single verify call failing (timeout / rate-limit / transport) degrades
    **fail-safe** for that one citation (``supported: True`` + a soft
    ``verify_error`` note, matching ``_parse_supported``'s fail-safe — verify only
    flags, never gates) rather than sinking the already-generated report. The
    model is injected (``config["configurable"]["verify_model"]`` upstream) so
    tests pass a fake.
    """
    index = _index(findings)
    cited_ids = extract_citations(report)
    semaphore = asyncio.Semaphore(_VERIFY_MAX_CONCURRENCY)

    async def _verify_one(finding_id: str) -> dict:
        finding = index.get(finding_id)
        if finding is None:
            # Unknown id — no LLM call; flagged as a render error (§A5).
            return {"finding_id": finding_id, "error": RENDER_ERROR_FLAG}
        sentence = _sentence_for_citation(report, finding_id)
        quote = finding.get("supporting_quote", "")
        async with semaphore:
            try:
                reply = await model.ainvoke(
                    _VERIFY_PROMPT.format(sentence=sentence, quote=quote)
                )
            except Exception:  # noqa: BLE001 — one flaky verify call must NOT
                # sink a successfully-generated report. Degrade fail-safe for
                # this one citation (assume supported, soft-flag the failure).
                logger.warning(
                    "citations.verify_call_failed finding_id=%s (non-fatal, "
                    "flagging supported fail-safe)",
                    finding_id,
                    exc_info=True,
                )
                return {
                    "finding_id": finding_id,
                    "supported": True,
                    "verify_error": True,
                }
        return {"finding_id": finding_id, "supported": _parse_supported(reply.content)}

    # gather preserves the order of ``cited_ids`` (first-appearance) in the result
    # list, so the returned flags are deterministically ordered despite running
    # concurrently. Each task is individually fail-safe, so gather never raises.
    return list(await asyncio.gather(*(_verify_one(fid) for fid in cited_ids)))


# Broken-citation marker rendered in the prose for an unresolvable id (no
# source_url). The Sources section pairs it with an explicit "source unavailable"
# line — we NEVER fabricate a number or a URL for a dangling id (§A5).
_BROKEN_MARKER = "?"
# Warning glyph appended to a Sources line whose verify flag says the quote did
# not clearly support the claim (``supported is False`` or an ``error``). It only
# *flags*; the report is never gated on it (matching ``verify``'s contract).
_UNSUPPORTED_MARKER = "⚠"


def _is_unresolvable(entry: dict | None) -> bool:
    """An id is unresolvable when its resolved/flag entry carries the render
    error sentinel or otherwise has no usable ``source_url`` (§A5 — never invent
    a source for such an id)."""
    if not isinstance(entry, dict):
        return True
    if entry.get("error"):
        return True
    return not entry.get("source_url")


def render_report(
    report: str,
    citations: list[dict],
    verify_flags: list[dict],
) -> str:
    """Render the raw Writer report into a numbered-citation report. Deterministic; **no LLM**.

    The Writer cites by raw ``[finding_id]`` (see :data:`_CITATION_RE`); this step
    is the final, deterministic binding that turns those internal hashes into
    reader-facing numbered sources, so the published report (and the Console
    activity turn that mirrors the ``report`` channel verbatim) never leaks an
    internal ``finding_id``.

    Steps:

    * Number each **distinct cited** ``finding_id`` by first appearance
      (``extract_citations`` order) → 1, 2, 3 …
    * Substitute each raw ``[finding_id]`` marker in the prose with ``[N]`` (or
      ``[?]`` for an unresolvable id), via :data:`_CITATION_RE` — pure marker
      substitution, the surrounding prose is **never reflowed** (style-agnostic:
      works for formal prose and annotated bullets alike).
    * Append a ``## Sources`` section: one line per ``[N]`` → its resolved
      ``source_url`` (optionally prefixed with a short ``claim`` label). A source
      whose matching ``verify_flags`` entry is unsupported / errored gets a
      :data:`_UNSUPPORTED_MARKER`. An **unresolvable** id renders ``[?] (source
      unavailable)`` — NEVER a fabricated source (§A5).

    Inputs are never mutated and ``supporting_quote`` is never emitted into the
    rendered report.
    """
    report = report or ""
    citations_by_id = {
        str(c.get("finding_id")): c for c in (citations or []) if isinstance(c, dict)
    }
    flags_by_id = {
        str(f.get("finding_id")): f
        for f in (verify_flags or [])
        if isinstance(f, dict)
    }

    cited_ids = extract_citations(report)

    # Assign a 1-based number to each distinct *resolvable* cited id, in
    # first-appearance order; unresolvable ids render as a shared broken marker
    # and never consume a source number.
    number_by_id: dict[str, int] = {}
    next_number = 1
    for fid in cited_ids:
        if _is_unresolvable(citations_by_id.get(fid)):
            continue
        number_by_id[fid] = next_number
        next_number += 1

    def _marker(fid: str) -> str:
        n = number_by_id.get(fid)
        return f"[{n}]" if n is not None else f"[{_BROKEN_MARKER}]"

    # Substitute raw [finding_id] -> [N] / [?] in place (no reflow). cited_ids is
    # derived from the same regex over the same report, so every matched id is
    # accounted for; a stray bracket not in the cited set falls through to [?].
    def _sub(match: "re.Match[str]") -> str:
        return _marker(match.group(1))

    rendered_body = _CITATION_RE.sub(_sub, report)

    # Build the Sources section in source-number order, then a single broken line
    # if any unresolvable id was cited.
    source_lines: list[str] = []
    ordered = sorted(number_by_id.items(), key=lambda kv: kv[1])
    for fid, n in ordered:
        cit = citations_by_id.get(fid, {})
        url = cit.get("source_url", "")
        claim = (cit.get("claim") or "").strip()
        line = f"[{n}] {claim} — {url}" if claim else f"[{n}] {url}"
        flag = flags_by_id.get(fid)
        if flag is not None and (flag.get("supported") is False or flag.get("error")):
            line = f"{line} {_UNSUPPORTED_MARKER}"
        source_lines.append(line)

    has_unresolvable = any(
        _is_unresolvable(citations_by_id.get(fid)) for fid in cited_ids
    )
    if has_unresolvable:
        source_lines.append(f"[{_BROKEN_MARKER}] (source unavailable)")

    if not source_lines:
        return rendered_body

    body = rendered_body.rstrip()
    return f"{body}\n\n## Sources\n" + "\n".join(source_lines)


__all__ = [
    "RENDER_ERROR_FLAG",
    "resolve",
    "verify",
    "extract_citations",
    "render_report",
]
