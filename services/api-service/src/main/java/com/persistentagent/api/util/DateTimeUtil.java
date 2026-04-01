package com.persistentagent.api.util;

import java.sql.Timestamp;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;

/**
 * Shared utility for converting JDBC result values to OffsetDateTime.
 */
public final class DateTimeUtil {

    private DateTimeUtil() {
    }

    /**
     * Converts a JDBC result value (Timestamp, Date, or OffsetDateTime) to OffsetDateTime in UTC.
     * Returns null if the value is null or not a recognized type.
     */
    public static OffsetDateTime toOffsetDateTime(Object value) {
        if (value == null) return null;
        if (value instanceof OffsetDateTime odt) return odt;
        if (value instanceof Timestamp ts) return ts.toInstant().atOffset(ZoneOffset.UTC);
        if (value instanceof java.util.Date d) return d.toInstant().atOffset(ZoneOffset.UTC);
        return null;
    }
}
