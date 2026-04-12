package com.persistentagent.api.service;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.model.ArtifactMetadata;
import com.persistentagent.api.repository.ArtifactRepository;
import com.persistentagent.api.repository.ArtifactRepository.ArtifactWithS3Key;
import com.persistentagent.api.repository.TaskRepository;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.web.server.ResponseStatusException;
import software.amazon.awssdk.core.ResponseInputStream;
import software.amazon.awssdk.services.s3.model.GetObjectResponse;

import java.util.List;
import java.util.UUID;

@Service
public class ArtifactService {

    private final ArtifactRepository artifactRepository;
    private final TaskRepository taskRepository;
    private final S3StorageService s3StorageService;

    public ArtifactService(ArtifactRepository artifactRepository,
                           TaskRepository taskRepository,
                           S3StorageService s3StorageService) {
        this.artifactRepository = artifactRepository;
        this.taskRepository = taskRepository;
        this.s3StorageService = s3StorageService;
    }

    public List<ArtifactMetadata> listArtifacts(UUID taskId, String direction) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        taskRepository.findByIdAndTenant(taskId, tenantId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));

        return artifactRepository.findByTaskId(taskId, tenantId, direction);
    }

    public ArtifactDownload downloadArtifact(UUID taskId, String filename, String direction) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        taskRepository.findByIdAndTenant(taskId, tenantId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));

        ArtifactWithS3Key artifact = artifactRepository
                .findByTaskIdAndFilename(taskId, tenantId, filename, direction)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND,
                        "Artifact not found: " + filename));

        ResponseInputStream<GetObjectResponse> stream = s3StorageService
                .download(artifact.s3Key())
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND,
                        "Artifact file not found in storage: " + filename));

        return new ArtifactDownload(artifact.metadata(), stream);
    }

    public record ArtifactDownload(
        ArtifactMetadata metadata,
        ResponseInputStream<GetObjectResponse> stream
    ) {}
}
