package com.persistentagent.api.controller;

import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.request.ToolServerCreateRequest;
import com.persistentagent.api.model.request.ToolServerUpdateRequest;
import com.persistentagent.api.model.response.*;
import com.persistentagent.api.service.ToolServerService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.UUID;

@RestController
@RequestMapping("/v1/tool-servers")
public class ToolServerController {

    private final ToolServerService service;

    public ToolServerController(ToolServerService service) {
        this.service = service;
    }

    @PostMapping
    public ResponseEntity<ToolServerResponse> create(@Valid @RequestBody ToolServerCreateRequest request) {
        ToolServerResponse response = service.createToolServer(request);
        return ResponseEntity.status(HttpStatus.CREATED).body(response);
    }

    @GetMapping
    public ResponseEntity<List<ToolServerSummaryResponse>> list(
            @RequestParam(required = false) String status,
            @RequestParam(required = false) Integer limit) {
        return ResponseEntity.ok(service.listToolServers(status, limit));
    }

    @GetMapping("/{serverId}")
    public ResponseEntity<ToolServerResponse> get(@PathVariable String serverId) {
        validateUuid(serverId);
        return ResponseEntity.ok(service.getToolServer(serverId));
    }

    @PutMapping("/{serverId}")
    public ResponseEntity<ToolServerResponse> update(
            @PathVariable String serverId,
            @Valid @RequestBody ToolServerUpdateRequest request) {
        validateUuid(serverId);
        return ResponseEntity.ok(service.updateToolServer(serverId, request));
    }

    @DeleteMapping("/{serverId}")
    public ResponseEntity<Void> delete(@PathVariable String serverId) {
        validateUuid(serverId);
        service.deleteToolServer(serverId);
        return ResponseEntity.noContent().build();
    }

    @PostMapping("/{serverId}/discover")
    public ResponseEntity<ToolDiscoverResponse> discover(@PathVariable String serverId) {
        validateUuid(serverId);
        return ResponseEntity.ok(service.discoverTools(serverId));
    }

    private void validateUuid(String id) {
        try {
            UUID.fromString(id);
        } catch (IllegalArgumentException e) {
            throw new ValidationException("Invalid server ID format: must be a valid UUID");
        }
    }
}
