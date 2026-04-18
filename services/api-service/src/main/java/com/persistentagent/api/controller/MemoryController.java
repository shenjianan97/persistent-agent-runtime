package com.persistentagent.api.controller;

import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.response.MemoryEntryResponse;
import com.persistentagent.api.model.response.MemoryListResponse;
import com.persistentagent.api.model.response.MemorySearchResponse;
import com.persistentagent.api.service.MemoryService;
import org.springframework.format.annotation.DateTimeFormat;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.time.OffsetDateTime;

/**
 * Memory REST surface (Phase 2 Track 5 Task 3).
 *
 * <p>All endpoints are scoped to the caller's {@code (tenant_id, agent_id)}.
 * Unknown agent id, agent from another tenant, and memory id in another scope
 * all return the same uniform 404 shape — the 404-not-403 disclosure rule.
 */
@RestController
@RequestMapping("/v1/agents/{agentId}/memory")
public class MemoryController {

    private final MemoryService service;

    public MemoryController(MemoryService service) {
        this.service = service;
    }

    /**
     * Paginated list; first page (cursor absent) includes {@code agent_storage_stats}.
     */
    @GetMapping
    public ResponseEntity<MemoryListResponse> list(
            @PathVariable("agentId") String agentId,
            @RequestParam(name = "outcome", required = false) String outcome,
            @RequestParam(name = "from", required = false)
                @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) OffsetDateTime from,
            @RequestParam(name = "to", required = false)
                @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) OffsetDateTime to,
            @RequestParam(name = "limit", required = false) Integer limit,
            @RequestParam(name = "cursor", required = false) String cursor) {
        validateDateRange(from, to);
        MemoryListResponse response = service.list(agentId, outcome, from, to, limit, cursor);
        return ResponseEntity.ok(response);
    }

    /**
     * Hybrid RRF / text-only / vector-only search over the agent's memory.
     * Hybrid silently degrades to text when embeddings are unavailable;
     * vector returns 503 under the same condition.
     */
    @GetMapping("/search")
    public ResponseEntity<MemorySearchResponse> search(
            @PathVariable("agentId") String agentId,
            @RequestParam(name = "q") String query,
            @RequestParam(name = "mode", required = false) String mode,
            @RequestParam(name = "limit", required = false) Integer limit,
            @RequestParam(name = "outcome", required = false) String outcome,
            @RequestParam(name = "from", required = false)
                @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) OffsetDateTime from,
            @RequestParam(name = "to", required = false)
                @DateTimeFormat(iso = DateTimeFormat.ISO.DATE_TIME) OffsetDateTime to) {
        validateDateRange(from, to);
        MemorySearchResponse response = service.search(agentId, query, mode, limit, outcome, from, to);
        return ResponseEntity.ok(response);
    }

    @GetMapping("/{memoryId}")
    public ResponseEntity<MemoryEntryResponse> get(
            @PathVariable("agentId") String agentId,
            @PathVariable("memoryId") String memoryId) {
        MemoryEntryResponse response = service.get(agentId, memoryId);
        return ResponseEntity.ok(response);
    }

    @DeleteMapping("/{memoryId}")
    public ResponseEntity<Void> delete(
            @PathVariable("agentId") String agentId,
            @PathVariable("memoryId") String memoryId) {
        service.delete(agentId, memoryId);
        return ResponseEntity.noContent().build();
    }

    private static void validateDateRange(OffsetDateTime from, OffsetDateTime to) {
        if (from != null && to != null && from.isAfter(to)) {
            throw new ValidationException("from must be <= to");
        }
    }
}
