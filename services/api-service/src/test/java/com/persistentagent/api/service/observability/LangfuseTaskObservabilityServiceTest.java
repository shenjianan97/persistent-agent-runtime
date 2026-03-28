package com.persistentagent.api.service.observability;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.model.response.TaskObservabilityItemResponse;
import com.persistentagent.api.model.response.TaskObservabilityResponse;
import org.junit.jupiter.api.AfterEach;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.*;

class LangfuseTaskObservabilityServiceTest {

    private final ObjectMapper objectMapper = new ObjectMapper();
    private HttpServer server;

    @Test
    void constructorFailsWhenLangfuseIsUnavailableAtStartup() {
        IllegalStateException error = assertThrows(IllegalStateException.class, () -> new LangfuseTaskObservabilityService(
                true,
                "http://127.0.0.1:1",
                "pk-test",
                "sk-test",
                objectMapper
        ));

        assertTrue(error.getMessage().contains("Unable to reach Langfuse"));
    }

    @Test
    void getTaskTotals_returnsEmptyTotalsWhenLangfuseBecomesUnavailableAfterStartup() throws IOException {
        server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/", exchange -> respond(exchange, "{\"ok\":true}"));
        server.start();

        LangfuseTaskObservabilityService service = new LangfuseTaskObservabilityService(
                true,
                "http://127.0.0.1:" + server.getAddress().getPort(),
                "pk-test",
                "sk-test",
                objectMapper
        );

        server.stop(0);
        server = null;

        TaskObservabilityTotals totals = service.getTaskTotals(UUID.randomUUID(), "agent-1", "completed");

        assertEquals(0L, totals.totalCostMicrodollars());
        assertEquals(0, totals.inputTokens());
        assertEquals(0, totals.outputTokens());
        assertEquals(0, totals.totalTokens());
        assertNull(totals.durationMs());
        assertNull(totals.traceId());
    }

    @AfterEach
    void tearDown() {
        if (server != null) {
            server.stop(0);
        }
    }

    @Test
    void getTaskObservability_mapsTraceAndSpanPayloadFromLangfuseApi() throws IOException {
        UUID taskId = UUID.randomUUID();
        server = HttpServer.create(new InetSocketAddress(0), 0);
        server.createContext("/", exchange -> respond(exchange, "{\"ok\":true}"));
        server.createContext("/api/public/traces", exchange -> respond(
                exchange,
                """
                {
                  "data": [
                    {
                      "id": "trace-123",
                      "totalCost": 0.005241,
                      "inputTokens": 1322,
                      "outputTokens": 85,
                      "totalTokens": 1407,
                      "latency": 3.862
                    }
                  ]
                }
                """
        ));
        server.createContext("/api/public/observations", exchange -> respond(
                exchange,
                """
                {
                  "data": [
                    {
                      "id": "obs-llm",
                      "parentObservationId": null,
                      "type": "GENERATION",
                      "name": "ChatAnthropic",
                      "model": "claude-sonnet-4-6",
                      "totalCost": 0.002793,
                      "usageDetails": {
                        "input": 616,
                        "output": 63,
                        "total": 679
                      },
                      "latency": 2.537,
                      "input": [{"role":"user","content":"What is 63 * 14?"}],
                      "output": {"role":"assistant","content":"Let me calculate that for you!"},
                      "startTime": "2026-03-27T20:21:59.881Z",
                      "endTime": "2026-03-27T20:22:02.418Z"
                    }
                  ]
                }
                """
        ));
        server.start();

        LangfuseTaskObservabilityService service = new LangfuseTaskObservabilityService(
                true,
                "http://127.0.0.1:" + server.getAddress().getPort(),
                "pk-test",
                "sk-test",
                objectMapper
        );

        TaskObservabilityResponse response = service.getTaskObservability(taskId, "agent-1", "completed");

        assertTrue(response.enabled());
        assertEquals(taskId, response.taskId());
        assertEquals("agent-1", response.agentId());
        assertEquals("trace-123", response.traceId());
        assertEquals(5241L, response.totalCostMicrodollars());
        assertEquals(1322, response.inputTokens());
        assertEquals(85, response.outputTokens());
        assertEquals(1407, response.totalTokens());
        assertEquals(3862L, response.durationMs());
        assertEquals(1, response.spans().size());
        assertEquals("obs-llm", response.spans().get(0).spanId());
        assertEquals("llm", response.spans().get(0).type());
        assertEquals(2793L, response.spans().get(0).costMicrodollars());
        assertEquals("claude-sonnet-4-6", response.spans().get(0).modelName());
        assertEquals(1, response.items().size());
        TaskObservabilityItemResponse item = response.items().get(0);
        assertEquals("obs-llm", item.itemId());
        assertEquals("llm_span", item.kind());
        assertEquals("ChatAnthropic", item.title());
        assertEquals(2793L, item.costMicrodollars());
        assertEquals(679, item.totalTokens());
    }

    private void respond(HttpExchange exchange, String body) throws IOException {
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.sendResponseHeaders(200, bytes.length);
        try (OutputStream outputStream = exchange.getResponseBody()) {
            outputStream.write(bytes);
        }
    }
}
