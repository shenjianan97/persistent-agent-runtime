package com.persistentagent.api.util;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

import java.util.List;
import java.util.Map;
import java.util.stream.Stream;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Fixture-driven tests for {@link MessageContentExtractor}. The shared
 * fixture set (F-* IDs) is kept in lock-step with the Python compaction
 * tokens tests — deliberate provider-shape parity across languages.
 */
class MessageContentExtractorTest {

    private static Stream<Arguments> sharedFixtures() {
        return Stream.of(
                Arguments.of("F-STR-SIMPLE",
                        "Hello world",
                        "Hello world"),
                Arguments.of("F-STR-EMPTY",
                        "",
                        ""),
                Arguments.of("F-NULL",
                        null,
                        ""),
                Arguments.of("F-ANTHROPIC-PROSE",
                        List.of(Map.of("type", "text", "text", "Let me search for that")),
                        "Let me search for that"),
                Arguments.of("F-ANTHROPIC-MIXED",
                        List.of(
                                Map.of("type", "text", "text", "Sure, I'll check"),
                                Map.of("type", "tool_use", "id", "tu_1",
                                        "name", "web_search",
                                        "input", Map.of("q", "x"))),
                        "Sure, I'll check"),
                Arguments.of("F-ANTHROPIC-TOOLS-ONLY",
                        List.of(Map.of("type", "tool_use", "id", "tu_1",
                                "name", "web_search",
                                "input", Map.of("q", "x"))),
                        ""),
                Arguments.of("F-ANTHROPIC-THINKING",
                        List.of(
                                Map.of("type", "thinking",
                                        "thinking", "Deliberating...",
                                        "signature", "..."),
                                Map.of("type", "text", "text", "Here is the answer")),
                        "Deliberating...\n\nHere is the answer"),
                Arguments.of("F-OPENAI-NATIVE-OUTPUT-TEXT",
                        List.of(Map.of("type", "output_text", "text", "Here is the report")),
                        "Here is the report"),
                Arguments.of("F-OPENAI-NESTED-MESSAGE",
                        List.of(
                                Map.of("id", "rs_1", "type", "reasoning", "summary", List.of()),
                                Map.of("id", "msg_1", "type", "message",
                                        "content", List.of(Map.of(
                                                "type", "output_text",
                                                "text", "Below is a summary"))),
                                Map.of("id", "fc_1", "type", "function_call",
                                        "name", "web_search",
                                        "arguments", "{}")),
                        "Below is a summary"),
                Arguments.of("F-OPENAI-REASONING-ONLY",
                        List.of(
                                Map.of("id", "rs_1", "type", "reasoning", "summary", List.of()),
                                Map.of("id", "fc_1", "type", "function_call",
                                        "name", "web_search",
                                        "arguments", "{}")),
                        ""),
                Arguments.of("F-GEMINI-BARE-DICT",
                        List.of(Map.of("text", "Response from Gemini")),
                        "Response from Gemini"),
                Arguments.of("F-BEDROCK-CONVERSE-TEXT",
                        List.of(
                                Map.of("text", "Response via Bedrock"),
                                Map.of("toolUse", Map.of(
                                        "name", "search",
                                        "input", Map.of(),
                                        "toolUseId", "tu_1"))),
                        "Response via Bedrock"),
                Arguments.of("F-MULTI-TEXT-JOIN",
                        List.of(
                                Map.of("type", "text", "text", "First para"),
                                Map.of("type", "text", "text", "Second para")),
                        "First para\n\nSecond para")
        );
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("sharedFixtures")
    @DisplayName("extractText matches shared fixture expectations")
    void extractText_fixture(String fixtureId, Object input, String expected) {
        assertEquals(expected, MessageContentExtractor.extractText(input));
    }

    @Test
    void extractText_unknownScalar_returnsEmpty() {
        assertEquals("", MessageContentExtractor.extractText(42));
        assertEquals("", MessageContentExtractor.extractText(true));
    }

    @Test
    void extractText_skipsNonMapListElements() {
        List<Object> mixed = List.of(
                Map.of("type", "text", "text", "first"),
                123,
                Map.of("type", "text", "text", "second"));
        assertEquals("first\n\nsecond", MessageContentExtractor.extractText(mixed));
    }

    @Test
    void extractText_bareDictWithThreeKeysDoesNotMatch() {
        // Guard: {"text": "...", "lang": "en", "role": "user"} should NOT match
        // the bare-dict heuristic because there are >2 keys.
        List<Object> blocks = List.of(Map.of(
                "text", "should not match",
                "lang", "en",
                "role", "user"));
        assertEquals("", MessageContentExtractor.extractText(blocks));
    }

    @Test
    void extractText_nestedStringInBlockList() {
        List<Object> blocks = List.of("raw string", Map.of("type", "text", "text", "block"));
        assertEquals("raw string\n\nblock", MessageContentExtractor.extractText(blocks));
    }

    @Test
    void extractText_textFieldNumeric_yieldsEmpty() {
        // A malformed block where "text" is a number must NOT leak its
        // Java toString() ("123") as prose.
        List<Object> blocks = List.of(Map.of("type", "text", "text", 123));
        assertEquals("", MessageContentExtractor.extractText(blocks));
    }

    @Test
    void extractText_textFieldList_yieldsEmpty() {
        // A malformed block where "text" is a list must not leak "[a, b]".
        java.util.Map<String, Object> block = new java.util.HashMap<>();
        block.put("type", "text");
        block.put("text", List.of("a", "b"));
        assertEquals("", MessageContentExtractor.extractText(List.of(block)));
    }

    @Test
    void extractText_thinkingFieldNumeric_yieldsEmpty() {
        List<Object> blocks = List.of(Map.of("type", "thinking", "thinking", 42));
        assertEquals("", MessageContentExtractor.extractText(blocks));
    }

    @Test
    void extractText_deeplyNestedMessageWrapper_terminatesAtDepthCap() {
        // Pathological payload where `message` wraps itself indirectly —
        // bounded recursion must yield "" rather than stack-overflow.
        java.util.Map<String, Object> leaf = Map.of("type", "text", "text", "leaf");
        java.util.Map<String, Object> current = leaf;
        for (int i = 0; i < 10; i++) {
            current = Map.of("type", "message", "content", List.of(current));
        }
        String out = MessageContentExtractor.extractText(List.of(current));
        // Either we land on "leaf" (wrapping stayed shallow enough) or we
        // hit the cap and return "". Both are safe outcomes; assert only
        // that we don't throw and that no non-text leak appears.
        assertTrue(out.isEmpty() || "leaf".equals(out),
                "expected empty or the leaf text, got: " + out);
    }
}
