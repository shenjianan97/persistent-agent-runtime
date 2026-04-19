package com.persistentagent.api.config;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for {@link DeadLetterReason} enum — verifies JSON serialization
 * round-trip and that every enum constant is present in
 * {@link ValidationConstants#ALLOWED_DEAD_LETTER_REASONS}.
 *
 * Track 7, Task 10: ensures {@code CONTEXT_EXCEEDED_IRRECOVERABLE} is
 * correctly registered.
 */
class DeadLetterReasonTest {

    private final ObjectMapper objectMapper = new ObjectMapper();

    // -----------------------------------------------------------------------
    // CONTEXT_EXCEEDED_IRRECOVERABLE value
    // -----------------------------------------------------------------------

    @Test
    void contextExceededIrrecoverable_hasCorrectedJsonValue() {
        assertEquals(
                "context_exceeded_irrecoverable",
                DeadLetterReason.CONTEXT_EXCEEDED_IRRECOVERABLE.getValue(),
                "Enum constant must carry the snake_case wire value"
        );
    }

    @Test
    void contextExceededIrrecoverable_serializesToSnakeCase() throws JsonProcessingException {
        String json = objectMapper.writeValueAsString(DeadLetterReason.CONTEXT_EXCEEDED_IRRECOVERABLE);
        assertEquals("\"context_exceeded_irrecoverable\"", json,
                "Jackson must write the snake_case wire value, not the enum name");
    }

    @Test
    void contextExceededIrrecoverable_deserializesFromSnakeCase() throws JsonProcessingException {
        DeadLetterReason reason = objectMapper.readValue(
                "\"context_exceeded_irrecoverable\"", DeadLetterReason.class);
        assertEquals(DeadLetterReason.CONTEXT_EXCEEDED_IRRECOVERABLE, reason);
    }

    // -----------------------------------------------------------------------
    // All enum constants must have matching entries in ValidationConstants
    // -----------------------------------------------------------------------

    @ParameterizedTest
    @EnumSource(DeadLetterReason.class)
    void allEnumValues_presentInValidationConstants(DeadLetterReason reason) {
        assertTrue(
                ValidationConstants.ALLOWED_DEAD_LETTER_REASONS.contains(reason.getValue()),
                "ValidationConstants.ALLOWED_DEAD_LETTER_REASONS must include: " + reason.getValue()
        );
    }

    // -----------------------------------------------------------------------
    // Bidirectional serialization for every constant
    // -----------------------------------------------------------------------

    @ParameterizedTest
    @EnumSource(DeadLetterReason.class)
    void allEnumValues_roundTripThroughJackson(DeadLetterReason reason) throws JsonProcessingException {
        String json = objectMapper.writeValueAsString(reason);
        DeadLetterReason deserialized = objectMapper.readValue(json, DeadLetterReason.class);
        assertEquals(reason, deserialized,
                "Round-trip serialization must be identity for: " + reason);
    }
}
