package com.persistentagent.api.service.observability;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.model.response.TaskObservabilityItemResponse;
import com.persistentagent.api.model.response.TaskObservabilityResponse;
import com.persistentagent.api.model.response.TaskObservabilitySpanResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.web.util.UriComponentsBuilder;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Comparator;
import java.util.List;
import java.util.UUID;

@Service
public class LangfuseTaskObservabilityService implements TaskObservabilityService {
    private static final Logger logger = LoggerFactory.getLogger(LangfuseTaskObservabilityService.class);
    private final boolean enabled;
    private final String host;
    private final String publicKey;
    private final String secretKey;
    private final ObjectMapper objectMapper;
    private final HttpClient httpClient;

    public LangfuseTaskObservabilityService(
            @Value("${app.langfuse.enabled:false}") boolean enabled,
            @Value("${app.langfuse.host:}") String host,
            @Value("${app.langfuse.public-key:}") String publicKey,
            @Value("${app.langfuse.secret-key:}") String secretKey,
            ObjectMapper objectMapper
    ) {
        this.enabled = enabled;
        this.host = trimTrailingSlash(host);
        this.publicKey = publicKey;
        this.secretKey = secretKey;
        if (enabled && (this.host.isBlank() || this.publicKey.isBlank() || this.secretKey.isBlank())) {
            throw new IllegalStateException("LANGFUSE_ENABLED requires LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, and LANGFUSE_SECRET_KEY");
        }
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .version(HttpClient.Version.HTTP_1_1)
                .build();
        if (enabled) {
            assertLangfuseReachable();
        }
        this.objectMapper = objectMapper;
    }

    @Override
    public TaskObservabilityTotals getTaskTotals(UUID taskId, String agentId, String taskStatus) {
        if (!enabled) {
            return TaskObservabilityTotals.empty();
        }

        TraceContext trace = safeFetchTrace(taskId);
        if (trace == null) {
            return TaskObservabilityTotals.empty();
        }

        List<TaskObservabilitySpanResponse> spans = safeFetchSpans(taskId, agentId, trace.traceId());
        return aggregate(trace, spans);
    }

    @Override
    public TaskObservabilityResponse getTaskObservability(UUID taskId, String agentId, String taskStatus) {
        if (!enabled) {
            return disabled(taskId, agentId, taskStatus);
        }

        TraceContext trace = safeFetchTrace(taskId);
        if (trace == null) {
            return new TaskObservabilityResponse(
                    true,
                    taskId,
                    agentId,
                    taskStatus,
                    null,
                    0L,
                    0,
                    0,
                    0,
                    null,
                    List.of(),
                    List.of()
            );
        }

        List<TaskObservabilitySpanResponse> spans = safeFetchSpans(taskId, agentId, trace.traceId());
        TaskObservabilityTotals totals = aggregate(trace, spans);
        return new TaskObservabilityResponse(
                true,
                taskId,
                agentId,
                taskStatus,
                trace.traceId(),
                totals.totalCostMicrodollars(),
                totals.inputTokens(),
                totals.outputTokens(),
                totals.totalTokens(),
                totals.durationMs(),
                spans,
                spans.stream().map(this::toItem).toList()
        );
    }

    private TaskObservabilityResponse disabled(UUID taskId, String agentId, String taskStatus) {
        return new TaskObservabilityResponse(false, taskId, agentId, taskStatus, null, 0L, 0, 0, 0, null, List.of(), List.of());
    }

    private void assertLangfuseReachable() {
        try {
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(host))
                    .GET()
                    .timeout(Duration.ofSeconds(5))
                    .build();
            HttpResponse<Void> response = httpClient.send(request, HttpResponse.BodyHandlers.discarding());
            if (response.statusCode() >= 500) {
                throw new IllegalStateException(
                        "Unable to reach Langfuse at " + host + " (status " + response.statusCode() + ")"
                );
            }
        } catch (IOException | InterruptedException e) {
            if (e instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            throw new IllegalStateException("Unable to reach Langfuse at " + host, e);
        }
    }

    private TraceContext safeFetchTrace(UUID taskId) {
        try {
            return fetchTrace(taskId);
        } catch (RuntimeException e) {
            logger.warn("Failed to load Langfuse trace for task {}. Returning empty observability payload.", taskId, e);
            return null;
        }
    }

    private List<TaskObservabilitySpanResponse> safeFetchSpans(UUID taskId, String agentId, String traceId) {
        try {
            return fetchSpans(taskId, agentId, traceId);
        } catch (RuntimeException e) {
            logger.warn("Failed to load Langfuse spans for task {} trace {}. Returning an empty span list.", taskId, traceId, e);
            return List.of();
        }
    }

    private TraceContext fetchTrace(UUID taskId) {
        String sessionId = encode(taskId.toString());
        URI uri = URI.create(host + "/api/public/traces?sessionId=" + sessionId + "&limit=1&orderBy=timestamp.desc&fields=metrics");
        JsonNode body = getJson(uri);
        JsonNode data = body.path("data");
        if (!data.isArray() || data.isEmpty()) {
            return null;
        }

        JsonNode trace = data.get(0);
        return new TraceContext(
                trace.path("id").asText(null),
                asLongMicrodollars(trace.get("totalCost")),
                asInt(trace.get("inputTokens")),
                asInt(trace.get("outputTokens")),
                asInt(trace.get("totalTokens")),
                asDurationMillis(trace.get("latency"))
        );
    }

    private List<TaskObservabilitySpanResponse> fetchSpans(UUID taskId, String agentId, String traceId) {
        URI uri = UriComponentsBuilder.fromUriString(host + "/api/public/observations")
                .queryParam("traceId", traceId)
                .queryParam("limit", 100)
                .build(true)
                .toUri();

        JsonNode body = getJson(uri);
        JsonNode data = body.path("data");
        if (!data.isArray()) {
            return List.of();
        }

        List<TaskObservabilitySpanResponse> spans = new ArrayList<>();
        for (JsonNode node : data) {
            OffsetDateTime startedAt = parseDate(node.get("startTime"));
            OffsetDateTime endedAt = parseDate(node.get("endTime"));
            String rawType = node.path("type").asText("SPAN");
            int inputTokens = usageValue(node.get("usageDetails"), "input", "inputTokens", "prompt_tokens", "promptTokens");
            if (inputTokens == 0) {
                inputTokens = usageValue(node.get("usage"), "input");
            }
            if (inputTokens == 0) {
                inputTokens = asInt(node.get("promptTokens"));
            }

            int outputTokens = usageValue(node.get("usageDetails"), "output", "outputTokens", "completion_tokens", "completionTokens");
            if (outputTokens == 0) {
                outputTokens = usageValue(node.get("usage"), "output");
            }
            if (outputTokens == 0) {
                outputTokens = asInt(node.get("completionTokens"));
            }

            int totalTokens = usageValue(node.get("usageDetails"), "total", "totalTokens", "total_tokens");
            if (totalTokens == 0) {
                totalTokens = usageValue(node.get("usage"), "total");
            }
            if (totalTokens == 0) {
                totalTokens = asInt(node.get("totalTokens"));
            }
            if (totalTokens == 0) {
                totalTokens = inputTokens + outputTokens;
            }

            spans.add(new TaskObservabilitySpanResponse(
                    node.path("id").asText(),
                    node.path("parentObservationId").asText(null),
                    taskId.toString(),
                    agentId,
                    null,
                    normalizeType(rawType),
                    node.path("name").asText(null),
                    textValue(node.get("model"), node.path("providedModelName").asText(null)),
                    "TOOL".equalsIgnoreCase(rawType) ? node.path("name").asText(null) : null,
                    firstCostMicrodollars(node.get("totalCost"), node.get("calculatedTotalCost")),
                    inputTokens,
                    outputTokens,
                    totalTokens,
                    asDurationMillis(node.get("latency")),
                    coerceJson(node.get("input")),
                    coerceJson(node.get("output")),
                    startedAt,
                    endedAt
            ));
        }

        spans.sort(Comparator.comparing(TaskObservabilitySpanResponse::startedAt, Comparator.nullsLast(Comparator.naturalOrder())));
        return spans;
    }

    private TaskObservabilityTotals aggregate(TraceContext trace, List<TaskObservabilitySpanResponse> spans) {
        if (spans.isEmpty()) {
            return new TaskObservabilityTotals(
                    trace.totalCostMicrodollars(),
                    trace.inputTokens(),
                    trace.outputTokens(),
                    trace.totalTokens(),
                    trace.durationMs(),
                    trace.traceId()
            );
        }

        long totalCost = trace.totalCostMicrodollars() > 0
                ? trace.totalCostMicrodollars()
                : spans.stream().mapToLong(TaskObservabilitySpanResponse::costMicrodollars).sum();
        int inputTokens = trace.inputTokens() > 0
                ? trace.inputTokens()
                : spans.stream().mapToInt(TaskObservabilitySpanResponse::inputTokens).sum();
        int outputTokens = trace.outputTokens() > 0
                ? trace.outputTokens()
                : spans.stream().mapToInt(TaskObservabilitySpanResponse::outputTokens).sum();
        int totalTokens = trace.totalTokens() > 0
                ? trace.totalTokens()
                : spans.stream().mapToInt(TaskObservabilitySpanResponse::totalTokens).sum();
        Long durationMs = trace.durationMs();
        if (durationMs == null) {
            OffsetDateTime started = spans.stream()
                    .map(TaskObservabilitySpanResponse::startedAt)
                    .filter(java.util.Objects::nonNull)
                    .min(Comparator.naturalOrder())
                    .orElse(null);
            OffsetDateTime ended = spans.stream()
                    .map(TaskObservabilitySpanResponse::endedAt)
                    .filter(java.util.Objects::nonNull)
                    .max(Comparator.naturalOrder())
                    .orElse(null);
            if (started != null && ended != null) {
                durationMs = Duration.between(started, ended).toMillis();
            }
        }

        return new TaskObservabilityTotals(totalCost, inputTokens, outputTokens, totalTokens, durationMs, trace.traceId());
    }

    private TaskObservabilityItemResponse toItem(TaskObservabilitySpanResponse span) {
        String kind = switch (span.type()) {
            case "llm" -> "llm_span";
            case "tool" -> "tool_span";
            default -> "system_span";
        };
        String title = switch (kind) {
            case "llm_span" -> firstNonBlank(span.nodeName(), span.modelName(), "LLM span");
            case "tool_span" -> "Tool: " + firstNonBlank(span.toolName(), span.nodeName(), "tool");
            default -> firstNonBlank(span.nodeName(), "System span");
        };
        String summary = switch (kind) {
            case "llm_span" -> "LLM generation completed.";
            case "tool_span" -> "Tool call completed.";
            default -> "System span recorded.";
        };
        return new TaskObservabilityItemResponse(
                span.spanId(),
                span.parentSpanId(),
                kind,
                title,
                summary,
                null,
                span.nodeName(),
                span.toolName(),
                span.modelName(),
                span.costMicrodollars(),
                span.inputTokens(),
                span.outputTokens(),
                span.totalTokens(),
                span.durationMs(),
                span.input(),
                span.output(),
                span.startedAt(),
                span.endedAt()
        );
    }

    private String firstNonBlank(String... values) {
        for (String value : values) {
            if (value != null && !value.isBlank()) {
                return value;
            }
        }
        return "";
    }

    private JsonNode getJson(URI uri) {
        HttpRequest request = HttpRequest.newBuilder(uri)
                .header("Authorization", basicAuth())
                .header("Accept", "application/json")
                .timeout(Duration.ofSeconds(10))
                .GET()
                .build();
        try {
            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() >= 400) {
                throw new IllegalStateException("Langfuse API request failed with status " + response.statusCode());
            }
            return objectMapper.readTree(response.body());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("Failed to fetch Langfuse observability data", e);
        } catch (IOException e) {
            throw new IllegalStateException("Failed to fetch Langfuse observability data", e);
        }
    }

    private String basicAuth() {
        String token = Base64.getEncoder()
                .encodeToString((publicKey + ":" + secretKey).getBytes(StandardCharsets.UTF_8));
        return "Basic " + token;
    }

    private static String trimTrailingSlash(String value) {
        if (value == null) {
            return "";
        }
        return value.endsWith("/") ? value.substring(0, value.length() - 1) : value;
    }

    private static String encode(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8);
    }

    private static int usageValue(JsonNode usageDetails, String... keys) {
        if (usageDetails == null || usageDetails.isMissingNode() || usageDetails.isNull()) {
            return 0;
        }
        for (String key : keys) {
            JsonNode value = usageDetails.get(key);
            if (value != null && value.isNumber()) {
                return value.asInt();
            }
        }
        return 0;
    }

    private static long asLongMicrodollars(JsonNode usdValue) {
        if (usdValue == null || usdValue.isNull() || !usdValue.isNumber()) {
            return 0L;
        }
        return Math.round(usdValue.asDouble() * 1_000_000d);
    }

    private static long firstCostMicrodollars(JsonNode... values) {
        for (JsonNode value : values) {
            long microdollars = asLongMicrodollars(value);
            if (microdollars > 0L) {
                return microdollars;
            }
        }
        return 0L;
    }

    private static int asInt(JsonNode node) {
        return node != null && node.isNumber() ? node.asInt() : 0;
    }

    private static Long asDurationMillis(JsonNode secondsNode) {
        if (secondsNode == null || secondsNode.isNull() || !secondsNode.isNumber()) {
            return null;
        }
        return Math.round(secondsNode.asDouble() * 1000d);
    }

    private OffsetDateTime parseDate(JsonNode node) {
        if (node == null || node.isNull() || node.asText().isBlank()) {
            return null;
        }
        return OffsetDateTime.parse(node.asText());
    }

    private Object coerceJson(JsonNode node) {
        if (node == null || node.isNull()) {
            return null;
        }
        return objectMapper.convertValue(node, Object.class);
    }

    private static String textValue(JsonNode node, String fallback) {
        if (node != null && !node.isNull() && !node.asText().isBlank()) {
            return node.asText();
        }
        return fallback;
    }

    private static String normalizeType(String rawType) {
        return switch (rawType.toUpperCase()) {
            case "GENERATION" -> "llm";
            case "TOOL" -> "tool";
            default -> "system";
        };
    }

    private record TraceContext(
            String traceId,
            long totalCostMicrodollars,
            int inputTokens,
            int outputTokens,
            int totalTokens,
            Long durationMs
    ) {
    }
}
