package com.persistentagent.api.repository;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.*;

@Repository
public class ToolServerRepository {

    private final JdbcTemplate jdbcTemplate;

    public ToolServerRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public Map<String, Object> insert(String tenantId, String name, String url,
                                       String authType, String authToken) {
        return jdbcTemplate.queryForMap(
            """
            INSERT INTO tool_servers (tenant_id, name, url, auth_type, auth_token)
            VALUES (?, ?, ?, ?, ?)
            RETURNING server_id, tenant_id, name, url, auth_type, auth_token, status, created_at, updated_at
            """,
            tenantId, name, url, authType, authToken
        );
    }

    public List<Map<String, Object>> listByTenant(String tenantId, String status, int limit) {
        if (status != null && !status.isBlank()) {
            return jdbcTemplate.queryForList(
                """
                SELECT server_id, tenant_id, name, url, auth_type, status, created_at, updated_at
                FROM tool_servers
                WHERE tenant_id = ? AND status = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                tenantId, status, limit
            );
        }
        return jdbcTemplate.queryForList(
            """
            SELECT server_id, tenant_id, name, url, auth_type, status, created_at, updated_at
            FROM tool_servers
            WHERE tenant_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            tenantId, limit
        );
    }

    public Optional<Map<String, Object>> findById(String tenantId, String serverId) {
        List<Map<String, Object>> rows = jdbcTemplate.queryForList(
            """
            SELECT server_id, tenant_id, name, url, auth_type, auth_token, status, created_at, updated_at
            FROM tool_servers
            WHERE tenant_id = ? AND server_id = ?::uuid
            """,
            tenantId, serverId
        );
        return rows.isEmpty() ? Optional.empty() : Optional.of(rows.get(0));
    }

    public Optional<Map<String, Object>> update(String tenantId, String serverId,
                                                  String name, String url,
                                                  String authType, String authToken,
                                                  String status) {
        List<Map<String, Object>> rows = jdbcTemplate.queryForList(
            """
            UPDATE tool_servers
            SET name = COALESCE(?, name),
                url = COALESCE(?, url),
                auth_type = COALESCE(?, auth_type),
                auth_token = CASE WHEN ? IS NOT NULL THEN ? ELSE auth_token END,
                status = COALESCE(?, status),
                updated_at = NOW()
            WHERE tenant_id = ? AND server_id = ?::uuid
            RETURNING server_id, tenant_id, name, url, auth_type, auth_token, status, created_at, updated_at
            """,
            name, url, authType, authToken, authToken, status, tenantId, serverId
        );
        return rows.isEmpty() ? Optional.empty() : Optional.of(rows.get(0));
    }

    public boolean delete(String tenantId, String serverId) {
        int affected = jdbcTemplate.update(
            "DELETE FROM tool_servers WHERE tenant_id = ? AND server_id = ?::uuid",
            tenantId, serverId
        );
        return affected > 0;
    }

    public List<Map<String, Object>> findByTenantAndNames(String tenantId, List<String> names) {
        if (names == null || names.isEmpty()) {
            return List.of();
        }
        String placeholders = String.join(",", Collections.nCopies(names.size(), "?"));
        List<Object> params = new ArrayList<>();
        params.add(tenantId);
        params.addAll(names);
        return jdbcTemplate.queryForList(
            "SELECT server_id, tenant_id, name, url, auth_type, status FROM tool_servers WHERE tenant_id = ? AND name IN (" + placeholders + ")",
            params.toArray()
        );
    }
}
