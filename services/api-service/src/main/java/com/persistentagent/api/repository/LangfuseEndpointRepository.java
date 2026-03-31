package com.persistentagent.api.repository;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

@Repository
public class LangfuseEndpointRepository {

    private final JdbcTemplate jdbcTemplate;

    public LangfuseEndpointRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    /**
     * Inserts a new Langfuse endpoint for the given tenant.
     * Returns the generated endpoint_id and created_at.
     */
    public Map<String, Object> insert(String tenantId, String name, String host,
            String publicKey, String secretKey) {
        String sql = """
                INSERT INTO langfuse_endpoints (tenant_id, name, host, public_key, secret_key)
                VALUES (?, ?, ?, ?, ?)
                RETURNING endpoint_id, created_at
                """;
        return jdbcTemplate.queryForMap(sql, tenantId, name, host, publicKey, secretKey);
    }

    /**
     * Finds a Langfuse endpoint by ID scoped to tenant.
     */
    public Optional<Map<String, Object>> findByIdAndTenant(UUID endpointId, String tenantId) {
        String sql = """
                SELECT endpoint_id, tenant_id, name, host, public_key, secret_key, created_at, updated_at
                FROM langfuse_endpoints
                WHERE endpoint_id = ? AND tenant_id = ?
                """;
        List<Map<String, Object>> results = jdbcTemplate.queryForList(sql, endpointId, tenantId);
        return results.isEmpty() ? Optional.empty() : Optional.of(results.get(0));
    }

    /**
     * Lists all Langfuse endpoints for the given tenant, ordered by most recent first.
     */
    public List<Map<String, Object>> listByTenant(String tenantId) {
        String sql = """
                SELECT endpoint_id, tenant_id, name, host, public_key, secret_key, created_at, updated_at
                FROM langfuse_endpoints
                WHERE tenant_id = ?
                ORDER BY created_at DESC
                """;
        return jdbcTemplate.queryForList(sql, tenantId);
    }

    /**
     * Updates an existing Langfuse endpoint. Returns true if a row was updated.
     */
    public boolean update(UUID endpointId, String tenantId, String name, String host,
            String publicKey, String secretKey) {
        String sql = """
                UPDATE langfuse_endpoints
                SET name = ?, host = ?, public_key = ?, secret_key = ?, updated_at = NOW()
                WHERE endpoint_id = ? AND tenant_id = ?
                """;
        int rows = jdbcTemplate.update(sql, name, host, publicKey, secretKey, endpointId, tenantId);
        return rows > 0;
    }

    /**
     * Deletes a Langfuse endpoint. Returns true if a row was deleted.
     */
    public boolean delete(UUID endpointId, String tenantId) {
        String sql = """
                DELETE FROM langfuse_endpoints
                WHERE endpoint_id = ? AND tenant_id = ?
                """;
        int rows = jdbcTemplate.update(sql, endpointId, tenantId);
        return rows > 0;
    }

    /**
     * Checks whether any active task (queued or running) references this endpoint.
     */
    public boolean isReferencedByActiveTask(UUID endpointId) {
        String sql = """
                SELECT EXISTS(SELECT 1 FROM tasks
                WHERE langfuse_endpoint_id = ? AND status IN ('queued', 'running'))
                """;
        return Boolean.TRUE.equals(jdbcTemplate.queryForObject(sql, Boolean.class, endpointId));
    }
}
