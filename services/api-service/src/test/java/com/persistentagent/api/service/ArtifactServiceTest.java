package com.persistentagent.api.service;

import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.model.ArtifactMetadata;
import com.persistentagent.api.repository.ArtifactRepository;
import com.persistentagent.api.repository.ArtifactRepository.ArtifactWithS3Key;
import com.persistentagent.api.repository.TaskRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.web.server.ResponseStatusException;
import software.amazon.awssdk.core.ResponseInputStream;
import software.amazon.awssdk.services.s3.model.GetObjectResponse;

import java.io.ByteArrayInputStream;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class ArtifactServiceTest {

    @Mock
    private ArtifactRepository artifactRepository;

    @Mock
    private TaskRepository taskRepository;

    @Mock
    private S3StorageService s3StorageService;

    private ArtifactService artifactService;

    @BeforeEach
    void setUp() {
        artifactService = new ArtifactService(artifactRepository, taskRepository, s3StorageService);
    }

    @Test
    void listArtifacts_taskNotFound_throws404() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default")).thenReturn(Optional.empty());

        assertThrows(TaskNotFoundException.class, () ->
                artifactService.listArtifacts(taskId, null));
    }

    @Test
    void listArtifacts_returnsArtifactsFromRepository() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default"))
                .thenReturn(Optional.of(Map.of("task_id", taskId)));

        ArtifactMetadata artifact = new ArtifactMetadata(
                UUID.randomUUID(), taskId, "report.pdf", "output",
                "application/pdf", 1024L, OffsetDateTime.now());
        when(artifactRepository.findByTaskId(taskId, "default", null))
                .thenReturn(List.of(artifact));

        List<ArtifactMetadata> result = artifactService.listArtifacts(taskId, null);
        assertEquals(1, result.size());
        assertEquals("report.pdf", result.get(0).filename());
    }

    @Test
    void downloadArtifact_taskNotFound_throws404() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default")).thenReturn(Optional.empty());

        assertThrows(TaskNotFoundException.class, () ->
                artifactService.downloadArtifact(taskId, "report.pdf", "output"));
    }

    @Test
    void downloadArtifact_artifactNotFound_throws404() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default"))
                .thenReturn(Optional.of(Map.of("task_id", taskId)));
        when(artifactRepository.findByTaskIdAndFilename(taskId, "default", "report.pdf", "output"))
                .thenReturn(Optional.empty());

        assertThrows(ResponseStatusException.class, () ->
                artifactService.downloadArtifact(taskId, "report.pdf", "output"));
    }

    @Test
    void downloadArtifact_s3KeyNotFound_throws404() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default"))
                .thenReturn(Optional.of(Map.of("task_id", taskId)));

        ArtifactMetadata metadata = new ArtifactMetadata(
                UUID.randomUUID(), taskId, "report.pdf", "output",
                "application/pdf", 1024L, OffsetDateTime.now());
        when(artifactRepository.findByTaskIdAndFilename(taskId, "default", "report.pdf", "output"))
                .thenReturn(Optional.of(new ArtifactWithS3Key(metadata, "default/task-1/output/report.pdf")));
        when(s3StorageService.download("default/task-1/output/report.pdf"))
                .thenReturn(Optional.empty());

        assertThrows(ResponseStatusException.class, () ->
                artifactService.downloadArtifact(taskId, "report.pdf", "output"));
    }

    @Test
    void downloadArtifact_success_returnsDownloadResult() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, "default"))
                .thenReturn(Optional.of(Map.of("task_id", taskId)));

        ArtifactMetadata metadata = new ArtifactMetadata(
                UUID.randomUUID(), taskId, "report.pdf", "output",
                "application/pdf", 1024L, OffsetDateTime.now());
        when(artifactRepository.findByTaskIdAndFilename(taskId, "default", "report.pdf", "output"))
                .thenReturn(Optional.of(new ArtifactWithS3Key(metadata, "default/task-1/output/report.pdf")));

        GetObjectResponse getObjectResponse = GetObjectResponse.builder().build();
        ResponseInputStream<GetObjectResponse> stream = new ResponseInputStream<>(
                getObjectResponse, new ByteArrayInputStream(new byte[0]));
        when(s3StorageService.download("default/task-1/output/report.pdf"))
                .thenReturn(Optional.of(stream));

        ArtifactService.ArtifactDownload result =
                artifactService.downloadArtifact(taskId, "report.pdf", "output");

        assertNotNull(result);
        assertEquals("report.pdf", result.metadata().filename());
        assertNotNull(result.stream());
    }
}
