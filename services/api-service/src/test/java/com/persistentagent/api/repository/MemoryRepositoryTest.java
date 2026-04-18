package com.persistentagent.api.repository;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.jdbc.core.JdbcTemplate;

import java.time.OffsetDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * Tests that the memory repository builds queries with {@code tenant_id}
 * and {@code agent_id} predicates on every path (the memory-query invariant
 * from the design doc). Also verifies the hybrid search SQL mentions RRF and
 * {@code websearch_to_tsquery}.
 */
@ExtendWith(MockitoExtension.class)
class MemoryRepositoryTest {

    @Mock
    private JdbcTemplate jdbcTemplate;

    private MemoryRepository repository;

    @BeforeEach
    void setUp() {
        repository = new MemoryRepository(jdbcTemplate);
    }

    @Test
    void requireScope_rejectsMissingTenantId() {
        assertThatThrownBy(() -> repository.findById(null, "agent-1", "a"))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void requireScope_rejectsMissingAgentId() {
        assertThatThrownBy(() -> repository.list("tenant", null, null, null, null, null, null, 10))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void list_sqlIncludesTenantAndAgentPredicates_andOrderByCreatedAt() {
        when(jdbcTemplate.queryForList(anyString(), any(Object[].class)))
                .thenReturn(List.of());

        repository.list("tenant", "agent", null, null, null, null, null, 25);

        String sql = capturedSql();
        assertThat(sql).contains("WHERE tenant_id = ?");
        assertThat(sql).contains("AND agent_id = ?");
        assertThat(sql).contains("ORDER BY created_at DESC, memory_id DESC");
        assertThat(sql).contains("LIMIT ?");
    }

    @Test
    void list_cursorPredicateUsesCompositeTuple() {
        when(jdbcTemplate.queryForList(anyString(), any(Object[].class)))
                .thenReturn(List.of());

        repository.list("tenant", "agent", null, null, null,
                OffsetDateTime.now(), "aaa-bbb", 25);

        String sql = capturedSql();
        assertThat(sql).contains("(created_at, memory_id) <");
    }

    @Test
    void findById_filtersByTenantAndAgent() {
        when(jdbcTemplate.queryForList(anyString(),
                eq("tenant"), eq("agent"), eq("memory-id")))
                .thenReturn(List.of());
        repository.findById("tenant", "agent", "memory-id");

        String sql = capturedSql();
        assertThat(sql).contains("WHERE tenant_id = ? AND agent_id = ?");
        assertThat(sql).contains("memory_id = ?::uuid");
    }

    @Test
    void delete_filtersByTenantAndAgent() {
        when(jdbcTemplate.update(anyString(), eq("tenant"), eq("agent"), eq("mid")))
                .thenReturn(1);
        boolean removed = repository.delete("tenant", "agent", "mid");

        assertThat(removed).isTrue();
        ArgumentCaptor<String> sqlCaptor = ArgumentCaptor.forClass(String.class);
        verify(jdbcTemplate).update(sqlCaptor.capture(), eq("tenant"), eq("agent"), eq("mid"));
        assertThat(sqlCaptor.getValue()).contains("WHERE tenant_id = ? AND agent_id = ?");
    }

    @Test
    void searchText_usesWebsearchToTsqueryNeverRawToTsquery() {
        when(jdbcTemplate.queryForList(anyString(), any(Object[].class)))
                .thenReturn(List.of());
        repository.searchText("tenant", "agent", "cats & dogs", null, null, null, 5);

        String sql = capturedSql();
        assertThat(sql).contains("websearch_to_tsquery('english', ?)");
        // Guard against a naive regression to raw to_tsquery:
        assertThat(sql).doesNotContain("plainto_tsquery");
        // The raw form must never be inlined against user input — the surrounding
        // context must always be websearch_to_tsquery.
        assertThat(sql.replace("websearch_to_tsquery", "")).doesNotContain("to_tsquery");
    }

    @Test
    void searchVector_castsVectorLiteralAndFiltersNullVec() {
        when(jdbcTemplate.queryForList(anyString(), any(Object[].class)))
                .thenReturn(List.of());
        repository.searchVector("tenant", "agent", new float[]{0.1f, 0.2f},
                null, null, null, 5);
        String sql = capturedSql();
        assertThat(sql).contains("?::vector");
        assertThat(sql).contains("content_vec IS NOT NULL");
        assertThat(sql).contains("tenant_id = ?");
        assertThat(sql).contains("agent_id = ?");
    }

    @Test
    void searchHybrid_usesRRFWithCandidateMultiplierAndK60() {
        when(jdbcTemplate.queryForList(anyString(), any(Object[].class)))
                .thenReturn(List.of());
        repository.searchHybrid("tenant", "agent", "cats",
                new float[]{0.1f}, null, null, null, 40, 10, 60);

        String sql = capturedSql();
        // CTE shape
        assertThat(sql).contains("WITH q AS");
        assertThat(sql).contains("scoped AS");
        assertThat(sql).contains("bm25 AS");
        assertThat(sql).contains("vec AS");
        // RRF fusion expression
        assertThat(sql).contains("1.0 / (? + bm25.r)");
        assertThat(sql).contains("1.0 / (? + vec.r)");
        // Tiebreak + scope
        assertThat(sql).contains("ORDER BY rrf_score DESC");
        assertThat(sql).contains("tenant_id = ? AND agent_id = ?");
    }

    @Test
    void approxBytesForAgent_aggregatesPgColumnSize() {
        when(jdbcTemplate.queryForObject(anyString(), eq(Long.class), eq("tenant"), eq("agent")))
                .thenReturn(9_999L);
        long bytes = repository.approxBytesForAgent("tenant", "agent");
        assertThat(bytes).isEqualTo(9_999L);
    }

    @Test
    void toVectorLiteral_usesBracketsAndRootLocale() {
        String s = MemoryRepository.toVectorLiteral(new float[]{0.1f, 0.2f, 0.3f});
        assertThat(s).startsWith("[").endsWith("]");
        assertThat(s).contains("0.1");
        assertThat(s).contains("0.2");
        assertThat(s).contains("0.3");
        // No comma-decimals (non-ROOT locales would emit "0,1").
        assertThat(s).doesNotContain(",0,");
    }

    // --- helpers ---

    private String capturedSql() {
        ArgumentCaptor<String> sqlCaptor = ArgumentCaptor.forClass(String.class);
        verify(jdbcTemplate, org.mockito.Mockito.atLeastOnce())
                .queryForList(sqlCaptor.capture(), any(Object[].class));
        return sqlCaptor.getValue();
    }
}
