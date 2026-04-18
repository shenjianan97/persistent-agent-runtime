package com.persistentagent.api.repository;

import com.persistentagent.api.model.response.AttachedMemoryPreview;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;

import java.util.List;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

@ExtendWith(MockitoExtension.class)
class TaskAttachedMemoryRepositoryTest {

    @Mock
    private JdbcTemplate jdbcTemplate;

    private TaskAttachedMemoryRepository repository;

    @BeforeEach
    void setUp() {
        repository = new TaskAttachedMemoryRepository(jdbcTemplate);
    }

    @Test
    void resolveScopedMemoryIds_withEmptyInput_returnsEmptyListWithoutQuerying() {
        List<UUID> resolved = repository.resolveScopedMemoryIds("default", "agent1", List.of());

        assertTrue(resolved.isEmpty());
        verifyNoInteractions(jdbcTemplate);
    }

    @Test
    void resolveScopedMemoryIds_queriesWithTenantAndAgentScope() {
        UUID id1 = UUID.randomUUID();
        UUID id2 = UUID.randomUUID();
        org.mockito.Mockito.doReturn(List.of(id1, id2))
                .when(jdbcTemplate)
                .queryForList(anyString(), eq(UUID.class), any(Object[].class));

        List<UUID> resolved = repository.resolveScopedMemoryIds(
                "default", "agent1", List.of(id1, id2));

        assertEquals(List.of(id1, id2), resolved);
        ArgumentCaptor<String> sqlCaptor = ArgumentCaptor.forClass(String.class);
        ArgumentCaptor<Object[]> argsCaptor = ArgumentCaptor.forClass(Object[].class);
        verify(jdbcTemplate).queryForList(
                sqlCaptor.capture(), eq(UUID.class), argsCaptor.capture());
        String sql = sqlCaptor.getValue();
        assertTrue(sql.contains("agent_memory_entries"));
        assertTrue(sql.contains("memory_id IN"));
        assertTrue(sql.contains("tenant_id"));
        assertTrue(sql.contains("agent_id"));
        Object[] args = argsCaptor.getValue();
        // 2 memory ids (as strings) + tenant + agent
        assertEquals(4, args.length);
        assertEquals(id1.toString(), args[0]);
        assertEquals(id2.toString(), args[1]);
        assertEquals("default", args[2]);
        assertEquals("agent1", args[3]);
    }

    @Test
    void insertAttachments_writesOneRowPerIdWithPosition() throws Exception {
        UUID taskId = UUID.randomUUID();
        UUID m1 = UUID.randomUUID();
        UUID m2 = UUID.randomUUID();
        UUID m3 = UUID.randomUUID();

        when(jdbcTemplate.batchUpdate(anyString(), any(List.class))).thenReturn(new int[]{1, 1, 1});

        repository.insertAttachments(taskId, List.of(m1, m2, m3));

        ArgumentCaptor<String> sqlCaptor = ArgumentCaptor.forClass(String.class);
        ArgumentCaptor<List<Object[]>> argsCaptor = ArgumentCaptor.forClass(List.class);
        verify(jdbcTemplate).batchUpdate(sqlCaptor.capture(), argsCaptor.capture());
        assertTrue(sqlCaptor.getValue().contains("INSERT INTO task_attached_memories"));
        List<Object[]> args = argsCaptor.getValue();
        assertEquals(3, args.size());
        // row 0: task_id, memory_id, position=0
        assertEquals(taskId, args.get(0)[0]);
        assertEquals(m1, args.get(0)[1]);
        assertEquals(0, args.get(0)[2]);
        // row 1
        assertEquals(taskId, args.get(1)[0]);
        assertEquals(m2, args.get(1)[1]);
        assertEquals(1, args.get(1)[2]);
        // row 2
        assertEquals(taskId, args.get(2)[0]);
        assertEquals(m3, args.get(2)[1]);
        assertEquals(2, args.get(2)[2]);
    }

    @Test
    void insertAttachments_withEmptyList_skipsQuery() {
        UUID taskId = UUID.randomUUID();

        repository.insertAttachments(taskId, List.of());

        verifyNoInteractions(jdbcTemplate);
    }

    @Test
    void findAttachedMemoryIds_orderByPosition() {
        UUID taskId = UUID.randomUUID();
        UUID m1 = UUID.randomUUID();
        UUID m2 = UUID.randomUUID();
        when(jdbcTemplate.queryForList(anyString(), eq(UUID.class),
                eq("default"), eq("agent1"), eq(taskId)))
                .thenReturn(List.of(m1, m2));

        List<UUID> result = repository.findAttachedMemoryIds(taskId, "default", "agent1");

        assertEquals(List.of(m1, m2), result);
        ArgumentCaptor<String> sqlCaptor = ArgumentCaptor.forClass(String.class);
        verify(jdbcTemplate).queryForList(sqlCaptor.capture(), eq(UUID.class),
                eq("default"), eq("agent1"), eq(taskId));
        String sql = sqlCaptor.getValue();
        assertTrue(sql.contains("task_attached_memories"));
        assertTrue(sql.contains("ORDER BY tam.position"));
        assertTrue(sql.contains("t.tenant_id"));
        assertTrue(sql.contains("t.agent_id"));
    }

    @Test
    void findAttachedMemoriesPreview_joinsScopedMemoryEntriesInPositionOrder() {
        UUID taskId = UUID.randomUUID();
        UUID m1 = UUID.randomUUID();
        AttachedMemoryPreview preview = new AttachedMemoryPreview(m1, "Resolved cache bug");

        when(jdbcTemplate.query(anyString(), any(RowMapper.class),
                eq("default"), eq("agent1"), eq(taskId)))
                .thenReturn(List.of(preview));

        List<AttachedMemoryPreview> results = repository.findAttachedMemoriesPreview(
                taskId, "default", "agent1");

        assertEquals(1, results.size());
        assertEquals(m1, results.get(0).memoryId());
        assertEquals("Resolved cache bug", results.get(0).title());
        ArgumentCaptor<String> sqlCaptor = ArgumentCaptor.forClass(String.class);
        verify(jdbcTemplate).query(sqlCaptor.capture(), any(RowMapper.class),
                eq("default"), eq("agent1"), eq(taskId));
        String sql = sqlCaptor.getValue();
        assertTrue(sql.contains("task_attached_memories"));
        assertTrue(sql.contains("agent_memory_entries"));
        assertTrue(sql.contains("tenant_id"));
        assertTrue(sql.contains("agent_id"));
        assertTrue(sql.contains("ORDER BY"));
        assertTrue(sql.toLowerCase().contains("position"));
    }

}
