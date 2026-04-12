package com.persistentagent.api.controller;

import com.persistentagent.api.model.ArtifactMetadata;
import com.persistentagent.api.service.ArtifactService;
import com.persistentagent.api.service.ArtifactService.ArtifactDownload;
import org.springframework.core.io.InputStreamResource;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.UUID;

@RestController
@RequestMapping("/v1/tasks/{taskId}/artifacts")
public class ArtifactController {

    private final ArtifactService artifactService;

    public ArtifactController(ArtifactService artifactService) {
        this.artifactService = artifactService;
    }

    @GetMapping
    public ResponseEntity<List<ArtifactMetadata>> listArtifacts(
            @PathVariable UUID taskId,
            @RequestParam(name = "direction", required = false) String direction) {
        List<ArtifactMetadata> artifacts = artifactService.listArtifacts(taskId, direction);
        return ResponseEntity.ok(artifacts);
    }

    @GetMapping("/{filename}")
    public ResponseEntity<InputStreamResource> downloadArtifact(
            @PathVariable UUID taskId,
            @PathVariable String filename,
            @RequestParam(name = "direction", defaultValue = "output") String direction) {
        ArtifactDownload download = artifactService.downloadArtifact(taskId, filename, direction);

        ArtifactMetadata metadata = download.metadata();
        InputStreamResource resource = new InputStreamResource(download.stream());

        // Always include charset=utf-8 for text types since upload_artifact encodes as UTF-8
        String contentType = metadata.contentType();
        if (contentType.startsWith("text/") && !contentType.contains("charset")) {
            contentType = contentType + "; charset=utf-8";
        }

        return ResponseEntity.ok()
                .contentType(MediaType.parseMediaType(contentType))
                .contentLength(metadata.sizeBytes())
                .header(HttpHeaders.CONTENT_DISPOSITION,
                        "attachment; filename=\"" + metadata.filename() + "\"")
                .body(resource);
    }
}
