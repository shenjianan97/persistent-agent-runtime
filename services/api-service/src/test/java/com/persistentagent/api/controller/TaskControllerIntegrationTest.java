package com.persistentagent.api.controller;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledIfEnvironmentVariable;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;

import com.fasterxml.jackson.databind.ObjectMapper;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

/**
 * Integration tests that hit the real PostgreSQL database.
 * Requires the persistent-agent-runtime-postgres container running on
 * localhost:55432.
 *
 * Enable by setting environment variable: INTEGRATION_TESTS_ENABLED=true
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("integration")
@EnabledIfEnvironmentVariable(named = "INTEGRATION_TESTS_ENABLED", matches = "true")
class TaskControllerIntegrationTest {

        @Autowired
        private MockMvc mockMvc;

        @Autowired
        private JdbcTemplate jdbcTemplate;

        @Autowired
        private ObjectMapper objectMapper;

        private static final String TEST_AGENT_ID = "integ-test-agent";

        @BeforeEach
        void cleanDb() {
                jdbcTemplate.execute("DELETE FROM task_events");
                jdbcTemplate.execute("DELETE FROM checkpoint_writes");
                jdbcTemplate.execute("DELETE FROM checkpoints");
                // task_attached_memories cascades with tasks (ON DELETE CASCADE),
                // but clear explicitly to make test state obvious. Only drop if
                // the migration has run; otherwise these rows don't exist yet.
                try {
                        jdbcTemplate.execute("DELETE FROM task_attached_memories");
                } catch (Exception ignored) {
                        // Table absent on older schemas — Track 5 Task 1 adds it.
                }
                try {
                        jdbcTemplate.execute("DELETE FROM agent_memory_entries");
                } catch (Exception ignored) {
                        // Table absent on older schemas — Track 5 Task 1 adds it.
                }
                jdbcTemplate.execute("DELETE FROM tasks");
                jdbcTemplate.execute("DELETE FROM agents");
                // Create agent for task submission
                jdbcTemplate.execute("""
                        INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
                        VALUES ('default', 'integ-test-agent', 'Integration Test Agent',
                                '{"system_prompt":"You are a test assistant.","provider":"anthropic","model":"claude-sonnet-4-6","temperature":0.5,"allowed_tools":["web_search","calculator"],"memory":{"enabled":true}}'::jsonb,
                                'active')
                        ON CONFLICT (tenant_id, agent_id) DO NOTHING
                        """);
        }

        @Test
        void fullTaskLifecycle_submitQueryCancelRedrive() throws Exception {
                // 1. Submit a task
                String submitBody = """
                                {
                                  "agent_id": "integ-test-agent",
                                  "input": "What is 2+2?",
                                  "max_retries": 2,
                                  "max_steps": 10,
                                  "task_timeout_seconds": 600
                                }
                                """;

                MvcResult submitResult = mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(submitBody))
                                .andExpect(status().isCreated())
                                .andExpect(jsonPath("$.task_id").exists())
                                .andExpect(jsonPath("$.agent_id").value(TEST_AGENT_ID))
                                .andExpect(jsonPath("$.status").value("queued"))
                                .andReturn();

                String taskId = objectMapper.readTree(submitResult.getResponse().getContentAsString())
                                .get("task_id").asText();

                // 2. Get task status
                mockMvc.perform(get("/v1/tasks/" + taskId))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.task_id").value(taskId))
                                .andExpect(jsonPath("$.status").value("queued"))
                                .andExpect(jsonPath("$.checkpoint_count").value(0))
                                .andExpect(jsonPath("$.total_cost_microdollars").value(0));

                // 3. Get checkpoints (empty)
                mockMvc.perform(get("/v1/tasks/" + taskId + "/checkpoints"))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.checkpoints").isArray())
                                .andExpect(jsonPath("$.checkpoints").isEmpty());

                // 4. Cancel task
                mockMvc.perform(post("/v1/tasks/" + taskId + "/cancel"))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.status").value("dead_letter"))
                                .andExpect(jsonPath("$.dead_letter_reason").value("cancelled_by_user"));

                // 5. Verify status is dead_letter
                mockMvc.perform(get("/v1/tasks/" + taskId))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.status").value("dead_letter"));

                // 6. Verify in dead-letter list
                mockMvc.perform(get("/v1/tasks/dead-letter")
                                .param("agent_id", TEST_AGENT_ID))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.items[0].task_id").value(taskId));

                // 7. Redrive task
                mockMvc.perform(post("/v1/tasks/" + taskId + "/redrive"))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.status").value("queued"));

                // 8. Verify status is queued again
                mockMvc.perform(get("/v1/tasks/" + taskId))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.status").value("queued"));
        }

        @Test
        void submitTask_unknownAgent_returns404() throws Exception {
                String body = """
                                {
                                  "agent_id": "nonexistent-agent",
                                  "input": "test"
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isNotFound());
        }

        @Test
        void submitTask_disabledAgent_returns400() throws Exception {
                jdbcTemplate.execute("""
                        INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
                        VALUES ('default', 'disabled-agent', 'Disabled Agent',
                                '{"system_prompt":"prompt","provider":"anthropic","model":"claude-sonnet-4-6","temperature":0.5,"allowed_tools":[]}'::jsonb,
                                'disabled')
                        ON CONFLICT (tenant_id, agent_id) DO NOTHING
                        """);

                String body = """
                                {
                                  "agent_id": "disabled-agent",
                                  "input": "test"
                                }
                                """;

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest());
        }

        @Test
        void cancelTask_alreadyCancelled_returns409() throws Exception {
                String body = """
                                {
                                  "agent_id": "integ-test-agent",
                                  "input": "test"
                                }
                                """;

                MvcResult result = mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isCreated())
                                .andReturn();

                String taskId = objectMapper.readTree(result.getResponse().getContentAsString())
                                .get("task_id").asText();

                // First cancel succeeds
                mockMvc.perform(post("/v1/tasks/" + taskId + "/cancel"))
                                .andExpect(status().isOk());

                // Second cancel fails (already dead_letter)
                mockMvc.perform(post("/v1/tasks/" + taskId + "/cancel"))
                                .andExpect(status().isConflict());
        }

        @Test
        void redriveTask_notDeadLetter_returns409() throws Exception {
                String body = """
                                {
                                  "agent_id": "integ-test-agent",
                                  "input": "test"
                                }
                                """;

                MvcResult result = mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isCreated())
                                .andReturn();

                String taskId = objectMapper.readTree(result.getResponse().getContentAsString())
                                .get("task_id").asText();

                // Try to redrive a queued task
                mockMvc.perform(post("/v1/tasks/" + taskId + "/redrive"))
                                .andExpect(status().isConflict());
        }

        @Test
        void getTaskStatus_nonExistentTask_returns404() throws Exception {
                mockMvc.perform(get("/v1/tasks/" + java.util.UUID.randomUUID()))
                                .andExpect(status().isNotFound());
        }

        @Test
        void healthEndpoint_returns200() throws Exception {
                mockMvc.perform(get("/v1/health"))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.status").value("healthy"))
                                .andExpect(jsonPath("$.database").value("connected"));
        }

        // --- Track 5: attached_memory_ids + memory_mode ---

        @Test
        void submitTask_withValidAttachedMemoryIds_persistsJoinRowsInOrder() throws Exception {
                // Seed three memory entries owned by the test agent
                java.util.UUID m1 = seedMemoryEntry(TEST_AGENT_ID, "Solved login bug");
                java.util.UUID m2 = seedMemoryEntry(TEST_AGENT_ID, "Optimized query path");
                java.util.UUID m3 = seedMemoryEntry(TEST_AGENT_ID, "Fixed flaky test");

                String body = String.format("""
                                {
                                  "agent_id": "%s",
                                  "input": "do more work",
                                  "attached_memory_ids": ["%s", "%s", "%s"]
                                }
                                """, TEST_AGENT_ID, m1, m2, m3);

                MvcResult submitResult = mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isCreated())
                                .andExpect(jsonPath("$.attached_memory_ids.length()").value(3))
                                .andReturn();

                String taskId = objectMapper.readTree(submitResult.getResponse().getContentAsString())
                                .get("task_id").asText();

                // Three rows in task_attached_memories, position preserved
                java.util.List<java.util.Map<String, Object>> rows = jdbcTemplate.queryForList(
                                "SELECT memory_id, position FROM task_attached_memories"
                                                + " WHERE task_id = ?::uuid ORDER BY position ASC",
                                taskId);
                org.junit.jupiter.api.Assertions.assertEquals(3, rows.size());
                org.junit.jupiter.api.Assertions.assertEquals(m1, rows.get(0).get("memory_id"));
                org.junit.jupiter.api.Assertions.assertEquals(0, rows.get(0).get("position"));
                org.junit.jupiter.api.Assertions.assertEquals(m2, rows.get(1).get("memory_id"));
                org.junit.jupiter.api.Assertions.assertEquals(1, rows.get(1).get("position"));
                org.junit.jupiter.api.Assertions.assertEquals(m3, rows.get(2).get("memory_id"));
                org.junit.jupiter.api.Assertions.assertEquals(2, rows.get(2).get("position"));

                // Event details JSONB mirrors the list
                String details = (String) jdbcTemplate.queryForObject(
                                "SELECT details::text FROM task_events"
                                                + " WHERE task_id = ?::uuid AND event_type = 'task_submitted'",
                                String.class, taskId);
                org.junit.jupiter.api.Assertions.assertTrue(details.contains("attached_memory_ids"));
                org.junit.jupiter.api.Assertions.assertTrue(details.contains(m1.toString()));
                org.junit.jupiter.api.Assertions.assertTrue(details.contains(m2.toString()));
                org.junit.jupiter.api.Assertions.assertTrue(details.contains(m3.toString()));

                // Detail GET exposes the full id list + preview
                mockMvc.perform(get("/v1/tasks/" + taskId))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.attached_memory_ids.length()").value(3))
                                .andExpect(jsonPath("$.attached_memory_ids[0]").value(m1.toString()))
                                .andExpect(jsonPath("$.attached_memories_preview.length()").value(3))
                                .andExpect(jsonPath("$.attached_memories_preview[0].memory_id").value(m1.toString()));
        }

        @Test
        void submitTask_withAttachedMemoryFromOtherAgent_returnsUniform400() throws Exception {
                // Create a second agent and seed a memory entry against it
                jdbcTemplate.execute("""
                        INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
                        VALUES ('default', 'other-agent', 'Other Agent',
                                '{"system_prompt":"p","provider":"anthropic","model":"claude-sonnet-4-6","temperature":0.5,"allowed_tools":[]}'::jsonb,
                                'active')
                        ON CONFLICT (tenant_id, agent_id) DO NOTHING
                        """);
                java.util.UUID otherAgentMemory = seedMemoryEntry("other-agent", "Belongs elsewhere");

                String body = String.format("""
                                {
                                  "agent_id": "%s",
                                  "input": "do work",
                                  "attached_memory_ids": ["%s"]
                                }
                                """, TEST_AGENT_ID, otherAgentMemory);

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest())
                                .andExpect(jsonPath("$.message").value(
                                                "one or more attached_memory_ids could not be resolved"));

                // No task, no attached-memory rows, no task_submitted event
                Integer taskCount = jdbcTemplate.queryForObject(
                                "SELECT COUNT(*) FROM tasks WHERE agent_id = ?", Integer.class, TEST_AGENT_ID);
                org.junit.jupiter.api.Assertions.assertEquals(0, taskCount.intValue());
                Integer attachmentCount = jdbcTemplate.queryForObject(
                                "SELECT COUNT(*) FROM task_attached_memories", Integer.class);
                org.junit.jupiter.api.Assertions.assertEquals(0, attachmentCount.intValue());
        }

        @Test
        void submitTask_withUnknownAttachedMemoryId_returnsUniform400() throws Exception {
                String body = String.format("""
                                {
                                  "agent_id": "%s",
                                  "input": "do work",
                                  "attached_memory_ids": ["%s"]
                                }
                                """, TEST_AGENT_ID, java.util.UUID.randomUUID());

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest())
                                .andExpect(jsonPath("$.message").value(
                                                "one or more attached_memory_ids could not be resolved"));
        }

        @Test
        void submitTask_withMemoryModeSkip_persistsOnTaskRow() throws Exception {
                String body = String.format("""
                                {
                                  "agent_id": "%s",
                                  "input": "sensitive work",
                                  "memory_mode": "skip"
                                }
                                """, TEST_AGENT_ID);

                MvcResult submitResult = mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isCreated())
                                .andReturn();
                String taskId = objectMapper.readTree(submitResult.getResponse().getContentAsString())
                                .get("task_id").asText();

                String mode = jdbcTemplate.queryForObject(
                                "SELECT memory_mode FROM tasks WHERE task_id = ?::uuid",
                                String.class, taskId);
                org.junit.jupiter.api.Assertions.assertEquals("skip", mode);

                // Task detail response surfaces the mode
                mockMvc.perform(get("/v1/tasks/" + taskId))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.memory_mode").value("skip"));
        }

        @Test
        void submitTask_memoryModeAbsent_defaultsAlways() throws Exception {
                String body = String.format("""
                                {
                                  "agent_id": "%s",
                                  "input": "plain work"
                                }
                                """, TEST_AGENT_ID);

                MvcResult submitResult = mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isCreated())
                                .andReturn();
                String taskId = objectMapper.readTree(submitResult.getResponse().getContentAsString())
                                .get("task_id").asText();

                String mode = jdbcTemplate.queryForObject(
                                "SELECT memory_mode FROM tasks WHERE task_id = ?::uuid",
                                String.class, taskId);
                org.junit.jupiter.api.Assertions.assertEquals("always", mode);
        }

        @Test
        void submitTask_withMemoryModeAgentDecides_persistsOnTaskRow() throws Exception {
                String body = String.format("""
                                {
                                  "agent_id": "%s",
                                  "input": "maybe remember",
                                  "memory_mode": "agent_decides"
                                }
                                """, TEST_AGENT_ID);

                MvcResult submitResult = mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isCreated())
                                .andReturn();
                String taskId = objectMapper.readTree(submitResult.getResponse().getContentAsString())
                                .get("task_id").asText();

                String mode = jdbcTemplate.queryForObject(
                                "SELECT memory_mode FROM tasks WHERE task_id = ?::uuid",
                                String.class, taskId);
                org.junit.jupiter.api.Assertions.assertEquals("agent_decides", mode);
        }

        @Test
        void submitTask_withInvalidMemoryMode_returns400() throws Exception {
                String body = String.format("""
                                {
                                  "agent_id": "%s",
                                  "input": "bad mode",
                                  "memory_mode": "maybe"
                                }
                                """, TEST_AGENT_ID);

                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isBadRequest());
        }

        @Test
        void submitTask_withAlwaysForMemoryDisabledAgent_returns400() throws Exception {
                // Create an agent with memory explicitly disabled; the cross-field
                // invariant must reject non-skip modes.
                jdbcTemplate.execute("""
                        INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
                        VALUES ('default', 'memory-off-agent', 'Memory Off Agent',
                                '{"system_prompt":"p","provider":"anthropic","model":"claude-sonnet-4-6","temperature":0.5,"allowed_tools":[],"memory":{"enabled":false}}'::jsonb,
                                'active')
                        ON CONFLICT (tenant_id, agent_id) DO NOTHING
                        """);

                for (String mode : new String[]{"always", "agent_decides"}) {
                        String body = String.format("""
                                        {
                                          "agent_id": "memory-off-agent",
                                          "input": "try to enable memory",
                                          "memory_mode": "%s"
                                        }
                                        """, mode);
                        mockMvc.perform(post("/v1/tasks")
                                        .contentType(MediaType.APPLICATION_JSON_VALUE)
                                        .content(body))
                                        .andExpect(status().isBadRequest())
                                        .andExpect(jsonPath("$.message").value(
                                                        org.hamcrest.Matchers.containsString("memory_mode")));
                }

                // skip mode is still accepted for memory-disabled agents
                String skipBody = """
                                {
                                  "agent_id": "memory-off-agent",
                                  "input": "skip still works",
                                  "memory_mode": "skip"
                                }
                                """;
                mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(skipBody))
                                .andExpect(status().isCreated());
        }

        @Test
        void submitTask_deletedMemoryEntryDropsFromPreviewButKeepsIdList() throws Exception {
                java.util.UUID keep = seedMemoryEntry(TEST_AGENT_ID, "Keep me");
                java.util.UUID doomed = seedMemoryEntry(TEST_AGENT_ID, "Will be deleted");

                String body = String.format("""
                                {
                                  "agent_id": "%s",
                                  "input": "work",
                                  "attached_memory_ids": ["%s", "%s"]
                                }
                                """, TEST_AGENT_ID, keep, doomed);

                MvcResult submitResult = mockMvc.perform(post("/v1/tasks")
                                .contentType(MediaType.APPLICATION_JSON_VALUE)
                                .content(body))
                                .andExpect(status().isCreated())
                                .andReturn();
                String taskId = objectMapper.readTree(submitResult.getResponse().getContentAsString())
                                .get("task_id").asText();

                // Simulate Task 3's DELETE — remove the memory entry
                jdbcTemplate.update("DELETE FROM agent_memory_entries WHERE memory_id = ?", doomed);

                // Attachment rows remain intact (soft reference, no cascade)
                Integer attachmentCount = jdbcTemplate.queryForObject(
                                "SELECT COUNT(*) FROM task_attached_memories WHERE task_id = ?::uuid",
                                Integer.class, taskId);
                org.junit.jupiter.api.Assertions.assertEquals(2, attachmentCount.intValue());

                // Full id list still includes both; preview drops the deleted entry
                mockMvc.perform(get("/v1/tasks/" + taskId))
                                .andExpect(status().isOk())
                                .andExpect(jsonPath("$.attached_memory_ids.length()").value(2))
                                .andExpect(jsonPath("$.attached_memories_preview.length()").value(1))
                                .andExpect(jsonPath("$.attached_memories_preview[0].memory_id").value(keep.toString()));
        }

        /**
         * Inserts a minimal memory row and returns its generated {@code memory_id}.
         * {@code content_vec} is left null — the deferred-embedding path.
         */
        private java.util.UUID seedMemoryEntry(String agentId, String title) {
                return jdbcTemplate.queryForObject("""
                        INSERT INTO agent_memory_entries (
                            tenant_id, agent_id, task_id, title, summary,
                            observations, outcome, tags,
                            summarizer_model_id, version
                        )
                        VALUES (
                            'default', ?, gen_random_uuid(), ?, 'test summary',
                            '{}', 'succeeded', '{}',
                            'template:fallback', 1
                        )
                        RETURNING memory_id
                        """, java.util.UUID.class, agentId, title);
        }
}
