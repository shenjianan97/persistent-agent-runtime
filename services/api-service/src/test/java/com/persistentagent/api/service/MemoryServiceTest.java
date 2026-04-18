package com.persistentagent.api.service;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.EmbeddingProviderUnavailableException;
import com.persistentagent.api.exception.MemoryNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.response.MemoryEntryResponse;
import com.persistentagent.api.model.response.MemoryListResponse;
import com.persistentagent.api.model.response.MemorySearchResponse;
import com.persistentagent.api.repository.MemoryRepository;
import com.persistentagent.api.service.observability.MemoryLogger;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.sql.Timestamp;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class MemoryServiceTest {

    private static final String TENANT = ValidationConstants.DEFAULT_TENANT_ID;
    private static final String AGENT = "agent-1";
    private static final String MEMORY_ID = UUID.randomUUID().toString();
    private static final String OTHER_MEMORY_ID = UUID.randomUUID().toString();
    private static final String MISSING_AGENT = "agent-missing";
    private static final String MALFORMED_UUID = "not-a-uuid";

    private MemoryRepository repository;
    private MemoryEmbeddingClient embeddingClient;
    private MemoryLogger logger;
    private MemoryService service;

    @BeforeEach
    void setUp() {
        repository = mock(MemoryRepository.class);
        embeddingClient = mock(MemoryEmbeddingClient.class);
        logger = mock(MemoryLogger.class);
        service = new MemoryService(repository, embeddingClient, logger);

        // Default: agent exists. Individual tests override for missing-agent checks.
        when(repository.agentExists(TENANT, AGENT)).thenReturn(true);
        when(repository.agentExists(TENANT, MISSING_AGENT)).thenReturn(false);
    }

    // ---------- list ----------

    @Test
    void list_firstPage_returnsItemsPlusStorageStats() {
        Map<String, Object> row = summaryRow(MEMORY_ID, "t1", "succeeded", "summary text");
        when(repository.list(eq(TENANT), eq(AGENT), isNull(), isNull(), isNull(), isNull(), isNull(), anyInt()))
                .thenReturn(List.of(row));
        when(repository.countForAgent(TENANT, AGENT)).thenReturn(42L);
        when(repository.approxBytesForAgent(TENANT, AGENT)).thenReturn(1234L);

        MemoryListResponse response = service.list(AGENT, null, null, null, null, null);

        assertThat(response.items()).hasSize(1);
        assertThat(response.items().get(0).memoryId()).isEqualTo(MEMORY_ID);
        assertThat(response.items().get(0).summaryPreview()).isNull(); // list omits preview
        assertThat(response.items().get(0).score()).isNull();
        assertThat(response.nextCursor()).isNull();
        assertThat(response.agentStorageStats()).isNotNull();
        assertThat(response.agentStorageStats().entryCount()).isEqualTo(42L);
        assertThat(response.agentStorageStats().approxBytes()).isEqualTo(1234L);
    }

    @Test
    void list_populatesNextCursor_whenMorePagesExist() {
        // Ask for limit=1; repository returns 2 rows to signal hasMore.
        OffsetDateTime now = OffsetDateTime.now();
        Map<String, Object> row1 = summaryRow(MEMORY_ID, "t1", "succeeded", "s1");
        row1.put("created_at", Timestamp.from(now.toInstant()));
        Map<String, Object> row2 = summaryRow(OTHER_MEMORY_ID, "t2", "succeeded", "s2");
        row2.put("created_at", Timestamp.from(now.minusSeconds(1).toInstant()));
        when(repository.list(eq(TENANT), eq(AGENT), isNull(), isNull(), isNull(), isNull(), isNull(), eq(2)))
                .thenReturn(List.of(row1, row2));
        when(repository.countForAgent(TENANT, AGENT)).thenReturn(10L);
        when(repository.approxBytesForAgent(TENANT, AGENT)).thenReturn(100L);

        MemoryListResponse response = service.list(AGENT, null, null, null, 1, null);

        assertThat(response.items()).hasSize(1);
        assertThat(response.items().get(0).memoryId()).isEqualTo(MEMORY_ID);
        assertThat(response.nextCursor()).isNotBlank();
    }

    @Test
    void list_followingPage_omitsStorageStats() {
        Map<String, Object> row = summaryRow(MEMORY_ID, "t1", "succeeded", "s");
        when(repository.list(eq(TENANT), eq(AGENT), isNull(), isNull(), isNull(), any(), any(), anyInt()))
                .thenReturn(List.of(row));

        String cursor = MemoryService.encodeCursor(OffsetDateTime.now(), OTHER_MEMORY_ID);
        MemoryListResponse response = service.list(AGENT, null, null, null, null, cursor);

        assertThat(response.items()).hasSize(1);
        assertThat(response.agentStorageStats()).isNull();
        verify(repository, never()).countForAgent(anyString(), anyString());
        verify(repository, never()).approxBytesForAgent(anyString(), anyString());
    }

    @Test
    void list_unknownAgent_throwsMemoryNotFound() {
        assertThatThrownBy(() -> service.list(MISSING_AGENT, null, null, null, null, null))
                .isInstanceOf(MemoryNotFoundException.class);
        verify(repository, never()).list(
                anyString(), anyString(), any(), any(), any(), any(), any(), anyInt());
    }

    @Test
    void list_rejectsInvalidOutcome() {
        assertThatThrownBy(() -> service.list(AGENT, "bogus", null, null, null, null))
                .isInstanceOf(ValidationException.class);
    }

    @Test
    void list_rejectsInvalidCursor() {
        assertThatThrownBy(() -> service.list(AGENT, null, null, null, null, "@@@"))
                .isInstanceOf(ValidationException.class);
    }

    // ---------- detail ----------

    @Test
    void get_returnsFullEntry() {
        Map<String, Object> row = detailRow(MEMORY_ID);
        when(repository.findById(TENANT, AGENT, MEMORY_ID)).thenReturn(Optional.of(row));

        MemoryEntryResponse response = service.get(AGENT, MEMORY_ID);

        assertThat(response.memoryId()).isEqualTo(MEMORY_ID);
        assertThat(response.agentId()).isEqualTo(AGENT);
        assertThat(response.title()).isEqualTo("title");
        assertThat(response.summary()).isEqualTo("summary");
        assertThat(response.observations()).containsExactly("obs1", "obs2");
        assertThat(response.tags()).containsExactly("tag1");
    }

    @Test
    void get_unknownMemoryId_throws404() {
        when(repository.findById(TENANT, AGENT, MEMORY_ID)).thenReturn(Optional.empty());
        assertThatThrownBy(() -> service.get(AGENT, MEMORY_ID))
                .isInstanceOf(MemoryNotFoundException.class);
    }

    @Test
    void get_malformedUuid_returns404_notBadRequest() {
        // 404-not-403: malformed id surfaced as "not found" so callers cannot
        // distinguish it from an unknown id / wrong scope.
        assertThatThrownBy(() -> service.get(AGENT, MALFORMED_UUID))
                .isInstanceOf(MemoryNotFoundException.class);
    }

    @Test
    void get_unknownAgent_throws404() {
        assertThatThrownBy(() -> service.get(MISSING_AGENT, MEMORY_ID))
                .isInstanceOf(MemoryNotFoundException.class);
    }

    // ---------- delete ----------

    @Test
    void delete_returnsSilentlyAndLogs() {
        when(repository.delete(TENANT, AGENT, MEMORY_ID)).thenReturn(true);
        service.delete(AGENT, MEMORY_ID);
        verify(logger).deleteSucceeded(TENANT, AGENT, MEMORY_ID);
    }

    @Test
    void delete_missingRow_throws404() {
        when(repository.delete(TENANT, AGENT, MEMORY_ID)).thenReturn(false);
        assertThatThrownBy(() -> service.delete(AGENT, MEMORY_ID))
                .isInstanceOf(MemoryNotFoundException.class);
        verify(logger, never()).deleteSucceeded(anyString(), anyString(), anyString());
    }

    @Test
    void delete_unknownAgent_throws404() {
        assertThatThrownBy(() -> service.delete(MISSING_AGENT, MEMORY_ID))
                .isInstanceOf(MemoryNotFoundException.class);
    }

    // ---------- search: mode handling ----------

    @Test
    void search_textMode_doesNotCallEmbedding() {
        Map<String, Object> row = summaryRow(MEMORY_ID, "t1", "succeeded", "summary");
        row.put("rank", 0.75);
        when(repository.searchText(eq(TENANT), eq(AGENT), eq("hello"), isNull(), isNull(), isNull(), anyInt()))
                .thenReturn(List.of(row));

        MemorySearchResponse response = service.search(AGENT, "hello", "text", null, null, null, null);

        assertThat(response.rankingUsed()).isEqualTo("text");
        assertThat(response.results()).hasSize(1);
        assertThat(response.results().get(0).summaryPreview()).isEqualTo("summary");
        assertThat(response.results().get(0).score()).isEqualTo(0.75);
        verify(embeddingClient, never()).embedQuery(anyString());
        verify(logger, never()).searchEmbedding(anyString(), anyString(), anyInt(), anyLong());
    }

    @Test
    void search_vectorMode_embeddingDown_returns503() {
        when(embeddingClient.embedQuery("x"))
                .thenThrow(new MemoryEmbeddingClient.EmbeddingUnavailableException("down"));
        assertThatThrownBy(() -> service.search(AGENT, "x", "vector", null, null, null, null))
                .isInstanceOf(EmbeddingProviderUnavailableException.class);
        verify(repository, never()).searchVector(
                anyString(), anyString(), any(), any(), any(), any(), anyInt());
        verify(logger).searchEmbeddingFailed(eq(TENANT), eq(AGENT), anyString(), anyString());
    }

    @Test
    void search_vectorMode_calls_searchVector() {
        float[] vec = new float[1536];
        when(embeddingClient.embedQuery("q")).thenReturn(
                new MemoryEmbeddingClient.EmbeddingResult(vec, 3, 10L, "text-embedding-3-small"));
        Map<String, Object> row = summaryRow(MEMORY_ID, "t1", "succeeded", "s");
        row.put("distance", 0.1);
        when(repository.searchVector(eq(TENANT), eq(AGENT), eq(vec), isNull(), isNull(), isNull(), anyInt()))
                .thenReturn(List.of(row));

        MemorySearchResponse response = service.search(AGENT, "q", "vector", null, null, null, null);

        assertThat(response.rankingUsed()).isEqualTo("vector");
        assertThat(response.results().get(0).score()).isCloseTo(0.9, within(1e-9));
        verify(logger).searchEmbedding(eq(TENANT), eq(AGENT), eq(3), eq(10L));
    }

    @Test
    void search_hybridMode_embeddingDown_degradesToText() {
        when(embeddingClient.embedQuery("q"))
                .thenThrow(new MemoryEmbeddingClient.EmbeddingUnavailableException("down"));
        Map<String, Object> row = summaryRow(MEMORY_ID, "t1", "succeeded", "s");
        row.put("rank", 0.5);
        when(repository.searchText(eq(TENANT), eq(AGENT), eq("q"), isNull(), isNull(), isNull(), anyInt()))
                .thenReturn(List.of(row));

        MemorySearchResponse response = service.search(AGENT, "q", "hybrid", null, null, null, null);

        assertThat(response.rankingUsed()).isEqualTo("text");
        assertThat(response.results()).hasSize(1);
        verify(repository, never()).searchHybrid(
                anyString(), anyString(), anyString(), any(), any(), any(), any(),
                anyInt(), anyInt(), anyInt());
        ArgumentCaptor<String> req = ArgumentCaptor.forClass(String.class);
        ArgumentCaptor<String> used = ArgumentCaptor.forClass(String.class);
        verify(logger).searchServed(eq(TENANT), eq(AGENT), req.capture(), used.capture(),
                anyLong(), eq(1), eq(1));
        assertThat(req.getValue()).isEqualTo("hybrid");
        assertThat(used.getValue()).isEqualTo("text");
    }

    @Test
    void search_hybridMode_success_usesRRFCandidateMultiplier() {
        float[] vec = new float[1536];
        when(embeddingClient.embedQuery("q")).thenReturn(
                new MemoryEmbeddingClient.EmbeddingResult(vec, 2, 5L, "m"));
        Map<String, Object> row = summaryRow(MEMORY_ID, "t1", "succeeded", "s");
        row.put("rrf_score", 0.031);
        int requestedLimit = 5;
        when(repository.searchHybrid(
                eq(TENANT), eq(AGENT), eq("q"), eq(vec),
                isNull(), isNull(), isNull(),
                eq(requestedLimit * MemoryService.CANDIDATE_MULTIPLIER),
                eq(requestedLimit),
                eq(MemoryService.RRF_K)))
                .thenReturn(List.of(row));

        MemorySearchResponse response = service.search(AGENT, "q", "hybrid", requestedLimit,
                null, null, null);

        assertThat(response.rankingUsed()).isEqualTo("hybrid");
        assertThat(response.results()).hasSize(1);
        assertThat(response.results().get(0).score()).isEqualTo(0.031);
    }

    @Test
    void search_rejectsLimitAbove20() {
        assertThatThrownBy(() -> service.search(AGENT, "q", "text", 21, null, null, null))
                .isInstanceOf(ValidationException.class);
    }

    @Test
    void search_rejectsBlankQuery() {
        assertThatThrownBy(() -> service.search(AGENT, "  ", "text", null, null, null, null))
                .isInstanceOf(ValidationException.class);
    }

    @Test
    void search_rejectsUnknownMode() {
        assertThatThrownBy(() -> service.search(AGENT, "q", "bogus", null, null, null, null))
                .isInstanceOf(ValidationException.class);
    }

    @Test
    void search_unknownAgent_throws404() {
        assertThatThrownBy(() -> service.search(MISSING_AGENT, "q", "text", null, null, null, null))
                .isInstanceOf(MemoryNotFoundException.class);
        verify(repository, never()).searchText(
                anyString(), anyString(), anyString(), any(), any(), any(), anyInt());
    }

    // ---------- cursor round-trip ----------

    @Test
    void cursor_encodeDecode_roundTrip() {
        OffsetDateTime when = OffsetDateTime.parse("2026-04-17T12:34:56Z");
        String id = UUID.randomUUID().toString();
        String cursor = MemoryService.encodeCursor(when, id);
        MemoryService.ListCursor decoded = MemoryService.decodeCursor(cursor);
        assertThat(decoded).isNotNull();
        assertThat(decoded.createdAt()).isEqualTo(when);
        assertThat(decoded.memoryId()).isEqualTo(id);
    }

    // ---------- helpers ----------

    private static Map<String, Object> summaryRow(String memoryId, String title, String outcome, String summary) {
        Map<String, Object> row = new LinkedHashMap<>();
        row.put("memory_id", memoryId);
        row.put("title", title);
        row.put("outcome", outcome);
        row.put("task_id", UUID.randomUUID().toString());
        row.put("summary", summary);
        row.put("created_at", Timestamp.from(Instant.now()));
        return row;
    }

    private static Map<String, Object> detailRow(String memoryId) {
        Map<String, Object> row = summaryRow(memoryId, "title", "succeeded", "summary");
        row.put("agent_id", AGENT);
        row.put("observations", new String[]{"obs1", "obs2"});
        row.put("tags", new String[]{"tag1"});
        row.put("summarizer_model_id", "claude-haiku");
        row.put("version", 1);
        row.put("updated_at", Timestamp.from(Instant.now()));
        return row;
    }

    private static org.assertj.core.data.Offset<Double> within(double d) {
        return org.assertj.core.data.Offset.offset(d);
    }
}
