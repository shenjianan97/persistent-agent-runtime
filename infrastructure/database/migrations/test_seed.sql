-- Seed data for CI / integration tests.
-- Provides the minimum provider + model rows so that task-submission
-- validation passes for the models used in tests.

INSERT INTO provider_keys (provider_id, api_key)
VALUES ('anthropic', 'test-key-not-real')
ON CONFLICT (provider_id) DO NOTHING;

INSERT INTO models (provider_id, model_id, display_name, is_active,
                    input_microdollars_per_million, output_microdollars_per_million)
VALUES ('anthropic', 'claude-sonnet-4-6', 'Claude Sonnet 4.6', true, 3000000, 15000000)
ON CONFLICT (provider_id, model_id) DO UPDATE SET
    input_microdollars_per_million = EXCLUDED.input_microdollars_per_million,
    output_microdollars_per_million = EXCLUDED.output_microdollars_per_million;

-- Langfuse endpoint for local dev
INSERT INTO langfuse_endpoints (tenant_id, name, host, public_key, secret_key)
VALUES ('default', 'Local Dev', 'http://127.0.0.1:3300', 'pk-lf-local', 'sk-lf-local')
ON CONFLICT (tenant_id, name) DO NOTHING;
