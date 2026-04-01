package com.persistentagent.api.controller;

import com.persistentagent.api.model.request.AgentCreateRequest;
import com.persistentagent.api.model.request.AgentUpdateRequest;
import com.persistentagent.api.model.response.AgentResponse;
import com.persistentagent.api.model.response.AgentSummaryResponse;
import com.persistentagent.api.service.AgentService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/v1/agents")
public class AgentController {

    private final AgentService agentService;

    public AgentController(AgentService agentService) {
        this.agentService = agentService;
    }

    @PostMapping
    public ResponseEntity<AgentResponse> create(@Valid @RequestBody AgentCreateRequest request) {
        AgentResponse response = agentService.createAgent(request);
        return ResponseEntity.status(HttpStatus.CREATED).body(response);
    }

    @GetMapping
    public ResponseEntity<List<AgentSummaryResponse>> list(
            @RequestParam(required = false) String status,
            @RequestParam(required = false) Integer limit) {
        List<AgentSummaryResponse> response = agentService.listAgents(status, limit);
        return ResponseEntity.ok(response);
    }

    @GetMapping("/{agentId}")
    public ResponseEntity<AgentResponse> get(@PathVariable String agentId) {
        AgentResponse response = agentService.getAgent(agentId);
        return ResponseEntity.ok(response);
    }

    @PutMapping("/{agentId}")
    public ResponseEntity<AgentResponse> update(
            @PathVariable String agentId,
            @Valid @RequestBody AgentUpdateRequest request) {
        AgentResponse response = agentService.updateAgent(agentId, request);
        return ResponseEntity.ok(response);
    }
}
