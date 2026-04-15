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

LOCK_ID = 543210987
DB_CREDENTIALS_SECRET_ENV = "DB_CREDENTIALS_SECRET_ARN"
PROVIDER_SOURCES = (
    ("anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_SECRET_ARN"),
    ("openai", "OPENAI_API_KEY", "OPENAI_API_KEY_SECRET_ARN"),
    ("bedrock", "AWS_BEARER_TOKEN_BEDROCK", "AWS_BEARER_TOKEN_BEDROCK_SECRET_ARN"),
)

AWS_BEDROCK_REGION_ENV = "AWS_BEDROCK_REGION"
AWS_BEDROCK_DEFAULT_REGION = "us-east-1"


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
                if m_id.startswith("gpt-") or m_id.startswith("o1") or m_id.startswith("o3"):
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
                    pricing = resolve_model_pricing(provider_id, m["id"])
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
                            input_microdollars_per_million = EXCLUDED.input_microdollars_per_million,
                            output_microdollars_per_million = EXCLUDED.output_microdollars_per_million
                        """,
                        (m["id"], provider_id, m["display_name"], pricing["input"], pricing["output"]),
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


def run_discovery() -> dict[str, Any]:
    provider_keys = _load_provider_api_keys()
    conn = get_db_connection()
    try:
        return upsert_models(conn, provider_keys)
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
