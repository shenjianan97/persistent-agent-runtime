<!-- AGENT_TASK_START: task-4-api-artifact-repo-and-s3.md -->

# Task 4 — API Artifact Repository and S3 Storage Service

## Agent Instructions

You are a software engineer implementing one module of a larger system. Your scope is strictly limited to this task. Do not modify components outside the "Affected Component" listed below.

**CRITICAL PRE-WORK:** Before beginning implementation, you MUST read:
1. `docs/design-docs/agent-capabilities/design.md` — canonical design contract (Section 2: Artifact Storage, Section 4: API Service Changes)
2. `docs/exec-plans/active/agent-capabilities/track-1/plan.md` — Track 1 execution plan
3. `services/api-service/src/main/java/com/persistentagent/api/repository/TaskRepository.java` — existing repository pattern
4. `services/api-service/build.gradle` — current dependencies
5. `services/api-service/src/main/resources/application.yml` — current app config
6. `infrastructure/database/migrations/0009_artifact_storage.sql` — `task_artifacts` table schema (Task 1 output)

**CRITICAL POST-WORK:** After completing this task:
1. Run the full test suite with `make test` and verify all existing tests still pass. Fix any regressions before proceeding.
2. Update the status in `docs/exec-plans/active/agent-capabilities/track-1/progress.md` to "Done".

## Context

Track 1 needs the API service to query artifact metadata from the database and stream artifact files from S3. This task creates the data access layer (`ArtifactRepository`) and the S3 service (`S3StorageService`) that the API endpoints (Task 5) will use.

The repository follows the same JDBC pattern as `TaskRepository`. The S3 service uses AWS SDK v2 (not v1) for consistency with modern Spring Boot practices.

## Task-Specific Shared Contract

- `ArtifactRepository` uses `JdbcTemplate` with raw SQL, matching the `TaskRepository` pattern.
- `ArtifactMetadata` is a Java record for the artifact DTO.
- `S3StorageService` uses AWS SDK v2 `S3Client` (from `software.amazon.awssdk:s3`).
- S3 endpoint URL is configurable via `s3.endpoint-url` property (nullable for real AWS).
- S3 bucket name is configurable via `s3.bucket-name` property.
- Tenant scoping: all queries filter by `tenant_id`.

## Affected Component

- **Service/Module:** API Service — Repository and Storage
- **File paths:**
  - `services/api-service/src/main/java/com/persistentagent/api/repository/ArtifactRepository.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/model/ArtifactMetadata.java` (new)
  - `services/api-service/src/main/java/com/persistentagent/api/service/S3StorageService.java` (new)
  - `services/api-service/build.gradle` (modify — add AWS SDK S3 dependency)
  - `services/api-service/src/main/resources/application.yml` (modify — add S3 config)
  - `services/api-service/src/test/java/com/persistentagent/api/repository/ArtifactRepositoryTest.java` (new)
  - `services/api-service/src/test/java/com/persistentagent/api/service/S3StorageServiceTest.java` (new)
- **Change type:** new code + dependency addition + config modification

## Dependencies

- **Must complete first:** Task 1 (DB Migration — `task_artifacts` table must exist), Task 2 (LocalStack — S3 endpoint for testing)
- **Provides output to:** Task 5 (API Artifact Endpoints — uses `ArtifactRepository` and `S3StorageService`)
- **Shared interfaces/contracts:** `ArtifactMetadata` record, `ArtifactRepository` query API, `S3StorageService.download()` and `S3StorageService.upload()` API

## Implementation Specification

### Step 1: Create ArtifactMetadata record

Create `services/api-service/src/main/java/com/persistentagent/api/model/ArtifactMetadata.java`:

```java
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
```

### Step 2: Create ArtifactRepository

Create `services/api-service/src/main/java/com/persistentagent/api/repository/ArtifactRepository.java`:

```java
package com.persistentagent.api.repository;

import com.persistentagent.api.model.ArtifactMetadata;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

import java.sql.ResultSet;
import java.sql.SQLException;
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

    /**
     * Inserts a new artifact metadata record.
     */
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

    /**
     * Finds all artifacts for a task, scoped to tenant.
     * Optionally filters by direction.
     */
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

    /**
     * Finds a specific artifact by task, filename, and direction, scoped to tenant.
     * Returns the s3_key for file download.
     */
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

    /**
     * Artifact metadata bundled with the S3 key for download operations.
     */
    public record ArtifactWithS3Key(ArtifactMetadata metadata, String s3Key) {}
}
```

### Step 3: Create S3StorageService

Create `services/api-service/src/main/java/com/persistentagent/api/service/S3StorageService.java`:

```java
package com.persistentagent.api.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.core.ResponseInputStream;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.model.GetObjectResponse;
import software.amazon.awssdk.services.s3.model.NoSuchKeyException;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;

import jakarta.annotation.PostConstruct;
import java.net.URI;
import java.util.Optional;

@Service
public class S3StorageService {

    private static final Logger logger = LoggerFactory.getLogger(S3StorageService.class);

    @Value("${s3.endpoint-url:#{null}}")
    private String endpointUrl;

    @Value("${s3.bucket-name:platform-artifacts}")
    private String bucketName;

    @Value("${s3.region:us-east-1}")
    private String region;

    private S3Client s3Client;

    @PostConstruct
    void init() {
        var builder = S3Client.builder()
                .region(Region.of(region))
                .credentialsProvider(DefaultCredentialsProvider.create());

        if (endpointUrl != null && !endpointUrl.isBlank()) {
            builder.endpointOverride(URI.create(endpointUrl))
                   .forcePathStyle(true);
        }

        this.s3Client = builder.build();
        logger.info("S3StorageService initialized: bucket={}, endpoint={}, region={}",
                bucketName, endpointUrl != null ? endpointUrl : "default (AWS)", region);
    }

    /**
     * Downloads an object from S3 and returns a stream with response metadata.
     *
     * @param s3Key the S3 object key
     * @return Optional containing the response stream, or empty if key not found
     */
    public Optional<ResponseInputStream<GetObjectResponse>> download(String s3Key) {
        try {
            GetObjectRequest request = GetObjectRequest.builder()
                    .bucket(bucketName)
                    .key(s3Key)
                    .build();

            ResponseInputStream<GetObjectResponse> response = s3Client.getObject(request);
            logger.info("S3 download started: bucket={}, key={}", bucketName, s3Key);
            return Optional.of(response);
        } catch (NoSuchKeyException e) {
            logger.warn("S3 object not found: bucket={}, key={}", bucketName, s3Key);
            return Optional.empty();
        }
    }

    /**
     * Uploads data to S3 with the given key and content type.
     * Used by Track 2 for multipart file upload on task submission.
     *
     * @param s3Key the S3 object key
     * @param data the file content as bytes
     * @param contentType the MIME type of the content
     */
    public void upload(String s3Key, byte[] data, String contentType) {
        PutObjectRequest request = PutObjectRequest.builder()
                .bucket(bucketName)
                .key(s3Key)
                .contentType(contentType)
                .contentLength((long) data.length)
                .build();

        s3Client.putObject(request, software.amazon.awssdk.core.sync.RequestBody.fromBytes(data));
        logger.info("S3 upload completed: bucket={}, key={}, size={}", bucketName, s3Key, data.length);
    }
}
```

### Step 4: Add AWS SDK S3 dependency to build.gradle

Add the AWS SDK BOM and S3 dependency to `services/api-service/build.gradle`:

```gradle
dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-web'
    implementation 'org.springframework.boot:spring-boot-starter-jdbc'
    implementation 'org.springframework.boot:spring-boot-starter-validation'
    implementation 'org.springframework.boot:spring-boot-starter-actuator'
    implementation 'com.fasterxml.jackson.core:jackson-databind'
    implementation 'com.fasterxml.jackson.datatype:jackson-datatype-jsr310'
    implementation 'org.postgresql:postgresql'

    // AWS SDK v2 for S3 artifact storage
    implementation platform('software.amazon.awssdk:bom:2.29.0')
    implementation 'software.amazon.awssdk:s3'

    testImplementation 'org.springframework.boot:spring-boot-starter-test'
    testImplementation 'org.mockito:mockito-core:5.18.0'
    testImplementation 'org.mockito:mockito-junit-jupiter:5.18.0'
    testImplementation 'net.bytebuddy:byte-buddy:1.17.5'
    testRuntimeOnly 'org.postgresql:postgresql'
}
```

### Step 5: Add S3 config to application.yml

Add the S3 configuration section to `services/api-service/src/main/resources/application.yml`:

```yaml
s3:
  endpoint-url: ${S3_ENDPOINT_URL:#{null}}
  bucket-name: ${S3_BUCKET_NAME:platform-artifacts}
  region: ${AWS_REGION:us-east-1}
```

### Step 6: Create ArtifactRepository unit tests

Create `services/api-service/src/test/java/com/persistentagent/api/repository/ArtifactRepositoryTest.java`:

```java
package com.persistentagent.api.repository;

import com.persistentagent.api.model.ArtifactMetadata;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;

import java.time.OffsetDateTime;
import java.util.List;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class ArtifactRepositoryTest {

    @Mock
    private JdbcTemplate jdbcTemplate;

    private ArtifactRepository repository;

    @BeforeEach
    void setUp() {
        repository = new ArtifactRepository(jdbcTemplate);
    }

    @Test
    void insert_returnsGeneratedArtifactId() {
        UUID taskId = UUID.randomUUID();
        UUID expectedArtifactId = UUID.randomUUID();

        when(jdbcTemplate.queryForObject(anyString(), eq(UUID.class),
                any(), any(), any(), any(), any(), any(), any()))
                .thenReturn(expectedArtifactId);

        UUID result = repository.insert(
                taskId, "default", "report.pdf", "output",
                "application/pdf", 1024L, "default/task-1/output/report.pdf");

        assertEquals(expectedArtifactId, result);
        verify(jdbcTemplate).queryForObject(
                contains("INSERT INTO task_artifacts"),
                eq(UUID.class),
                eq(taskId), eq("default"), eq("report.pdf"), eq("output"),
                eq("application/pdf"), eq(1024L), eq("default/task-1/output/report.pdf"));
    }

    @Test
    void findByTaskId_withoutDirectionFilter_returnsAllArtifacts() {
        UUID taskId = UUID.randomUUID();

        when(jdbcTemplate.query(contains("WHERE task_id = ? AND tenant_id = ?"),
                any(RowMapper.class), eq(taskId), eq("default")))
                .thenReturn(List.of());

        List<ArtifactMetadata> result = repository.findByTaskId(taskId, "default", null);

        assertNotNull(result);
        verify(jdbcTemplate).query(
                argThat((String sql) -> sql.contains("WHERE task_id = ?") && !sql.contains("AND direction = ?")),
                any(RowMapper.class), eq(taskId), eq("default"));
    }

    @Test
    void findByTaskId_withDirectionFilter_filtersResults() {
        UUID taskId = UUID.randomUUID();

        when(jdbcTemplate.query(contains("AND direction = ?"),
                any(RowMapper.class), eq(taskId), eq("default"), eq("output")))
                .thenReturn(List.of());

        List<ArtifactMetadata> result = repository.findByTaskId(taskId, "default", "output");

        assertNotNull(result);
        verify(jdbcTemplate).query(
                contains("AND direction = ?"),
                any(RowMapper.class), eq(taskId), eq("default"), eq("output"));
    }

    @Test
    void findByTaskIdAndFilename_returnsEmptyWhenNotFound() {
        UUID taskId = UUID.randomUUID();

        when(jdbcTemplate.query(contains("AND filename = ? AND direction = ?"),
                any(RowMapper.class), eq(taskId), eq("default"), eq("report.pdf"), eq("output")))
                .thenReturn(List.of());

        var result = repository.findByTaskIdAndFilename(
                taskId, "default", "report.pdf", "output");

        assertTrue(result.isEmpty());
    }
}
```

### Step 7: Create S3StorageService unit tests

Create `services/api-service/src/test/java/com/persistentagent/api/service/S3StorageServiceTest.java`:

```java
package com.persistentagent.api.service;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class S3StorageServiceTest {

    @Test
    void serviceClassExists() {
        // Verify the class can be loaded and has the expected structure
        assertDoesNotThrow(() -> Class.forName("com.persistentagent.api.service.S3StorageService"));
    }
}
```

Note: Full integration testing of `S3StorageService` requires a running LocalStack instance and is covered by Task 8 (Integration Tests). The unit test here validates class structure only, since mocking the AWS SDK v2 `S3Client` requires significant setup that provides little value compared to integration tests.

## Acceptance Criteria

- [ ] `ArtifactMetadata.java` record exists with fields: `artifactId`, `taskId`, `filename`, `direction`, `contentType`, `sizeBytes`, `createdAt`
- [ ] `ArtifactRepository.java` exists with `insert()`, `findByTaskId()`, `findByTaskIdAndFilename()` methods
- [ ] `ArtifactRepository.insert()` executes INSERT INTO with RETURNING artifact_id
- [ ] `ArtifactRepository.findByTaskId()` supports optional `direction` filter
- [ ] `ArtifactRepository.findByTaskIdAndFilename()` returns `Optional<ArtifactWithS3Key>` with both metadata and s3_key
- [ ] `S3StorageService.java` exists with `download()` method returning `Optional<ResponseInputStream>` and `upload(String s3Key, byte[] data, String contentType)` method
- [ ] `S3StorageService` uses AWS SDK v2 `S3Client` (not v1)
- [ ] `S3StorageService` supports configurable endpoint URL (nullable for real AWS) with `forcePathStyle(true)` for LocalStack
- [ ] `software.amazon.awssdk:s3` dependency added to `build.gradle` with BOM
- [ ] `s3.endpoint-url`, `s3.bucket-name`, `s3.region` added to `application.yml`
- [ ] All unit tests pass
- [ ] `make test` passes with no regressions

## Testing Requirements

- **Unit tests:** `ArtifactRepositoryTest` — mock `JdbcTemplate` to verify SQL patterns and parameter passing for `insert()`, `findByTaskId()` (with and without direction filter), `findByTaskIdAndFilename()`.
- **Unit tests:** `S3StorageServiceTest` — verify class structure loads correctly.
- **Integration tests:** Full S3 download testing covered by Task 8.
- **Regression tests:** Run `make test` — all existing API tests must still pass.

## Constraints and Guardrails

- Do not use AWS SDK v1 (`com.amazonaws`). Use only AWS SDK v2 (`software.amazon.awssdk`).
- Do not add multipart upload support — not needed for the API side.
- Do not modify `TaskRepository.java` — artifact queries go through `ArtifactRepository`.
- Follow the existing `@Repository` pattern: `JdbcTemplate` injection, raw SQL, no ORM.
- Use `forcePathStyle(true)` when `endpointUrl` is set — required for LocalStack compatibility.

## Assumptions

- Task 1 has been completed and `task_artifacts` table exists in the database.
- Task 2 has been completed and LocalStack is available on `http://localhost:4566`.
- AWS SDK v2 BOM version `2.29.0` is compatible with Spring Boot 3.4.
- The API service uses Spring Boot constructor injection (no `@Autowired` annotation).
- `S3_ENDPOINT_URL` environment variable is set to `http://localhost:4566` during local development.

<!-- AGENT_TASK_END: task-4-api-artifact-repo-and-s3.md -->
