-- Seed data for CI / integration tests.
-- Provides the minimum provider + model rows so that task-submission
-- validation passes for the models used in tests.

INSERT INTO provider_keys (provider_id, api_key)
VALUES ('anthropic', 'test-key-not-real')
ON CONFLICT (provider_id) DO NOTHING;

INSERT INTO models (provider_id, model_id, display_name, is_active)
VALUES ('anthropic', 'claude-sonnet-4-6', 'Claude Sonnet 4.6', true)
ON CONFLICT (provider_id, model_id) DO NOTHING;
