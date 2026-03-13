#!/usr/bin/env python3
import os
import sys
import json
import urllib.request
import urllib.error
from urllib.error import HTTPError
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
}

GLOBAL_FALLBACK_PRICING = {"input": 15_000_000, "output": 75_000_000}

# Conservative provider-level fallback pricing for newly discovered models that do
# not yet have an explicit entry in PRICING_DEFAULTS. This avoids silently
# under-reporting spend as zero while still allowing the model to be used.
PRICING_FALLBACKS = {
    "anthropic": {"input": 15_000_000, "output": 75_000_000},
    "openai": {"input": 15_000_000, "output": 60_000_000},
}

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

def get_db_connection():
    db_dsn = os.environ.get("DB_DSN")
    if db_dsn:
        return psycopg.connect(db_dsn)
        
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "55432")
    user = os.environ.get("DB_USER", "postgres")
    password = os.environ.get("DB_PASSWORD", "postgres")
    dbname = os.environ.get("DB_NAME", "persistent_agent_runtime")
    
    conninfo = f"host={host} port={port} user={user} password={password} dbname={dbname}"
    return psycopg.connect(conninfo)

def fetch_anthropic_models(api_key):
    print("[Discovery] Querying Anthropic models...")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    )
    models = []
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            for m in data.get("data", []):
                if m.get("type") == "model":
                    models.append({
                        "id": m["id"],
                        "display_name": m.get("display_name") or m["id"]
                    })
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
        }
    )
    models = []
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            for m in data.get("data", []):
                # Filter to chat models only to avoid cluttering UI with whisper/dall-e/tts
                m_id = m["id"]
                if m_id.startswith("gpt-") or m_id.startswith("o1") or m_id.startswith("o3"):
                    models.append({
                        "id": m_id,
                        "display_name": m_id
                    })
    except HTTPError as e:
        print(f"[Discovery] Failed to fetch OpenAI models: HTTP {e.code}")
    except Exception as e:
        print(f"[Discovery] Failed to fetch OpenAI models: {e}")
    return models

def upsert_models(conn, num_providers_found):
    print(f"[Discovery] Starting database sync (found {num_providers_found} provider keys)")
    
    # Use a PostgreSQL advisory lock to prevent concurrent writers from interfering
    LOCK_ID = 543210987
    
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            # First, mark everything inactive. We will exclusively reactivate what we find.
            cur.execute("UPDATE models SET is_active = false")
            
            # --- Anthropic ---
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
            if anthropic_key:
                cur.execute("""
                    INSERT INTO provider_keys (provider_id, api_key, updated_at)
                    VALUES ('anthropic', %s, NOW())
                    ON CONFLICT (provider_id) DO UPDATE SET api_key = EXCLUDED.api_key, updated_at = NOW()
                """, (anthropic_key,))
                
                models = fetch_anthropic_models(anthropic_key)
                for m in models:
                    pricing = resolve_model_pricing("anthropic", m["id"])
                    cur.execute("""
                        INSERT INTO models (model_id, provider_id, display_name, input_microdollars_per_million, output_microdollars_per_million, is_active, created_at)
                        VALUES (%s, 'anthropic', %s, %s, %s, true, NOW())
                        ON CONFLICT (provider_id, model_id) DO UPDATE SET
                            is_active = true,
                            display_name = EXCLUDED.display_name,
                            input_microdollars_per_million = EXCLUDED.input_microdollars_per_million,
                            output_microdollars_per_million = EXCLUDED.output_microdollars_per_million
                    """, (m["id"], m["display_name"], pricing["input"], pricing["output"]))
            
            # --- OpenAI ---
            openai_key = os.environ.get("OPENAI_API_KEY")
            if openai_key:
                cur.execute("""
                    INSERT INTO provider_keys (provider_id, api_key, updated_at)
                    VALUES ('openai', %s, NOW())
                    ON CONFLICT (provider_id) DO UPDATE SET api_key = EXCLUDED.api_key, updated_at = NOW()
                """, (openai_key,))
                
                models = fetch_openai_models(openai_key)
                for m in models:
                    pricing = resolve_model_pricing("openai", m["id"])
                    cur.execute("""
                        INSERT INTO models (model_id, provider_id, display_name, input_microdollars_per_million, output_microdollars_per_million, is_active, created_at)
                        VALUES (%s, 'openai', %s, %s, %s, true, NOW())
                        ON CONFLICT (provider_id, model_id) DO UPDATE SET
                            is_active = true,
                            display_name = EXCLUDED.display_name,
                            input_microdollars_per_million = EXCLUDED.input_microdollars_per_million,
                            output_microdollars_per_million = EXCLUDED.output_microdollars_per_million
                    """, (m["id"], m["display_name"], pricing["input"], pricing["output"]))
                    
            # --- AWS Bedrock (Example Future Integration) ---
            # bedrock_access_key = os.environ.get("AWS_ACCESS_KEY_ID")
            # bedrock_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
            # bedrock_region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
            # if bedrock_access_key and bedrock_secret_key:
            #     # Encode credentials securely into the single string column
            #     api_key_str = f"{bedrock_access_key}:{bedrock_secret_key}:{bedrock_region}"
            #     cur.execute("""
            #         INSERT INTO provider_keys (provider_id, api_key, updated_at)
            #         VALUES ('bedrock', %s, NOW())
            #         ON CONFLICT (provider_id) DO UPDATE SET api_key = EXCLUDED.api_key, updated_at = NOW()
            #     """, (api_key_str,))
            #     
            #     # Example boto3 fetching routine:
            #     # import boto3
            #     # client = boto3.client('bedrock', region_name=bedrock_region, ...)
            #     # models = client.list_foundation_models(byOutputModality='TEXT')
            #     # for m in models.get('modelSummaries', []):
            #     #     # Execute INSERT into models using m['modelId'] and 'bedrock' provider...
            
            conn.commit()
            
            cur.execute("SELECT COUNT(*) FROM models WHERE is_active = true")
            active_count = cur.fetchone()[0]
            print(f"[Discovery] Sync complete. {active_count} models are currently active.")
            
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
            conn.commit()

if __name__ == "__main__":
    found = 0
    if os.environ.get("ANTHROPIC_API_KEY"): found += 1
    if os.environ.get("OPENAI_API_KEY"): found += 1
    
    if found == 0:
        print("[Discovery] WARNING: No LLM API keys found in environment. No models will be activated.")
    
    try:
        conn = get_db_connection()
        upsert_models(conn, found)
        conn.close()
    except Exception as e:
        print(f"[Discovery] FATAL ERROR: {e}")
        sys.exit(1)
