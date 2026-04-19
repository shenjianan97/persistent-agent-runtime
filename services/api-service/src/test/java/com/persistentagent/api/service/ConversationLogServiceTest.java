package com.persistentagent.api.service;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.datatype.jsr310.JavaTimeModule;
import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.response.ConversationEntryResponse;
import com.persistentagent.api.repository.ConversationLogRepository;
import com.persistentagent.api.repository.TaskRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class ConversationLogServiceTest {

    @Mock
    private TaskRepository taskRepository;

    @Mock
    private ConversationLogRepository conversationLogRepository;

    private ConversationLogService service;
    private ObjectMapper objectMapper;

    @BeforeEach
    void setUp() {
        objectMapper = new ObjectMapper();
        objectMapper.registerModule(new JavaTimeModule());
        objectMapper.disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);
        service = new ConversationLogService(taskRepository, conversationLogRepository);
    }

    // --- Task existence / tenant-isolation ---

    @Test
    void getConversation_unknownTask_throwsTaskNotFound_anddoesNotQueryLog() {
        UUID taskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(taskId, ValidationConstants.DEFAULT_TENANT_ID))
                .thenReturn(Optional.empty());

        assertThrows(TaskNotFoundException.class,
                () -> service.getConversation(taskId, null, null));

        // Critical: on a 404 we must NOT leak the log contents. The log
        // repository is not queried at all, so even a bug in the SQL could
        // not surface another tenant's rows.
        verifyNoInteractions(conversationLogRepository);
    }

    @Test
    void getConversation_crossTenantTask_returns404_notLeakedEntries() {
        // Simulates tenant A requesting a task_id owned by tenant B.
        // findByIdAndTenant runs with tenantId="default"; tenant B's row
        // doesn't match, so Optional.empty() → 404. Indistinguishable from
        // "task does not exist" to the client.
        UUID foreignTaskId = UUID.randomUUID();
        when(taskRepository.findByIdAndTenant(foreignTaskId, ValidationConstants.DEFAULT_TENANT_ID))
                .thenReturn(Optional.empty());

        TaskNotFoundException ex = assertThrows(TaskNotFoundException.class,
                () -> service.getConversation(foreignTaskId, null, null));
        assertEquals(foreignTaskId, ex.getTaskId());
        verifyNoInteractions(conversationLogRepository);
    }

    // --- Limit validation / clamping ---

    @Test
    void getConversation_rejectsLimitAbove1000() {
        UUID taskId = UUID.randomUUID();
        // No task-existence stub: the limit validation must fire BEFORE any
        // DB work (cheap check). If the stub were reached, Mockito would
        // return null and the test would fail differently — this assertion
        // shape is load-bearing.
        assertThrows(ValidationException.class,
                () -> service.getConversation(taskId, null, 5000));
        verifyNoInteractions(taskRepository);
        verifyNoInteractions(conversationLogRepository);
    }

    @Test
    void getConversation_rejectsZeroLimit() {
        UUID taskId = UUID.randomUUID();
        assertThrows(ValidationException.class,
                () -> service.getConversation(taskId, null, 0));
    }

    @Test
    void getConversation_rejectsNegativeLimit() {
        UUID taskId = UUID.randomUUID();
        assertThrows(ValidationException.class,
                () -> service.getConversation(taskId, null, -1));
    }

    @Test
    void getConversation_defaultLimitIs200() {
        UUID taskId = UUID.randomUUID();
        stubTaskExists(taskId);
        when(conversationLogRepository.findByTask(
                eq(ValidationConstants.DEFAULT_TENANT_ID), eq(taskId), eq(0L), eq(200)))
                .thenReturn(List.of());

        ConversationEntryResponse.Page page = service.getConversation(taskId, null, null);
        assertTrue(page.entries().isEmpty());
        assertNull(page.nextSequence());
        verify(conversationLogRepository).findByTask(
                ValidationConstants.DEFAULT_TENANT_ID, taskId, 0L, 200);
    }

    @Test
    void getConversation_maxLimitExactlyAllowed() {
        UUID taskId = UUID.randomUUID();
        stubTaskExists(taskId);
        when(conversationLogRepository.findByTask(anyString(), eq(taskId), anyLong(), eq(1000)))
                .thenReturn(List.of());

        service.getConversation(taskId, null, 1000);
        verify(conversationLogRepository).findByTask(
                ValidationConstants.DEFAULT_TENANT_ID, taskId, 0L, 1000);
    }

    // --- Pagination ---

    @Test
    void getConversation_partialPage_nextSequenceIsNull() {
        UUID taskId = UUID.randomUUID();
        stubTaskExists(taskId);
        List<ConversationEntryResponse> entries = new ArrayList<>();
        entries.add(sampleEntry(1L, "user_turn", "user"));
        entries.add(sampleEntry(2L, "agent_turn", "assistant"));
        when(conversationLogRepository.findByTask(
                ValidationConstants.DEFAULT_TENANT_ID, taskId, 0L, 200))
                .thenReturn(entries);

        ConversationEntryResponse.Page page = service.getConversation(taskId, null, null);
        assertEquals(2, page.entries().size());
        assertNull(page.nextSequence(),
                "next_sequence must be null when the page is not full");
    }

    @Test
    void getConversation_fullPage_nextSequenceIsLastEntrySequence() {
        UUID taskId = UUID.randomUUID();
        stubTaskExists(taskId);
        int limit = 3;
        List<ConversationEntryResponse> entries = List.of(
                sampleEntry(10L, "user_turn", "user"),
                sampleEntry(11L, "agent_turn", "assistant"),
                sampleEntry(12L, "tool_call", "assistant")
        );
        when(conversationLogRepository.findByTask(
                ValidationConstants.DEFAULT_TENANT_ID, taskId, 0L, limit))
                .thenReturn(entries);

        ConversationEntryResponse.Page page = service.getConversation(taskId, null, limit);
        assertEquals(3, page.entries().size());
        assertEquals(12L, page.nextSequence(),
                "next_sequence must equal max(sequence) when the page is full");
    }

    @Test
    void getConversation_afterSequenceIsForwardedToRepository() {
        UUID taskId = UUID.randomUUID();
        stubTaskExists(taskId);
        when(conversationLogRepository.findByTask(
                ValidationConstants.DEFAULT_TENANT_ID, taskId, 42L, 200))
                .thenReturn(List.of());

        service.getConversation(taskId, 42L, null);
        verify(conversationLogRepository).findByTask(
                ValidationConstants.DEFAULT_TENANT_ID, taskId, 42L, 200);
    }

    @Test
    void getConversation_emptyFullPage_doesNotSetNextSequence() {
        // Defensive: a limit=0 path is rejected by validation above, so an
        // empty result + "full page" can never coincide. Guard against the
        // accidental case anyway — an empty list must yield null.
        UUID taskId = UUID.randomUUID();
        stubTaskExists(taskId);
        when(conversationLogRepository.findByTask(anyString(), eq(taskId), anyLong(), anyInt()))
                .thenReturn(List.of());

        ConversationEntryResponse.Page page = service.getConversation(taskId, null, 1);
        assertTrue(page.entries().isEmpty());
        assertNull(page.nextSequence());
    }

    // --- Jackson round-trip for all 9 kind values ---
    // The content shapes come from Task 13 spec §Content schema.

    @Test
    void jacksonRoundTrip_allNineKindsSerializeAndDeserialize() throws Exception {
        Map<String, Map<String, Object>> fixtures = Map.of(
                "user_turn", Map.of("text", "hello world"),
                "agent_turn", Map.of("text", "response from model"),
                "tool_call", Map.of(
                        "tool_name", "sandbox_read_file",
                        "args", Map.of("path", "/tmp/x"),
                        "call_id", "call_abc"),
                "tool_result", Map.of(
                        "call_id", "call_abc",
                        "tool_name", "sandbox_read_file",
                        "text", "contents",
                        "is_error", false),
                "system_note", Map.of("text", "platform notice"),
                "compaction_boundary", Map.of(
                        "summary_text", "earlier the agent did X",
                        "first_turn_index", 5,
                        "last_turn_index", 22),
                "memory_flush", Map.of(),
                "hitl_pause", Map.of(
                        "reason", "tool_requires_approval",
                        "prompt_to_user", "approve this?"),
                "hitl_resume", Map.of(
                        "resolution", "approved",
                        "user_note", "looks fine")
        );

        long seq = 100L;
        for (Map.Entry<String, Map<String, Object>> fx : fixtures.entrySet()) {
            String kind = fx.getKey();
            Map<String, Object> content = fx.getValue();

            ConversationEntryResponse entry = new ConversationEntryResponse(
                    seq,
                    kind,
                    roleFor(kind),
                    1,
                    objectMapper.valueToTree(content),
                    objectMapper.valueToTree(Map.of()),
                    128,
                    OffsetDateTime.now());

            String json = objectMapper.writeValueAsString(entry);

            // Snake-case field names required by the API contract.
            assertTrue(json.contains("\"sequence\":"), "sequence field missing for kind " + kind);
            assertTrue(json.contains("\"content_version\":"), "content_version missing for " + kind);
            assertTrue(json.contains("\"content_size\":"), "content_size missing for " + kind);
            assertTrue(json.contains("\"created_at\":"), "created_at missing for " + kind);
            assertTrue(json.contains("\"" + kind + "\""), "kind value missing in JSON for " + kind);

            ConversationEntryResponse rt = objectMapper.readValue(json, ConversationEntryResponse.class);
            assertEquals(kind, rt.kind());
            assertEquals(1, rt.contentVersion());
            assertEquals(seq, rt.sequence());
            assertNotNull(rt.content(), "content must round-trip for " + kind);
            assertNotNull(rt.metadata(), "metadata must round-trip for " + kind);

            seq++;
        }
    }

    @Test
    void jacksonRoundTrip_contentVersion2_degradesGracefully() throws Exception {
        // Forward-compat: a schema-v2 entry (e.g. with an unknown field like
        // "branch_id" in content) must still deserialize via the opaque
        // JsonNode type, so older Console clients render a debug fold
        // instead of crashing the API.
        String json = """
                {
                  "sequence": 200,
                  "kind": "agent_turn",
                  "role": "assistant",
                  "content_version": 2,
                  "content": {"text": "hello", "branch_id": "abc-123"},
                  "metadata": {"unknown_future_field": true},
                  "content_size": 42,
                  "created_at": "2026-04-19T12:00:00Z"
                }
                """;

        ConversationEntryResponse entry = objectMapper.readValue(json, ConversationEntryResponse.class);
        assertEquals(2, entry.contentVersion());
        assertEquals("agent_turn", entry.kind());
        assertTrue(entry.content().has("branch_id"),
                "unknown v2 fields must round-trip via opaque JsonNode");
    }

    @Test
    void pageResponse_serializesExpectedJsonShape() throws Exception {
        // Lock the top-level response shape: {"entries": [...], "next_sequence": ...}
        ConversationEntryResponse.Page page = new ConversationEntryResponse.Page(
                List.of(sampleEntry(1L, "user_turn", "user")), 1L);
        String json = objectMapper.writeValueAsString(page);
        assertTrue(json.contains("\"entries\":"), "entries field missing");
        assertTrue(json.contains("\"next_sequence\":1"), "next_sequence must be emitted as snake_case");
    }

    @Test
    void pageResponse_nullNextSequenceIsSerialized() throws Exception {
        // next_sequence=null (partial page) must appear in the JSON — Console
        // uses its presence as the "no more pages" signal.
        ConversationEntryResponse.Page page = new ConversationEntryResponse.Page(
                List.of(), null);
        String json = objectMapper.writeValueAsString(page);
        assertTrue(json.contains("\"next_sequence\":null"),
                "next_sequence: null must serialize explicitly, not be omitted");
    }

    // --- helpers ---

    private void stubTaskExists(UUID taskId) {
        Map<String, Object> row = Map.of(
                "task_id", taskId,
                "tenant_id", ValidationConstants.DEFAULT_TENANT_ID);
        when(taskRepository.findByIdAndTenant(taskId, ValidationConstants.DEFAULT_TENANT_ID))
                .thenReturn(Optional.of(row));
    }

    private ConversationEntryResponse sampleEntry(long sequence, String kind, String role) {
        return new ConversationEntryResponse(
                sequence,
                kind,
                role,
                1,
                objectMapper.valueToTree(Map.of("text", "sample")),
                objectMapper.valueToTree(Map.of()),
                32,
                OffsetDateTime.parse("2026-04-19T12:00:00Z"));
    }

    private String roleFor(String kind) {
        return switch (kind) {
            case "user_turn" -> "user";
            case "agent_turn", "tool_call" -> "assistant";
            case "tool_result" -> "tool";
            default -> "system";
        };
    }
}
