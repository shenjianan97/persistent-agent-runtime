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
                jdbcTemplate.execute("DELETE FROM tasks");
                jdbcTemplate.execute("DELETE FROM agents");
                // Create agent for task submission
                jdbcTemplate.execute("""
                        INSERT INTO agents (tenant_id, agent_id, display_name, agent_config, status)
                        VALUES ('default', 'integ-test-agent', 'Integration Test Agent',
                                '{"system_prompt":"You are a test assistant.","provider":"anthropic","model":"claude-sonnet-4-6","temperature":0.5,"allowed_tools":["web_search","calculator"]}'::jsonb,
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
}
