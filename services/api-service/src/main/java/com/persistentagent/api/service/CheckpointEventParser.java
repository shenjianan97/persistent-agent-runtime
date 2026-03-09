package com.persistentagent.api.service;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.persistentagent.api.model.response.CheckpointEventResponse;
import org.postgresql.util.PGobject;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

@Component
public class CheckpointEventParser {
    private static final Logger log = LoggerFactory.getLogger(CheckpointEventParser.class);

    private final ObjectMapper objectMapper;

    public CheckpointEventParser(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    public CheckpointEventResponse parseEvent(
            Object checkpointPayload,
            Object metadataPayload,
            String fallbackNodeName,
            String checkpointId) {
        Object parsedPayload = parseJson(checkpointPayload, "checkpoint_payload", checkpointId);
        if (parsedPayload instanceof Map<?, ?> payloadMap) {
            Object channelValues = payloadMap.get("channel_values");
            if (channelValues instanceof Map<?, ?> channelMap) {
                Object messages = channelMap.get("messages");
                if (messages instanceof List<?> messageList && !messageList.isEmpty()) {
                    Object lastMessage = messageList.get(messageList.size() - 1);
                    CheckpointEventResponse event = extractEventFromMessage(lastMessage, checkpointId);
                    if (event != null) {
                        return event;
                    }

                    log.debug("Could not derive event from final checkpoint message for checkpoint {}", checkpointId);
                }
            }
        }

        return fallbackEvent(metadataPayload, fallbackNodeName, checkpointId);
    }

    public String extractNodeName(Object metadataPayload, String checkpointId) {
        Object parsed = parseJson(metadataPayload, "metadata_payload", checkpointId);
        if (parsed instanceof Map<?, ?> map) {
            Object source = map.get("source");
            if (source instanceof String s && !s.isBlank()) {
                return s;
            }
        }
        return "unknown";
    }

    private CheckpointEventResponse extractEventFromMessage(Object rawMessage, String checkpointId) {
        if (!(rawMessage instanceof Map<?, ?> messageMap)) {
            return null;
        }

        Object rawKwargs = messageMap.get("kwargs");
        if (!(rawKwargs instanceof Map<?, ?> kwargs)) {
            log.debug("Checkpoint {} message is missing kwargs", checkpointId);
            return null;
        }

        String messageType = asString(kwargs.get("type"));
        if (messageType == null || messageType.isBlank()) {
            log.debug("Checkpoint {} message is missing type", checkpointId);
            return null;
        }

        return switch (messageType) {
            case "human" -> new CheckpointEventResponse(
                    "input",
                    "User Input",
                    summarizeValue(kwargs.get("content")),
                    kwargs.get("content"),
                    null,
                    null,
                    null,
                    null);
            case "tool" -> {
                String toolName = asString(kwargs.get("name"));
                Object toolResult = parseJson(kwargs.get("content"), "tool_content", checkpointId);
                yield new CheckpointEventResponse(
                        "tool_result",
                        toolName == null || toolName.isBlank() ? "Tool Result" : "Tool Result: " + toolName,
                        summarizeValue(toolResult),
                        null,
                        toolName,
                        null,
                        toolResult,
                        null);
            }
            case "ai" -> parseAiMessage(kwargs);
            default -> new CheckpointEventResponse(
                    "checkpoint",
                    "Checkpoint",
                    summarizeValue(kwargs.get("content")),
                    kwargs.get("content"),
                    null,
                    null,
                    null,
                    null);
        };
    }

    private CheckpointEventResponse parseAiMessage(Map<?, ?> kwargs) {
        List<Object> toolCalls = asList(kwargs.get("tool_calls"));
        Object usage = kwargs.get("usage_metadata");
        if (!toolCalls.isEmpty()) {
            return buildToolCallEvent(toolCalls, usage);
        }

        return new CheckpointEventResponse(
                "output",
                "Agent Response",
                summarizeValue(kwargs.get("content")),
                kwargs.get("content"),
                null,
                null,
                null,
                usage);
    }

    private CheckpointEventResponse buildToolCallEvent(List<Object> toolCalls, Object usage) {
        List<Map<String, Object>> normalizedCalls = toolCalls.stream()
                .filter(Map.class::isInstance)
                .map(Map.class::cast)
                .map(call -> {
                    Map<String, Object> normalized = new java.util.LinkedHashMap<>();
                    normalized.put("name", asString(call.get("name")));
                    normalized.put("args", call.get("args"));
                    return normalized;
                })
                .toList();

        if (normalizedCalls.size() == 1) {
            Map<String, Object> call = normalizedCalls.get(0);
            String toolName = asString(call.get("name"));
            Object toolArgs = call.get("args");
            return new CheckpointEventResponse(
                    "tool_call",
                    toolName == null || toolName.isBlank() ? "Tool Call" : "Tool Call: " + toolName,
                    summarizeToolCall(toolName, toolArgs),
                    null,
                    toolName,
                    toolArgs,
                    null,
                    usage);
        }

        String toolNames = normalizedCalls.stream()
                .map(call -> asString(call.get("name")))
                .filter(name -> name != null && !name.isBlank())
                .collect(Collectors.joining(", "));

        String summary = normalizedCalls.isEmpty()
                ? "Agent requested multiple tools."
                : "Agent requested " + normalizedCalls.size() + " tools"
                + (toolNames.isBlank() ? "." : ": " + toolNames + ".");

        return new CheckpointEventResponse(
                "tool_call",
                "Tool Calls",
                summary,
                null,
                null,
                normalizedCalls,
                null,
                usage);
    }

    private CheckpointEventResponse fallbackEvent(Object metadataPayload, String fallbackNodeName, String checkpointId) {
        Object parsedMetadata = parseJson(metadataPayload, "metadata_payload", checkpointId);
        if (parsedMetadata instanceof Map<?, ?> map) {
            String source = asString(map.get("source"));
            Object step = map.get("step");
            if ("input".equals(source)) {
                return new CheckpointEventResponse(
                        "system",
                        "Execution Started",
                        "Worker claimed the task and initialized the graph state.",
                        null,
                        null,
                        null,
                        null,
                        null);
            }
            if (source != null) {
                return new CheckpointEventResponse(
                        "checkpoint",
                        "Checkpoint Saved",
                        "Framework step \"" + source + "\" completed" + (step != null ? " at graph step " + step + "." : "."),
                        null,
                        null,
                        null,
                        null,
                        null);
            }
        }

        return new CheckpointEventResponse(
                "checkpoint",
                "Checkpoint Saved",
                "Runtime checkpoint recorded for node \"" + fallbackNodeName + "\".",
                null,
                null,
                null,
                null,
                null);
    }

    private List<Object> asList(Object value) {
        if (value instanceof List<?> list) {
            return new ArrayList<>(list);
        }
        return List.of();
    }

    private String asString(Object value) {
        return value instanceof String s ? s : null;
    }

    private String summarizeToolCall(String toolName, Object toolArgs) {
        String toolLabel = (toolName == null || toolName.isBlank()) ? "tool" : toolName;
        String argsSummary = summarizeValue(toolArgs);
        if (argsSummary.isBlank()) {
            return "Agent requested " + toolLabel + ".";
        }
        return "Agent requested " + toolLabel + " with " + argsSummary;
    }

    private String summarizeValue(Object value) {
        if (value == null) {
            return "";
        }
        if (value instanceof String s) {
            String normalized = s.replaceAll("\\s+", " ").trim();
            if (normalized.length() <= 160) {
                return normalized;
            }
            return normalized.substring(0, 157) + "...";
        }

        Object parsed = parseJson(value, "summary_value", "n/a");
        if (parsed == null) {
            return "";
        }
        if (parsed instanceof String s) {
            String normalized = s.replaceAll("\\s+", " ").trim();
            if (normalized.length() <= 160) {
                return normalized;
            }
            return normalized.substring(0, 157) + "...";
        }

        try {
            String json = objectMapper.writeValueAsString(parsed);
            if (json.length() <= 160) {
                return json;
            }
            return json.substring(0, 157) + "...";
        } catch (JsonProcessingException e) {
            log.debug("Failed to serialize value while building checkpoint summary", e);
            return String.valueOf(parsed);
        }
    }

    private Object parseJson(Object value, String fieldName, String checkpointId) {
        if (value == null) {
            return null;
        }

        try {
            if (value instanceof String s) {
                return objectMapper.readValue(s, Object.class);
            }
            if (value instanceof PGobject pgObj) {
                String raw = pgObj.getValue();
                if (raw == null) {
                    return null;
                }
                return objectMapper.readValue(raw, Object.class);
            }
        } catch (Exception e) {
            log.debug("Failed to parse {} for checkpoint {}", fieldName, checkpointId, e);
        }

        return value;
    }
}
