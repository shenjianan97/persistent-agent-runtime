package com.persistentagent.api.util;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.postgresql.util.PGobject;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Map;

/**
 * Shared utility for parsing JSONB / String values from JDBC result maps.
 */
public final class JsonParseUtil {

    private static final Logger log = LoggerFactory.getLogger(JsonParseUtil.class);

    private JsonParseUtil() {
    }

    /**
     * Parses a value that may be a JSON String or PGobject into a deserialized Object.
     * Returns the original value if parsing fails.
     */
    public static Object parseJson(ObjectMapper objectMapper, Object value, String fieldName, String contextId) {
        if (value == null) {
            return null;
        }

        try {
            if (value instanceof String s) {
                return objectMapper.readValue(s, Object.class);
            }
            if (value instanceof PGobject pgObj) {
                String raw = pgObj.getValue();
                if (raw == null) {
                    return null;
                }
                return objectMapper.readValue(raw, Object.class);
            }
        } catch (Exception e) {
            log.debug("Failed to parse {} for context {}", fieldName, contextId, e);
        }

        return value;
    }

    /**
     * Parses a JSONB / String / already-deserialized payload into a
     * {@code Map<String, Object>}. Shared by the checkpoint-payload readers
     * ({@code ActivityProjectionService}, {@code TaskPlanService}) so the
     * Activity view and Plan view can never diverge on how they read
     * {@code checkpoint_payload}.
     *
     * <p>Semantics (intentionally different from {@link #parseJson}):
     * <ul>
     *   <li>{@code null} input → {@code null}</li>
     *   <li>already a {@code Map} → returned as-is (no re-serialization)</li>
     *   <li>{@code PGobject} → its string value is parsed; other types via
     *       {@code toString()}</li>
     *   <li>blank string or parse failure → {@code null}, with a WARN-level
     *       log (a checkpoint payload that fails to parse is an anomaly worth
     *       surfacing, unlike the best-effort {@link #parseJson} fields)</li>
     * </ul>
     */
    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseJsonMap(ObjectMapper objectMapper, Object value) {
        if (value == null) {
            return null;
        }
        if (value instanceof Map<?, ?> map) {
            return (Map<String, Object>) map;
        }
        String json;
        if (value instanceof PGobject pg) {
            json = pg.getValue();
        } else {
            json = value.toString();
        }
        if (json == null || json.isBlank()) {
            return null;
        }
        try {
            return objectMapper.readValue(json, Map.class);
        } catch (Exception e) {
            log.warn("Failed to parse JSON payload into a map: {}", e.getMessage());
            return null;
        }
    }
}
