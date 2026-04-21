package com.persistentagent.api.util;

import java.util.List;
import java.util.Map;

/**
 * Shared text extractor for LangChain message {@code content}, which is a
 * union type — {@code String} for legacy Anthropic-prose turns, or
 * {@code List<Block>} where each block carries a provider-specific shape
 * ({@code {type: text, ...}}, {@code {type: output_text, ...}}, nested
 * Responses message wrappers, Claude extended-thinking, Gemini / Bedrock
 * Converse bare-dict, etc.).
 *
 * <p>The walker is an <em>allowlist</em>, not a full LangChain block
 * translator (Python owns canonical normalization via
 * {@code BaseMessage.text}). The Java side only needs to flatten text for
 * the Activity projection + the task-detail {@code output.result} field,
 * where the Console renders pre-normalized strings.
 *
 * <p>Recognized text-bearing shapes (first match wins):
 * <ol>
 *   <li>{@code {type: "text", text: "..."}} — Anthropic + legacy worker paths</li>
 *   <li>{@code {type: "output_text", text: "..."}} — OpenAI Responses native</li>
 *   <li>{@code {type: "message", content: [...]}} — OpenAI Responses wrapper
 *       (recurses one level)</li>
 *   <li>{@code {type: "thinking", thinking: "..."}} — Claude extended thinking</li>
 *   <li>bare {@code {text: "..."}} dict with no {@code type} key and at most
 *       two keys — Gemini, Bedrock Converse (HEURISTIC)</li>
 * </ol>
 *
 * <p>Rule 5 is explicitly a heuristic. If a future provider ships a block
 * where {@code text} is metadata rather than prose, add a typed rule before
 * rule 5 rather than extending the heuristic's reach. The {@code size() <= 2}
 * guard keeps block dicts carrying multiple sibling keys from matching.
 *
 * <p>Non-text block types ({@code tool_use}, {@code function_call},
 * {@code reasoning}, {@code image}, {@code inline_data}, …) yield nothing —
 * they are surfaced separately via the normalized {@code tool_calls[]} field
 * or intentionally omitted from the text view.
 */
public final class MessageContentExtractor {

    /** Join separator between sibling text blocks. Human-readable; matches
     *  the legacy Console UX. Python delegates to
     *  {@code AIMessage.content_blocks} which uses {@code ""} by design — the
     *  divergence is intentional (see CLAUDE.md §LLM Provider Support). */
    private static final String BLOCK_SEPARATOR = "\n\n";

    /** Recursion cap for the OpenAI-Responses {@code message.content[...]}
     *  unwrap. Real checkpoints never nest beyond one level; the cap is a
     *  defence against hostile / malformed payloads where a {@code message}
     *  block wraps itself indirectly. Past the cap, the walker yields
     *  {@code ""} and logs nothing — pathological input degrades to empty
     *  prose, not a stack overflow on the API thread. */
    private static final int MAX_MESSAGE_RECURSION = 4;

    private MessageContentExtractor() {
    }

    /**
     * Flattens message content (string, block list, or other) to plain text.
     *
     * <ul>
     *   <li>{@code null} → {@code ""}</li>
     *   <li>{@code String} → unchanged</li>
     *   <li>{@code List<?>} → walk recognized blocks, join with {@code "\n\n"}</li>
     *   <li>anything else → {@code ""}</li>
     * </ul>
     */
    public static String extractText(Object content) {
        return extractText(content, 0);
    }

    private static String extractText(Object content, int depth) {
        if (content == null) {
            return "";
        }
        if (content instanceof String s) {
            return s;
        }
        if (content instanceof List<?> list) {
            StringBuilder sb = new StringBuilder();
            for (Object block : list) {
                String part = extractFromBlock(block, depth);
                if (part == null || part.isEmpty()) {
                    continue;
                }
                if (sb.length() > 0) {
                    sb.append(BLOCK_SEPARATOR);
                }
                sb.append(part);
            }
            return sb.toString();
        }
        return "";
    }

    @SuppressWarnings("unchecked")
    private static String extractFromBlock(Object block, int depth) {
        if (block instanceof String s) {
            return s;
        }
        if (!(block instanceof Map<?, ?> rawMap)) {
            return null;
        }
        Map<String, Object> map = (Map<String, Object>) rawMap;

        Object typeObj = map.get("type");
        String type = typeObj instanceof String s ? s : null;

        if (type != null) {
            switch (type) {
                case "text", "output_text" -> {
                    // Strict: only accept String. A malformed block with a
                    // numeric / null / list "text" field must yield nothing
                    // rather than leak a Java toString() into user-visible
                    // prose (e.g. "[a, b]" or "{k=v}").
                    return stringOrNull(map.get("text"));
                }
                case "message" -> {
                    // OpenAI Responses wrapper: recurse one step into its
                    // inner content list so nested output_text blocks
                    // surface. Bounded by MAX_MESSAGE_RECURSION to stop
                    // hostile/malformed payloads blowing the stack.
                    if (depth >= MAX_MESSAGE_RECURSION) {
                        return null;
                    }
                    Object inner = map.get("content");
                    if (inner instanceof List<?>) {
                        return extractText(inner, depth + 1);
                    }
                    return null;
                }
                case "thinking" -> {
                    return stringOrNull(map.get("thinking"));
                }
                default -> {
                    return null;
                }
            }
        }

        // Bare {text: "..."} dict — Gemini, Bedrock Converse. Heuristic:
        // require no "type" key and at most two keys total so noisy dicts
        // carrying sibling metadata do not falsely match.
        if (map.containsKey("text") && map.size() <= 2) {
            return stringOrNull(map.get("text"));
        }
        return null;
    }

    /** Returns the value only when it is a {@code String}; otherwise
     *  {@code null}. Deliberately strict — the walker's contract is "text",
     *  not "any scalar stringified". */
    private static String stringOrNull(Object value) {
        return value instanceof String s ? s : null;
    }
}
