-- Phase 2 Track 4: Tool Server Constraint Hardening
-- Adds missing CHECK constraints to tool_servers for auth consistency,
-- stricter name validation, and max-length guards.

-- Step 1: Auth token consistency — auth_token must be NULL when auth_type is 'none',
--         and NOT NULL when auth_type is 'bearer_token'
ALTER TABLE tool_servers ADD CONSTRAINT chk_auth_token_consistency CHECK (
    (auth_type = 'none' AND auth_token IS NULL)
    OR (auth_type = 'bearer_token' AND auth_token IS NOT NULL)
);

-- Step 2: Stricter name regex — disallow trailing hyphens
ALTER TABLE tool_servers DROP CONSTRAINT tool_servers_name_check;
ALTER TABLE tool_servers ADD CONSTRAINT tool_servers_name_check CHECK (name ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$');

-- Step 3: Max-length constraints for name, url, and auth_token
ALTER TABLE tool_servers ADD CONSTRAINT chk_name_length CHECK (char_length(name) <= 100);
ALTER TABLE tool_servers ADD CONSTRAINT chk_url_length CHECK (char_length(url) <= 2048);
ALTER TABLE tool_servers ADD CONSTRAINT chk_auth_token_length CHECK (auth_token IS NULL OR char_length(auth_token) <= 4096);
