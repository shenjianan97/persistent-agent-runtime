package com.persistentagent.api.repository;

import com.persistentagent.api.model.ArtifactMetadata;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;

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
