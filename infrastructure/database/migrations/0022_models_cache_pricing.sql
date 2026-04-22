-- 0022: Cache-token pricing for supported providers.
--
-- Prompt caching changes the cost shape: Anthropic charges ~1.25x the input
-- rate for cache-creation tokens and ~0.1x for cache-read tokens; OpenAI
-- simply charges a discounted read rate. To keep cost accounting faithful
-- we store per-model cache rates alongside the existing input/output rates.
--
-- Both columns are nullable. When NULL the worker falls back to
-- input_microdollars_per_million (conservative — over-reports cache-hit
-- savings rather than under-reporting spend). model-discovery populates
-- the explicit values for known Anthropic/Bedrock models on its next run.

ALTER TABLE models
    ADD COLUMN cache_creation_microdollars_per_million BIGINT,
    ADD COLUMN cache_read_microdollars_per_million     BIGINT;
