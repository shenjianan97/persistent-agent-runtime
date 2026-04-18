package com.persistentagent.api.controller;

import com.persistentagent.api.exception.EmbeddingProviderUnavailableException;
import com.persistentagent.api.exception.MemoryNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.response.MemoryEntryResponse;
import com.persistentagent.api.model.response.MemoryEntrySummary;
import com.persistentagent.api.model.response.MemoryListResponse;
import com.persistentagent.api.model.response.MemorySearchResponse;
import com.persistentagent.api.model.response.MemoryStorageStats;
import com.persistentagent.api.service.MemoryService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.doNothing;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@WebMvcTest(MemoryController.class)
class MemoryControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @MockitoBean
    private MemoryService memoryService;

    private static final String AGENT = "agent-1";
    private static final String MEMORY_ID = UUID.randomUUID().toString();
    private static final OffsetDateTime NOW = OffsetDateTime.now(ZoneOffset.UTC);

    // ----- list -----

    @Test
    void list_returnsItemsAndStorageStats() throws Exception {
        MemoryEntrySummary item = MemoryEntrySummary.listItem(
                MEMORY_ID, "title", "succeeded", UUID.randomUUID().toString(), NOW);
        MemoryListResponse response = new MemoryListResponse(
                List.of(item), null, new MemoryStorageStats(5L, 123L));
        when(memoryService.list(eq(AGENT), isNull(), isNull(), isNull(), isNull(), isNull()))
                .thenReturn(response);

        mockMvc.perform(get("/v1/agents/" + AGENT + "/memory"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.items[0].memory_id").value(MEMORY_ID))
                .andExpect(jsonPath("$.items[0].outcome").value("succeeded"))
                .andExpect(jsonPath("$.agent_storage_stats.entry_count").value(5))
                .andExpect(jsonPath("$.agent_storage_stats.approx_bytes").value(123));
    }

    @Test
    void list_supportsOutcomeAndDateFilters() throws Exception {
        MemoryListResponse response = new MemoryListResponse(List.of(), null, new MemoryStorageStats(0, 0));
        when(memoryService.list(eq(AGENT), eq("succeeded"), any(), any(), any(), isNull()))
                .thenReturn(response);

        mockMvc.perform(get("/v1/agents/" + AGENT + "/memory")
                        .param("outcome", "succeeded")
                        .param("from", "2026-04-17T00:00:00Z")
                        .param("to", "2026-04-18T00:00:00Z"))
                .andExpect(status().isOk());
    }

    @Test
    void list_unknownAgent_returns404() throws Exception {
        when(memoryService.list(any(), any(), any(), any(), any(), any()))
                .thenThrow(new MemoryNotFoundException());

        mockMvc.perform(get("/v1/agents/unknown/memory"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value(MemoryNotFoundException.UNIFORM_MESSAGE));
    }

    @Test
    void list_badLimit_returnsBadRequest() throws Exception {
        when(memoryService.list(any(), any(), any(), any(), any(), any()))
                .thenThrow(new ValidationException("limit must be <= 200"));
        mockMvc.perform(get("/v1/agents/" + AGENT + "/memory").param("limit", "5000"))
                .andExpect(status().isBadRequest());
    }

    @Test
    void list_fromAfterTo_returnsBadRequest() throws Exception {
        mockMvc.perform(get("/v1/agents/" + AGENT + "/memory")
                        .param("from", "2026-04-18T00:00:00Z")
                        .param("to", "2026-04-17T00:00:00Z"))
                .andExpect(status().isBadRequest());
    }

    // ----- search -----

    @Test
    void search_defaultsToHybrid() throws Exception {
        MemoryEntrySummary result = new MemoryEntrySummary(
                MEMORY_ID, "t", "succeeded", UUID.randomUUID().toString(), NOW, "preview", 0.05);
        MemorySearchResponse response = new MemorySearchResponse(List.of(result), "hybrid");
        when(memoryService.search(eq(AGENT), eq("cats"), isNull(), isNull(), isNull(), isNull(), isNull()))
                .thenReturn(response);

        mockMvc.perform(get("/v1/agents/" + AGENT + "/memory/search").param("q", "cats"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.ranking_used").value("hybrid"))
                .andExpect(jsonPath("$.results[0].summary_preview").value("preview"))
                .andExpect(jsonPath("$.results[0].score").value(0.05));
    }

    @Test
    void search_textMode_hitsService() throws Exception {
        MemorySearchResponse response = new MemorySearchResponse(List.of(), "text");
        when(memoryService.search(eq(AGENT), eq("cats"), eq("text"), isNull(), isNull(), isNull(), isNull()))
                .thenReturn(response);

        mockMvc.perform(get("/v1/agents/" + AGENT + "/memory/search")
                        .param("q", "cats").param("mode", "text"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.ranking_used").value("text"));
    }

    @Test
    void search_vector_embeddingDown_returns503() throws Exception {
        when(memoryService.search(eq(AGENT), eq("cats"), eq("vector"), isNull(), isNull(), isNull(), isNull()))
                .thenThrow(new EmbeddingProviderUnavailableException("down"));

        mockMvc.perform(get("/v1/agents/" + AGENT + "/memory/search")
                        .param("q", "cats").param("mode", "vector"))
                .andExpect(status().isServiceUnavailable());
    }

    @Test
    void search_missingQuery_returnsBadRequest() throws Exception {
        mockMvc.perform(get("/v1/agents/" + AGENT + "/memory/search"))
                .andExpect(status().isBadRequest());
    }

    @Test
    void search_unknownAgent_returns404() throws Exception {
        when(memoryService.search(any(), any(), any(), any(), any(), any(), any()))
                .thenThrow(new MemoryNotFoundException());
        mockMvc.perform(get("/v1/agents/unknown/memory/search").param("q", "x"))
                .andExpect(status().isNotFound());
    }

    @Test
    void search_limitTooHigh_returnsBadRequest() throws Exception {
        when(memoryService.search(any(), any(), any(), any(), any(), any(), any()))
                .thenThrow(new ValidationException("limit must be <= 20"));
        mockMvc.perform(get("/v1/agents/" + AGENT + "/memory/search")
                        .param("q", "x").param("limit", "21"))
                .andExpect(status().isBadRequest());
    }

    // ----- detail -----

    @Test
    void get_returnsFullEntry() throws Exception {
        MemoryEntryResponse response = new MemoryEntryResponse(
                MEMORY_ID, AGENT, UUID.randomUUID().toString(),
                "title", "summary body",
                List.of("obs1", "obs2"), "succeeded",
                List.of("tag1"),
                "claude-haiku", 1, NOW, NOW);
        when(memoryService.get(AGENT, MEMORY_ID)).thenReturn(response);

        mockMvc.perform(get("/v1/agents/" + AGENT + "/memory/" + MEMORY_ID))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.memory_id").value(MEMORY_ID))
                .andExpect(jsonPath("$.observations[0]").value("obs1"))
                .andExpect(jsonPath("$.tags[0]").value("tag1"))
                .andExpect(jsonPath("$.summarizer_model_id").value("claude-haiku"));
    }

    @Test
    void get_notFound_returns404() throws Exception {
        when(memoryService.get(AGENT, MEMORY_ID)).thenThrow(new MemoryNotFoundException());
        mockMvc.perform(get("/v1/agents/" + AGENT + "/memory/" + MEMORY_ID))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.message").value(MemoryNotFoundException.UNIFORM_MESSAGE));
    }

    @Test
    void get_malformedUuid_returns404NotBadRequest() throws Exception {
        // 404-not-403: malformed id surfaces as "not found" via the service
        // layer rather than 400. The controller simply forwards to the service.
        when(memoryService.get(AGENT, "not-a-uuid")).thenThrow(new MemoryNotFoundException());
        mockMvc.perform(get("/v1/agents/" + AGENT + "/memory/not-a-uuid"))
                .andExpect(status().isNotFound());
    }

    // ----- delete -----

    @Test
    void delete_success_returns204() throws Exception {
        doNothing().when(memoryService).delete(AGENT, MEMORY_ID);
        mockMvc.perform(delete("/v1/agents/" + AGENT + "/memory/" + MEMORY_ID))
                .andExpect(status().isNoContent());
    }

    @Test
    void delete_notFound_returns404() throws Exception {
        doThrow(new MemoryNotFoundException()).when(memoryService).delete(AGENT, MEMORY_ID);
        mockMvc.perform(delete("/v1/agents/" + AGENT + "/memory/" + MEMORY_ID))
                .andExpect(status().isNotFound());
    }
}
