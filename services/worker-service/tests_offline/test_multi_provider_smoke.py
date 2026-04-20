"""Scenario 5 — one cheap completion per provider.

Smoke check that credentials and the ``langchain-*`` adapter for each
provider still work. A scenario that targets a specific provider skips when
the matrix cell targets a different one; the workflow's matrix-over-provider
strategy makes this a 1-of-3 pattern per run.

Unlike the other scenarios, this one does NOT depend on the Track 7
follow-up compaction pipeline — it exercises a single short completion. The
intent is to catch provider / SDK breakage early (e.g. upstream deprecated
a model slug) independently of our own pipeline regressions.
"""

from __future__ import annotations

import os

import pytest

# These imports are always-available in the worker venv (they're listed as
# direct dependencies in ``pyproject.toml``). If any of them is missing
# something is very wrong with the venv, and the collection error is the
# correct signal.


@pytest.mark.offline
def test_anthropic_smoke_completion(
    offline_provider: str,
    record_spend,
) -> None:
    if offline_provider != "anthropic":
        pytest.skip(f"matrix cell targets {offline_provider}, not anthropic")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    pytest.importorskip("langchain_anthropic")
    pytest.skip(
        "Scenario body stub — one short ``invoke()`` against Claude Haiku "
        "with strict max_tokens, then ``record_spend`` with the observed "
        "micro-dollar cost. Wired up once the API-key secret flows through "
        "to the workflow env."
    )


@pytest.mark.offline
def test_openai_smoke_completion(
    offline_provider: str,
    record_spend,
) -> None:
    if offline_provider != "openai":
        pytest.skip(f"matrix cell targets {offline_provider}, not openai")
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")
    pytest.importorskip("langchain_openai")
    pytest.skip(
        "Scenario body stub — one short ``invoke()`` against gpt-4o-mini, "
        "then ``record_spend``. Wired up once the OpenAI secret flows "
        "through to the workflow env."
    )


@pytest.mark.offline
def test_bedrock_smoke_completion(
    offline_provider: str,
    record_spend,
) -> None:
    if offline_provider != "bedrock":
        pytest.skip(f"matrix cell targets {offline_provider}, not bedrock")
    # The model-discovery service uses AWS_BEARER_TOKEN_BEDROCK; reuse it.
    if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        pytest.skip("AWS_BEARER_TOKEN_BEDROCK not set")
    pytest.importorskip("langchain_aws")
    pytest.skip(
        "Scenario body stub — one short ``invoke()`` against a Bedrock "
        "Haiku-class model, then ``record_spend``. Wired up once the "
        "Bedrock secret flows through to the workflow env."
    )
