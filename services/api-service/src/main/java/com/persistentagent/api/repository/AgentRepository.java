package com.persistentagent.api.repository;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;

@Repository
public class AgentRepository {

    private final JdbcTemplate jdbcTemplate;

    public AgentRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    /**
     * Inserts a new agent for the given tenant.
     * Returns the created_at and updated_at timestamps.
     */
    public Map<String, Object> insert(String tenantId, String agentId, String displayName,
            String agentConfigJson) {
        String sql = """
                INSERT INTO agents (tenant_id, agent_id, display_name, agent_config)
                VALUES (?, ?, ?, ?::jsonb)
                RETURNING created_at, updated_at
                """;
        return jdbcTemplate.queryForMap(sql, tenantId, agentId, displayName, agentConfigJson);
    }

    /**
     * Finds an agent by ID scoped to tenant. Returns all columns including agent_config.
     */
    public Optional<Map<String, Object>> findByIdAndTenant(String tenantId, String agentId) {
        String sql = """
                SELECT agent_id, display_name, agent_config, status, created_at, updated_at
                FROM agents
                WHERE tenant_id = ? AND agent_id = ?
                """;
        List<Map<String, Object>> results = jdbcTemplate.queryForList(sql, tenantId, agentId);
        return results.isEmpty() ? Optional.empty() : Optional.of(results.get(0));
    }

    /**
     * Lists agents for the given tenant with summary fields only (no agent_config payload).
     * Extracts provider and model from agent_config JSONB.
     * Supports optional status filter. Ordered by created_at DESC.
     */
    public List<Map<String, Object>> listByTenant(String tenantId, String status, int limit) {
        StringBuilder sql = new StringBuilder("""
                SELECT agent_id, display_name,
                       agent_config->>'provider' AS provider,
                       agent_config->>'model' AS model,
                       status, created_at, updated_at
                FROM agents
                WHERE tenant_id = ?""");

        List<Object> params = new ArrayList<>();
        params.add(tenantId);

        if (status != null && !status.isBlank()) {
            sql.append(" AND status = ?");
            params.add(status);
        }

        sql.append(" ORDER BY created_at DESC LIMIT ?");
        params.add(limit);

        return jdbcTemplate.queryForList(sql.toString(), params.toArray());
    }

    /**
     * Updates an existing agent. Full replacement of mutable fields.
     * Sets updated_at = NOW(). Returns the updated row, or empty if not found.
     */
    public Optional<Map<String, Object>> update(String tenantId, String agentId, String displayName,
            String agentConfigJson, String status) {
        String sql = """
                UPDATE agents
                SET display_name = ?, agent_config = ?::jsonb, status = ?, updated_at = NOW()
                WHERE tenant_id = ? AND agent_id = ?
                RETURNING agent_id, display_name, agent_config, status, created_at, updated_at
                """;
        List<Map<String, Object>> results = jdbcTemplate.queryForList(
                sql, displayName, agentConfigJson, status, tenantId, agentId);
        return results.isEmpty() ? Optional.empty() : Optional.of(results.get(0));
    }
}
