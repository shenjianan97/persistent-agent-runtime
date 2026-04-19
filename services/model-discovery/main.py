#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from urllib.error import HTTPError
from typing import Any, Mapping

import psycopg

# Prices in microdollars per million tokens (e.g. 3,000,000 = $3.00)
PRICING_DEFAULTS = {
    # Anthropic
    "claude-3-7-sonnet-20250219": {"input": 3_000_000, "output": 15_000_000},
    "claude-3-5-sonnet-20241022": {"input": 3_000_000, "output": 15_000_000},
    "claude-3-5-haiku-20241022": {"input": 1_000_000, "output": 5_000_000},
    "claude-3-opus-20240229": {"input": 15_000_000, "output": 75_000_000},
    # OpenAI
    "gpt-4o": {"input": 2_500_000, "output": 10_000_000},
    "gpt-4o-mini": {"input": 150_000, "output": 600_000},
    "o1": {"input": 15_000_000, "output": 60_000_000},
    "o1-mini": {"input": 3_000_000, "output": 12_000_000},
    "o3-mini": {"input": 1_100_000, "output": 4_400_000},
    # Bedrock (Anthropic models via Bedrock use the same pricing)
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 3_000_000, "output": 15_000_000},
    "anthropic.claude-3-5-haiku-20241022-v1:0": {"input": 1_000_000, "output": 5_000_000},
    "anthropic.claude-3-opus-20240229-v1:0": {"input": 15_000_000, "output": 75_000_000},
    "anthropic.claude-3-7-sonnet-20250219-v1:0": {"input": 3_000_000, "output": 15_000_000},
    "anthropic.claude-sonnet-4-20250514-v1:0": {"input": 3_000_000, "output": 15_000_000},
    "anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3_000_000, "output": 15_000_000},
    "anthropic.claude-sonnet-4-6": {"input": 3_000_000, "output": 15_000_000},
    "anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 1_000_000, "output": 5_000_000},
    "anthropic.claude-opus-4-20250514-v1:0": {"input": 15_000_000, "output": 75_000_000},
    "anthropic.claude-opus-4-1-20250805-v1:0": {"input": 15_000_000, "output": 75_000_000},
    "anthropic.claude-opus-4-5-20251101-v1:0": {"input": 15_000_000, "output": 75_000_000},
    "anthropic.claude-opus-4-6-v1": {"input": 15_000_000, "output": 75_000_000},
    "amazon.nova-pro-v1:0": {"input": 800_000, "output": 3_200_000},
    "amazon.nova-lite-v1:0": {"input": 60_000, "output": 240_000},
    "amazon.nova-micro-v1:0": {"input": 35_000, "output": 140_000},
}

GLOBAL_FALLBACK_PRICING = {"input": 15_000_000, "output": 75_000_000}

# Conservative provider-level fallback pricing for newly discovered models that do
# not yet have an explicit entry in PRICING_DEFAULTS. This avoids silently
# under-reporting spend as zero while still allowing the model to be used.
PRICING_FALLBACKS = {
    "anthropic": {"input": 15_000_000, "output": 75_000_000},
    "openai": {"input": 15_000_000, "output": 60_000_000},
    "bedrock": {"input": 15_000_000, "output": 75_000_000},
}

# Context window (input tokens) per model. Values verified against each
# provider's official documentation as of 2026-04. The worker's compaction
# pipeline (services/worker-service/executor/graph.py:_get_model_context_window)
# reads this value out of the ``models`` table — a NULL or missing row
# forces the conservative per-service default, which fires Tier 3
# summarization aggressively on models that actually support much larger
# windows. Keep this list in sync with supported model families; an unknown
# model falls through to ``CONTEXT_WINDOW_FALLBACKS`` by provider.
CONTEXT_WINDOW_DEFAULTS = {
    # Anthropic direct API — Claude 4.6 / 4.7 have 1M, 4.5 family has 200K.
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-opus-4-5-20251101": 200_000,
    "claude-sonnet-4-5-20250929": 200_000,
    "claude-opus-4-1-20250805": 200_000,
    "claude-opus-4-20250514": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-3-7-sonnet-20250219": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-5-haiku-20241022": 200_000,
    "claude-3-opus-20240229": 200_000,
    # Anthropic via Bedrock (same models, different IDs).
    "anthropic.claude-opus-4-7": 1_000_000,
    "anthropic.claude-opus-4-6-v1": 1_000_000,
    "anthropic.claude-sonnet-4-6": 1_000_000,
    "anthropic.claude-haiku-4-5-20251001-v1:0": 200_000,
    "anthropic.claude-opus-4-5-20251101-v1:0": 200_000,
    "anthropic.claude-sonnet-4-5-20250929-v1:0": 200_000,
    "anthropic.claude-opus-4-1-20250805-v1:0": 200_000,
    "anthropic.claude-opus-4-20250514-v1:0": 200_000,
    "anthropic.claude-sonnet-4-20250514-v1:0": 200_000,
    "anthropic.claude-3-7-sonnet-20250219-v1:0": 200_000,
    "anthropic.claude-3-5-sonnet-20241022-v2:0": 200_000,
    "anthropic.claude-3-5-haiku-20241022-v1:0": 200_000,
    "anthropic.claude-3-opus-20240229-v1:0": 200_000,
    # OpenAI — gpt-4.1 family is 1M; gpt-4o is 128K; gpt-5 is 400K; o-series is 200K.
    "gpt-4.1": 1_000_000,
    "gpt-4.1-mini": 1_000_000,
    "gpt-4.1-nano": 1_000_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-5": 400_000,
    "gpt-5-mini": 400_000,
    "gpt-5-nano": 400_000,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    # Z.AI GLM on Bedrock — 200K input, 128K output.
    "zai.glm-5": 200_000,
    "zai.glm-4.7": 200_000,
    "zai.glm-4.7-flash": 200_000,
    # Amazon Nova on Bedrock — 300K for Pro/Lite, 128K for Micro.
    "amazon.nova-pro-v1:0": 300_000,
    "amazon.nova-lite-v1:0": 300_000,
    "amazon.nova-micro-v1:0": 128_000,
}

# Fallback per provider when a model isn't in CONTEXT_WINDOW_DEFAULTS. 128K is
# the de-facto floor for modern LLMs (GPT-4o, Claude 3.x, Gemini 1.5, Nova
# Micro, most Bedrock Converse models). Legacy sub-128K models are filtered
# out via DEACTIVATE_MODEL_IDS before they reach the ``models`` table, so
# a provider-level fallback at 128K is safe in practice. If a brand-new
# sub-128K model shows up that discovery doesn't know about, we'll see it
# in production via context-exceeded errors and add to the deny list then.
CONTEXT_WINDOW_FALLBACKS = {
    "anthropic": 200_000,
    "openai": 128_000,
    "bedrock": 128_000,
}

GLOBAL_FALLBACK_CONTEXT_WINDOW = 128_000

# Models the provider's API lists that we do NOT want active in this platform,
# typically because their context window is below the platform floor (128K)
# or because they are non-chat modalities (audio/image/tts) that slipped past
# the existing prefix filter. Models in this set are skipped at discovery
# time — they won't appear in the agent-config model dropdown, and
# ``upsert_models`` will leave any pre-existing row as ``is_active = false``.
DEACTIVATE_MODEL_IDS = frozenset({
    # OpenAI legacy chat models with sub-128K context windows.
    "gpt-4",                         # 8K
    "gpt-4-0613",                    # 8K
    "gpt-3.5-turbo",                 # 16K
    "gpt-3.5-turbo-0125",            # 16K
    "gpt-3.5-turbo-1106",            # 16K
    "gpt-3.5-turbo-16k",             # 16K
    "gpt-3.5-turbo-instruct",        # 4K
    "gpt-3.5-turbo-instruct-0914",   # 4K
})

LOCK_ID = 543210987
DB_CREDENTIALS_SECRET_ENV = "DB_CREDENTIALS_SECRET_ARN"
PROVIDER_SOURCES = (
    ("anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_SECRET_ARN"),
    ("openai", "OPENAI_API_KEY", "OPENAI_API_KEY_SECRET_ARN"),
    ("bedrock", "AWS_BEARER_TOKEN_BEDROCK", "AWS_BEARER_TOKEN_BEDROCK_SECRET_ARN"),
)

AWS_BEDROCK_REGION_ENV = "AWS_BEDROCK_REGION"
AWS_BEDROCK_DEFAULT_REGION = "us-east-1"

# Phase 2 Track 5 — embedding provider.
#
# The platform uses a single embedding provider (OpenAI text-embedding-3-small,
# 1536 dimensions). The API key lives in the same ``provider_keys`` table as
# the chat provider — OpenAI's embedding endpoint shares credentials with its
# chat endpoint, so the existing ``openai`` row is reused. The validation
# behaviour below runs at discovery/startup time so a broken embedding key
# surfaces here, not at the first memory-enabled task.
EMBEDDING_PROVIDER_ID = "openai"
EMBEDDING_MODEL_ID = "text-embedding-3-small"
EMBEDDING_MODEL_DISPLAY_NAME = "OpenAI text-embedding-3-small"
EMBEDDING_MODEL_INPUT_MICRODOLLARS_PER_MTOK = 20_000  # OpenAI: $0.02 / 1M tokens
EMBEDDING_PROBE_URL = "https://api.openai.com/v1/embeddings"
EMBEDDING_PROBE_INPUT = "healthcheck"

# Query used to decide how loudly to fail when the embedding key is missing
# or invalid. Any row with ``agent_config -> 'memory' ->> 'enabled' = 'true'``
# counts. The table is part of Phase 2 Track 1 (agents table, migration 0005);
# if it is absent (e.g. fresh CI schema without migrations) the count comes
# back zero and we treat the situation as "no memory-enabled agents".
MEMORY_ENABLED_AGENT_COUNT_SQL = """
    SELECT COUNT(*)
      FROM agents
     WHERE (agent_config -> 'memory' ->> 'enabled') = 'true'
"""


def resolve_model_pricing(provider_id, model_id):
    pricing = PRICING_DEFAULTS.get(model_id)
    if pricing:
        return dict(pricing)

    fallback = PRICING_FALLBACKS.get(provider_id, GLOBAL_FALLBACK_PRICING)
    print(
        f"[Discovery] No predefined pricing for {provider_id}/{model_id}; "
        f"using fallback pricing input={fallback['input']} output={fallback['output']}"
    )
    return dict(fallback)


def resolve_model_context_window(provider_id, model_id):
    """Return the context window (input tokens) for a model.

    Lookup order:
      1. Explicit entry in CONTEXT_WINDOW_DEFAULTS (verified value).
      2. Provider-level fallback from CONTEXT_WINDOW_FALLBACKS.
      3. Global fallback (GLOBAL_FALLBACK_CONTEXT_WINDOW).

    The worker's compaction pipeline reads this value from the database.
    Any model allowed to reach the ``models`` table (i.e. not in
    DEACTIVATE_MODEL_IDS) is assumed to support at least the platform
    floor of 128K — if that turns out to be wrong for a newly discovered
    model, add it to DEACTIVATE_MODEL_IDS or to CONTEXT_WINDOW_DEFAULTS
    with its real value.
    """
    explicit = CONTEXT_WINDOW_DEFAULTS.get(model_id)
    if explicit is not None:
        return explicit
    return CONTEXT_WINDOW_FALLBACKS.get(provider_id, GLOBAL_FALLBACK_CONTEXT_WINDOW)


def _load_secret_text(secret_arn: str) -> str:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - boto3 exists in Lambda runtime
        raise RuntimeError(
            "boto3 is required to resolve Secrets Manager ARNs at runtime"
        ) from exc

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    secret_text = response.get("SecretString")
    if secret_text is None:
        secret_binary = response.get("SecretBinary")
        if secret_binary is None:
            raise RuntimeError(f"Secret {secret_arn!r} did not contain a string payload")
        if isinstance(secret_binary, bytes):
            secret_text = secret_binary.decode("utf-8")
        else:
            secret_text = bytes(secret_binary).decode("utf-8")
    return secret_text.strip()


def _load_secret_json(secret_arn: str) -> Mapping[str, Any]:
    return json.loads(_load_secret_text(secret_arn))


def _coerce_port(value: Any) -> Any:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


def _resolve_db_connection_kwargs() -> dict[str, Any]:
    dsn = os.environ.get("DB_DSN")
    if dsn:
        return {"conninfo": dsn}

    credentials: Mapping[str, Any] = {}
    credentials_secret_arn = os.environ.get(DB_CREDENTIALS_SECRET_ENV)
    if credentials_secret_arn:
        credentials = _load_secret_json(credentials_secret_arn)

    host = credentials.get("host") or os.environ.get("DB_HOST", "localhost")
    port = credentials.get("port") or os.environ.get("DB_PORT", "55432")
    dbname = (
        credentials.get("dbname")
        or credentials.get("database")
        or os.environ.get("DB_NAME", "persistent_agent_runtime")
    )
    user = credentials.get("username") or credentials.get("user") or os.environ.get("DB_USER", "postgres")
    password = credentials.get("password") or os.environ.get("DB_PASSWORD", "postgres")

    return {
        "host": host,
        "port": _coerce_port(port),
        "dbname": dbname,
        "user": user,
        "password": password,
    }


def get_db_connection():
    params = _resolve_db_connection_kwargs()
    return psycopg.connect(**params)


def _load_provider_api_keys() -> dict[str, str]:
    provider_keys: dict[str, str] = {}

    for provider_id, env_var, secret_env_var in PROVIDER_SOURCES:
        api_key = os.environ.get(env_var)
        source = "environment"
        if not api_key:
            secret_arn = os.environ.get(secret_env_var)
            if secret_arn:
                api_key = _load_secret_text(secret_arn)
                source = "Secrets Manager"

        if api_key:
            provider_keys[provider_id] = api_key.strip()
            print(f"[Discovery] Loaded {provider_id} API key from {source}")

    return provider_keys


def fetch_anthropic_models(api_key):
    print("[Discovery] Querying Anthropic models...")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    models = []
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            for m in data.get("data", []):
                if m.get("type") == "model":
                    models.append(
                        {
                            "id": m["id"],
                            "display_name": m.get("display_name") or m["id"],
                        }
                    )
    except HTTPError as e:
        print(f"[Discovery] Failed to fetch Anthropic models: HTTP {e.code}")
    except Exception as e:
        print(f"[Discovery] Failed to fetch Anthropic models: {e}")
    return models


def fetch_openai_models(api_key):
    print("[Discovery] Querying OpenAI models...")
    req = urllib.request.Request(
        "https://api.openai.com/v1/models",
        headers={
            "Authorization": f"Bearer {api_key}",
        },
    )
    models = []
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            for m in data.get("data", []):
                # Filter to chat models only to avoid cluttering UI with whisper/dall-e/tts
                m_id = m["id"]
                if not (m_id.startswith("gpt-") or m_id.startswith("o1") or m_id.startswith("o3")):
                    continue
                # Skip models on the platform deny list (legacy sub-128K context,
                # non-chat modalities). Their rows stay is_active=false.
                if m_id in DEACTIVATE_MODEL_IDS:
                    continue
                models.append(
                    {
                        "id": m_id,
                        "display_name": m_id,
                    }
                )
    except HTTPError as e:
        print(f"[Discovery] Failed to fetch OpenAI models: HTTP {e.code}")
    except Exception as e:
        print(f"[Discovery] Failed to fetch OpenAI models: {e}")
    return models


def fetch_bedrock_models(api_key):
    """Fetch available foundation models from AWS Bedrock using a bearer token (API key)."""
    region = os.environ.get(AWS_BEDROCK_REGION_ENV, AWS_BEDROCK_DEFAULT_REGION)
    print(f"[Discovery] Querying Bedrock models in {region}...")
    url = f"https://bedrock.{region}.amazonaws.com/foundation-models"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
        },
    )
    models = []
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode())
            for m in data.get("modelSummaries", []):
                model_id = m.get("modelId", "")
                # Only include models that support on-demand or inference-profile invocation
                inference_types = set(m.get("inferenceTypesSupported", []))
                if not inference_types.intersection({"ON_DEMAND", "INFERENCE_PROFILE"}):
                    continue
                # Only include models that support the Converse API (used by ChatBedrockConverse)
                if not m.get("converse"):
                    continue
                # Only include text-generating models
                output_modalities = m.get("outputModalities", [])
                if "TEXT" not in output_modalities:
                    continue
                display_name = m.get("modelName", model_id)
                provider_name = m.get("providerName", "")
                if provider_name:
                    display_name = f"{provider_name} {display_name}"
                models.append({"id": model_id, "display_name": display_name})
    except HTTPError as e:
        print(f"[Discovery] Failed to fetch Bedrock models: HTTP {e.code}")
    except Exception as e:
        print(f"[Discovery] Failed to fetch Bedrock models: {e}")
    return models


def _fetch_models(provider_id: str, api_key: str) -> list[dict[str, str]]:
    if provider_id == "anthropic":
        return fetch_anthropic_models(api_key)
    if provider_id == "openai":
        return fetch_openai_models(api_key)
    if provider_id == "bedrock":
        return fetch_bedrock_models(api_key)
    print(f"[Discovery] Skipping unsupported provider: {provider_id}")
    return []


def _delete_stale_provider_data(cur, configured_provider_ids: set[str]) -> list[str]:
    cur.execute("SELECT provider_id FROM provider_keys ORDER BY provider_id")
    existing_provider_ids = {row[0] for row in cur.fetchall()}
    stale_provider_ids = sorted(existing_provider_ids.difference(configured_provider_ids))
    if not stale_provider_ids:
        return []

    cur.execute("DELETE FROM models WHERE provider_id = ANY(%s)", (stale_provider_ids,))
    deleted_models = cur.rowcount
    cur.execute("DELETE FROM provider_keys WHERE provider_id = ANY(%s)", (stale_provider_ids,))
    deleted_keys = cur.rowcount
    print(
        "[Discovery] Removed stale provider metadata for "
        f"{', '.join(stale_provider_ids)} "
        f"({deleted_keys} key rows, {deleted_models} model rows)"
    )
    return stale_provider_ids


def upsert_models(conn, provider_keys):
    configured_provider_ids = set(provider_keys)
    print(
        "[Discovery] Starting database sync (configured providers: "
        f"{', '.join(sorted(configured_provider_ids)) or 'none'})"
    )

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            stale_provider_ids = _delete_stale_provider_data(cur, configured_provider_ids)

            # First, mark all remaining models inactive. We will exclusively reactivate what we find.
            cur.execute("UPDATE models SET is_active = false")

            for provider_id, api_key in provider_keys.items():
                cur.execute(
                    """
                    INSERT INTO provider_keys (provider_id, api_key, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (provider_id) DO UPDATE
                    SET api_key = EXCLUDED.api_key, updated_at = NOW()
                    """,
                    (provider_id, api_key),
                )

                models = _fetch_models(provider_id, api_key)
                for m in models:
                    # Belt-and-suspenders: provider fetchers already filter
                    # DEACTIVATE_MODEL_IDS, but keeping the check here means
                    # any future fetcher is protected too.
                    if m["id"] in DEACTIVATE_MODEL_IDS:
                        continue
                    pricing = resolve_model_pricing(provider_id, m["id"])
                    context_window = resolve_model_context_window(provider_id, m["id"])
                    cur.execute(
                        """
                        INSERT INTO models (
                            model_id,
                            provider_id,
                            display_name,
                            input_microdollars_per_million,
                            output_microdollars_per_million,
                            context_window,
                            is_active,
                            created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, true, NOW())
                        ON CONFLICT (provider_id, model_id) DO UPDATE SET
                            is_active = true,
                            display_name = EXCLUDED.display_name,
                            input_microdollars_per_million = EXCLUDED.input_microdollars_per_million,
                            output_microdollars_per_million = EXCLUDED.output_microdollars_per_million,
                            context_window = EXCLUDED.context_window
                        """,
                        (
                            m["id"],
                            provider_id,
                            m["display_name"],
                            pricing["input"],
                            pricing["output"],
                            context_window,
                        ),
                    )

            conn.commit()

            cur.execute("SELECT COUNT(*) FROM models WHERE is_active = true")
            active_count = cur.fetchone()[0]
            if configured_provider_ids:
                print(
                    f"[Discovery] Sync complete. {active_count} models are currently active."
                )
            else:
                print(
                    "[Discovery] No provider secrets configured; cleared stale provider "
                    f"data for {len(stale_provider_ids)} provider(s) and left {active_count} active models."
                )

            return {
                "configured_providers": sorted(configured_provider_ids),
                "active_models": active_count,
                "stale_providers_removed": stale_provider_ids,
            }

        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
            conn.commit()


def _probe_openai_embedding(api_key: str) -> bool:
    """Make a minimal real embedding call to confirm the key works.

    Uses a ~1-token input so the cost is negligible. Returns True on any
    2xx response, False on any HTTP / transport error. Logs the failure
    reason at WARNING level so operators can tell "bad key" from "provider
    outage" without reading stack traces.
    """
    try:
        req = urllib.request.Request(
            EMBEDDING_PROBE_URL,
            data=json.dumps(
                {"model": EMBEDDING_MODEL_ID, "input": EMBEDDING_PROBE_INPUT}
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            return 200 <= getattr(response, "status", response.getcode()) < 300
    except HTTPError as exc:
        print(f"[Discovery] Embedding provider probe failed: HTTP {exc.code}")
        return False
    except Exception as exc:
        print(f"[Discovery] Embedding provider probe failed: {exc}")
        return False


def _count_memory_enabled_agents(conn) -> int:
    """Return the number of agents with ``memory.enabled = true``. If the
    ``agents`` table doesn't exist (early-stage CI / bootstrap), returns
    zero. We explicitly do not want the embedding check to fail startup
    just because Phase 2 Track 1's migration hasn't been applied yet.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(MEMORY_ENABLED_AGENT_COUNT_SQL)
            row = cur.fetchone()
            if row is None:
                return 0
            return int(row[0] or 0)
    except Exception as exc:  # table missing, schema drift, etc.
        print(f"[Discovery] Unable to count memory-enabled agents: {exc}")
        return 0


def validate_embedding_provider(
    conn,
    *,
    api_key: str | None,
    probe=None,
):
    """Validate the embedding provider's credentials at discovery time.

    Behaviour follows Track 5 Task 5:

    * Key present AND probe succeeds → upsert a row into ``models`` for
      ``EMBEDDING_MODEL_ID`` with the published input price so downstream
      cost accounting can find it.
    * Key absent or probe fails AND any ``memory.enabled = true`` agent
      exists → raise ``RuntimeError`` so the caller fails startup fast
      with a diagnostic message. Memory-enabled agents cannot function
      without this credential; silent continuation would turn every
      memory-write into a deferred-embedding ``content_vec = NULL``.
    * Key absent or probe fails AND no memory-enabled agents exist →
      log a warning and continue. Memory-disabled agents are unaffected.

    The ``probe`` hook exists for tests; production callers rely on the
    default probe which actually hits the embedding endpoint.
    """
    probe_fn = probe if probe is not None else _probe_openai_embedding

    key_valid = bool(api_key) and probe_fn(api_key)
    if key_valid:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO models (
                    model_id,
                    provider_id,
                    display_name,
                    input_microdollars_per_million,
                    output_microdollars_per_million,
                    is_active,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s, true, NOW())
                ON CONFLICT (provider_id, model_id) DO UPDATE SET
                    is_active = true,
                    display_name = EXCLUDED.display_name,
                    input_microdollars_per_million = EXCLUDED.input_microdollars_per_million
                """,
                (
                    EMBEDDING_MODEL_ID,
                    EMBEDDING_PROVIDER_ID,
                    EMBEDDING_MODEL_DISPLAY_NAME,
                    EMBEDDING_MODEL_INPUT_MICRODOLLARS_PER_MTOK,
                    0,  # embedding models have no output token billing
                ),
            )
        print(
            f"[Discovery] Embedding provider validated (provider={EMBEDDING_PROVIDER_ID}, "
            f"model={EMBEDDING_MODEL_ID})."
        )
        return

    memory_enabled_agents = _count_memory_enabled_agents(conn)
    reason = "missing" if not api_key else "invalid or unreachable"
    if memory_enabled_agents > 0:
        raise RuntimeError(
            "Embedding provider key "
            f"{reason} — required because {memory_enabled_agents} memory-enabled "
            f"agent(s) exist. Set the embedding provider ({EMBEDDING_PROVIDER_ID}) "
            "API key or disable memory on those agents."
        )

    print(
        f"[Discovery] WARNING: Embedding provider key {reason} and no memory-enabled "
        "agents exist. Memory-disabled agents continue to work; enabling memory on "
        "any agent will require a valid embedding provider key."
    )


def run_discovery() -> dict[str, Any]:
    provider_keys = _load_provider_api_keys()
    conn = get_db_connection()
    try:
        result = upsert_models(conn, provider_keys)
        validate_embedding_provider(
            conn,
            api_key=provider_keys.get(EMBEDDING_PROVIDER_ID),
        )
        conn.commit()
        return result
    finally:
        conn.close()


def lambda_handler(event, context):  # noqa: D401 - AWS Lambda entrypoint
    """Lambda entrypoint used by the scheduled job and deploy-time invocation."""
    return run_discovery()


def main() -> int:
    result = run_discovery()
    print(
        "[Discovery] Completed discovery run with "
        f"{result['active_models']} active models across "
        f"{len(result['configured_providers'])} configured provider(s)."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"[Discovery] FATAL ERROR: {e}")
        sys.exit(1)
