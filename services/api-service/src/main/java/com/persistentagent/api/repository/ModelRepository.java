package com.persistentagent.api.repository;

import com.persistentagent.api.model.response.ModelInfo;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.List;
import java.util.Optional;

@Repository
public class ModelRepository {
    private final JdbcTemplate jdbcTemplate;

    public ModelRepository(JdbcTemplate jdbcTemplate) {
        this.jdbcTemplate = jdbcTemplate;
    }

    public List<ModelInfo> findActiveModels() {
        return jdbcTemplate.query(
            "SELECT provider_id, model_id, display_name FROM models WHERE is_active = true ORDER BY provider_id, display_name",
            (rs, rowNum) -> new ModelInfo(
                rs.getString("provider_id"),
                rs.getString("model_id"),
                rs.getString("display_name")
            )
        );
    }
    
    public boolean isModelActive(String providerId, String modelId) {
        Integer count = jdbcTemplate.queryForObject(
            "SELECT count(*) FROM models WHERE provider_id = ? AND model_id = ? AND is_active = true",
            Integer.class,
            providerId, modelId
        );
        return count != null && count > 0;
    }

    /**
     * Returns the {@code context_window} token count for the given model, or
     * {@link Optional#empty()} when the column is absent from the row (e.g.,
     * older seeds) or the model cannot be resolved.
     *
     * <p>Used by {@link com.persistentagent.api.service.ConfigValidationHelper} to
     * enforce that a chosen {@code summarizer_model}'s context window is large enough
     * to hold the primary model's Tier 3 trigger. When the column value is {@code NULL}
     * (or the model is missing), the check is skipped — graceful degradation rather
     * than a false-positive 400.
     *
     * <p>The {@code context_window} column was added in migration {@code 0014_model_context_window.sql}.
     */
    public Optional<Integer> getContextWindow(String providerId, String modelId) {
        try {
            List<Integer> result = jdbcTemplate.query(
                "SELECT context_window FROM models WHERE provider_id = ? AND model_id = ?",
                (rs, rowNum) -> {
                    int val = rs.getInt("context_window");
                    return rs.wasNull() ? null : val;
                },
                providerId, modelId
            );
            if (result.isEmpty() || result.get(0) == null) {
                return Optional.empty();
            }
            return Optional.of(result.get(0));
        } catch (Exception e) {
            // Column may not exist on an older schema — fail open.
            return Optional.empty();
        }
    }
}
