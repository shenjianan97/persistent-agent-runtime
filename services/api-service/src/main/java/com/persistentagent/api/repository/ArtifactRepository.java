package com.persistentagent.api.repository;

import com.persistentagent.api.model.ArtifactMetadata;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

import java.sql.ResultSet;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

@Repository
public class ArtifactRepository {

    private final JdbcTemplate jdbcTemplate;

    public ArtifactRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    private static final RowMapper<ArtifactMetadata> ARTIFACT_MAPPER = (ResultSet rs, int rowNum) -> {
        return new ArtifactMetadata(
            UUID.fromString(rs.getString("artifact_id")),
            UUID.fromString(rs.getString("task_id")),
            rs.getString("filename"),
            rs.getString("direction"),
            rs.getString("content_type"),
            rs.getLong("size_bytes"),
            rs.getObject("created_at", OffsetDateTime.class)
        );
    };

    public UUID insert(UUID taskId, String tenantId, String filename, String direction,
                       String contentType, long sizeBytes, String s3Key) {
        String sql = """
                INSERT INTO task_artifacts (task_id, tenant_id, filename, direction, content_type, size_bytes, s3_key)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                RETURNING artifact_id
                """;

        return jdbcTemplate.queryForObject(sql, UUID.class,
                taskId, tenantId, filename, direction, contentType, sizeBytes, s3Key);
    }

    public List<ArtifactMetadata> findByTaskId(UUID taskId, String tenantId, String direction) {
        if (direction != null && !direction.isBlank()) {
            String sql = """
                    SELECT artifact_id, task_id, filename, direction, content_type, size_bytes, created_at
                    FROM task_artifacts
                    WHERE task_id = ? AND tenant_id = ? AND direction = ?
                    ORDER BY created_at ASC
                    """;
            return jdbcTemplate.query(sql, ARTIFACT_MAPPER, taskId, tenantId, direction);
        }

        String sql = """
                SELECT artifact_id, task_id, filename, direction, content_type, size_bytes, created_at
                FROM task_artifacts
                WHERE task_id = ? AND tenant_id = ?
                ORDER BY created_at ASC
                """;
        return jdbcTemplate.query(sql, ARTIFACT_MAPPER, taskId, tenantId);
    }

    public Optional<ArtifactWithS3Key> findByTaskIdAndFilename(
            UUID taskId, String tenantId, String filename, String direction) {
        String sql = """
                SELECT artifact_id, task_id, filename, direction, content_type, size_bytes, s3_key, created_at
                FROM task_artifacts
                WHERE task_id = ? AND tenant_id = ? AND filename = ? AND direction = ?
                """;
        List<ArtifactWithS3Key> results = jdbcTemplate.query(sql,
                (ResultSet rs, int rowNum) -> new ArtifactWithS3Key(
                    new ArtifactMetadata(
                        UUID.fromString(rs.getString("artifact_id")),
                        UUID.fromString(rs.getString("task_id")),
                        rs.getString("filename"),
                        rs.getString("direction"),
                        rs.getString("content_type"),
                        rs.getLong("size_bytes"),
                        rs.getObject("created_at", OffsetDateTime.class)
                    ),
                    rs.getString("s3_key")
                ),
                taskId, tenantId, filename, direction);
        return results.isEmpty() ? Optional.empty() : Optional.of(results.get(0));
    }

    public record ArtifactWithS3Key(ArtifactMetadata metadata, String s3Key) {}
}
