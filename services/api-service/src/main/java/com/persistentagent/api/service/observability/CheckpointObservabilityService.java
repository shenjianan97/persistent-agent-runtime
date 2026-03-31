package com.persistentagent.api.service.observability;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.repository.TaskRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.sql.Timestamp;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Service
public class CheckpointObservabilityService implements TaskObservabilityService {
    private static final Logger logger = LoggerFactory.getLogger(CheckpointObservabilityService.class);

    private final TaskRepository taskRepository;
    private final ObjectMapper objectMapper;

    public CheckpointObservabilityService(TaskRepository taskRepository, ObjectMapper objectMapper) {
        this.taskRepository = taskRepository;
        this.objectMapper = objectMapper;
    }

    @Override
    public CheckpointCostTotals getTaskCostTotals(UUID taskId, String tenantId) {
        List<Map<String, Object>> rows = taskRepository.getCheckpoints(taskId, tenantId)
                .orElse(List.of());

        if (rows.isEmpty()) {
            return CheckpointCostTotals.empty();
        }

        long totalCostMicrodollars = 0L;
        int inputTokens = 0;
        int outputTokens = 0;

        for (Map<String, Object> row : rows) {
            Object costObj = row.get("cost_microdollars");
            if (costObj instanceof Number number) {
                totalCostMicrodollars += number.longValue();
            }

            Object execMeta = row.get("execution_metadata");
            if (execMeta != null) {
                try {
                    String json = execMeta.toString();
                    JsonNode node = objectMapper.readTree(json);
                    inputTokens += nodeInt(node, "input_tokens");
                    outputTokens += nodeInt(node, "output_tokens");
                } catch (Exception e) {
                    logger.debug("Could not parse execution_metadata for checkpoint in task {}", taskId, e);
                }
            }
        }

        int totalTokens = inputTokens + outputTokens;

        Long durationMs = null;
        if (rows.size() >= 2) {
            OffsetDateTime first = toOffsetDateTime(rows.get(0).get("created_at"));
            OffsetDateTime last = toOffsetDateTime(rows.get(rows.size() - 1).get("created_at"));
            if (first != null && last != null) {
                durationMs = java.time.Duration.between(first, last).toMillis();
            }
        }

        return new CheckpointCostTotals(totalCostMicrodollars, inputTokens, outputTokens, totalTokens, durationMs);
    }

    private static int nodeInt(JsonNode node, String field) {
        JsonNode child = node.get(field);
        return (child != null && child.isNumber()) ? child.asInt() : 0;
    }

    private static OffsetDateTime toOffsetDateTime(Object value) {
        if (value == null) return null;
        if (value instanceof OffsetDateTime odt) return odt;
        if (value instanceof Timestamp ts) return ts.toInstant().atOffset(ZoneOffset.UTC);
        if (value instanceof java.util.Date d) return d.toInstant().atOffset(ZoneOffset.UTC);
        return null;
    }
}
