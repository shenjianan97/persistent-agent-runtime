package com.persistentagent.api.repository;

import com.persistentagent.api.model.response.ModelInfo;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.List;

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
}
