package com.persistentagent.api.util;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.postgresql.util.PGobject;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

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
}
