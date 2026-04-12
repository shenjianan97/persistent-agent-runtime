package com.persistentagent.api.model;

import java.time.OffsetDateTime;
import java.util.UUID;

/**
 * Metadata for a task artifact (input or output file).
 */
public record ArtifactMetadata(
    UUID artifactId,
    UUID taskId,
    String filename,
    String direction,
    String contentType,
    long sizeBytes,
    OffsetDateTime createdAt
) {}
