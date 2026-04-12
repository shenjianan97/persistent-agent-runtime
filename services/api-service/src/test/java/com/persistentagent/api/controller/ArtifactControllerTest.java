package com.persistentagent.api.controller;

import com.persistentagent.api.model.ArtifactMetadata;
import com.persistentagent.api.service.ArtifactService;
import com.persistentagent.api.service.ArtifactService.ArtifactDownload;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.http.HttpStatus;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.web.server.ResponseStatusException;
import software.amazon.awssdk.core.ResponseInputStream;
import software.amazon.awssdk.services.s3.model.GetObjectResponse;

import java.io.ByteArrayInputStream;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.UUID;

import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@WebMvcTest(ArtifactController.class)
class ArtifactControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private ArtifactService artifactService;

    @Test
    void listArtifacts_returnsArtifactList() throws Exception {
        UUID taskId = UUID.randomUUID();
        ArtifactMetadata artifact = new ArtifactMetadata(
                UUID.randomUUID(), taskId, "report.pdf", "output",
                "application/pdf", 1024L, OffsetDateTime.now());

        when(artifactService.listArtifacts(eq(taskId), isNull()))
                .thenReturn(List.of(artifact));

        mockMvc.perform(get("/v1/tasks/{taskId}/artifacts", taskId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].filename").value("report.pdf"))
                .andExpect(jsonPath("$[0].direction").value("output"))
                .andExpect(jsonPath("$[0].contentType").value("application/pdf"))
                .andExpect(jsonPath("$[0].sizeBytes").value(1024));
    }

    @Test
    void listArtifacts_withDirectionFilter() throws Exception {
        UUID taskId = UUID.randomUUID();

        when(artifactService.listArtifacts(eq(taskId), eq("output")))
                .thenReturn(List.of());

        mockMvc.perform(get("/v1/tasks/{taskId}/artifacts", taskId)
                        .param("direction", "output"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$").isArray())
                .andExpect(jsonPath("$").isEmpty());
    }

    @Test
    void listArtifacts_taskNotFound_returns404() throws Exception {
        UUID taskId = UUID.randomUUID();

        when(artifactService.listArtifacts(eq(taskId), isNull()))
                .thenThrow(new ResponseStatusException(HttpStatus.NOT_FOUND, "Task not found"));

        mockMvc.perform(get("/v1/tasks/{taskId}/artifacts", taskId))
                .andExpect(status().isNotFound());
    }

    @Test
    void downloadArtifact_streamsFileWithCorrectHeaders() throws Exception {
        UUID taskId = UUID.randomUUID();
        byte[] content = "file content here".getBytes();
        ArtifactMetadata metadata = new ArtifactMetadata(
                UUID.randomUUID(), taskId, "report.pdf", "output",
                "application/pdf", content.length, OffsetDateTime.now());

        GetObjectResponse getObjectResponse = GetObjectResponse.builder()
                .contentType("application/pdf")
                .contentLength((long) content.length)
                .build();
        ResponseInputStream<GetObjectResponse> stream = new ResponseInputStream<>(
                getObjectResponse, new ByteArrayInputStream(content));

        ArtifactDownload download = new ArtifactDownload(metadata, stream);

        when(artifactService.downloadArtifact(eq(taskId), eq("report.pdf"), eq("output")))
                .thenReturn(download);

        mockMvc.perform(get("/v1/tasks/{taskId}/artifacts/{filename}", taskId, "report.pdf"))
                .andExpect(status().isOk())
                .andExpect(header().string("Content-Type", "application/pdf"))
                .andExpect(header().string("Content-Disposition", "attachment; filename=\"report.pdf\""))
                .andExpect(content().bytes(content));
    }

    @Test
    void downloadArtifact_artifactNotFound_returns404() throws Exception {
        UUID taskId = UUID.randomUUID();

        when(artifactService.downloadArtifact(eq(taskId), eq("missing.pdf"), eq("output")))
                .thenThrow(new ResponseStatusException(HttpStatus.NOT_FOUND, "Artifact not found"));

        mockMvc.perform(get("/v1/tasks/{taskId}/artifacts/{filename}", taskId, "missing.pdf"))
                .andExpect(status().isNotFound());
    }
}
