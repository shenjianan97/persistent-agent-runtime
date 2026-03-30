package com.persistentagent.api.controller;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.model.request.LangfuseEndpointRequest;
import com.persistentagent.api.model.response.LangfuseEndpointResponse;
import com.persistentagent.api.model.response.LangfuseEndpointTestResponse;
import com.persistentagent.api.service.LangfuseEndpointService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.UUID;

@RestController
@RequestMapping("/v1/langfuse-endpoints")
public class LangfuseEndpointController {

    private final LangfuseEndpointService langfuseEndpointService;

    public LangfuseEndpointController(LangfuseEndpointService langfuseEndpointService) {
        this.langfuseEndpointService = langfuseEndpointService;
    }

    @PostMapping
    public ResponseEntity<LangfuseEndpointResponse> create(
            @Valid @RequestBody LangfuseEndpointRequest request) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        LangfuseEndpointResponse response = langfuseEndpointService.create(tenantId, request);
        return ResponseEntity.status(HttpStatus.CREATED).body(response);
    }

    @GetMapping
    public ResponseEntity<List<LangfuseEndpointResponse>> list() {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        List<LangfuseEndpointResponse> response = langfuseEndpointService.list(tenantId);
        return ResponseEntity.ok(response);
    }

    @GetMapping("/{endpointId}")
    public ResponseEntity<LangfuseEndpointResponse> get(@PathVariable UUID endpointId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        LangfuseEndpointResponse response = langfuseEndpointService.get(endpointId, tenantId);
        return ResponseEntity.ok(response);
    }

    @PutMapping("/{endpointId}")
    public ResponseEntity<LangfuseEndpointResponse> update(
            @PathVariable UUID endpointId,
            @Valid @RequestBody LangfuseEndpointRequest request) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        LangfuseEndpointResponse response = langfuseEndpointService.update(endpointId, tenantId, request);
        return ResponseEntity.ok(response);
    }

    @DeleteMapping("/{endpointId}")
    public ResponseEntity<Void> delete(@PathVariable UUID endpointId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        langfuseEndpointService.delete(endpointId, tenantId);
        return ResponseEntity.noContent().build();
    }

    @PostMapping("/{endpointId}/test")
    public ResponseEntity<LangfuseEndpointTestResponse> testConnectivity(@PathVariable UUID endpointId) {
        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;
        LangfuseEndpointTestResponse response = langfuseEndpointService.testConnectivity(endpointId, tenantId);
        return ResponseEntity.ok(response);
    }
}
