CREATE TABLE provider_keys (
    provider_id   TEXT PRIMARY KEY,
    api_key       TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE models (
    model_id                        TEXT NOT NULL,
    provider_id                     TEXT NOT NULL REFERENCES provider_keys(provider_id),
    display_name                    TEXT NOT NULL,
    is_active                       BOOLEAN NOT NULL DEFAULT true,
    input_microdollars_per_million   BIGINT NOT NULL DEFAULT 0,
    output_microdollars_per_million  BIGINT NOT NULL DEFAULT 0,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (provider_id, model_id)
);
