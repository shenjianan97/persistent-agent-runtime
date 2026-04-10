package com.persistentagent.api.service;

import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.repository.ModelRepository;
import com.persistentagent.api.repository.ToolServerRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@ExtendWith(MockitoExtension.class)
class ConfigValidationHelperTest {

    @Mock
    private ModelRepository modelRepository;

    @Mock
    private ToolServerRepository toolServerRepository;

    private ConfigValidationHelper helper;

    @BeforeEach
    void setUp() {
        helper = new ConfigValidationHelper(modelRepository, toolServerRepository, false);
    }

    // --- validateToolServers tests ---

    @Test
    void testValidateToolServers_null_ok() {
        // null tool_servers should pass without any repository calls
        assertDoesNotThrow(() -> helper.validateToolServers(null));
        verifyNoInteractions(toolServerRepository);
    }

    @Test
    void testValidateToolServers_empty_ok() {
        // empty list should pass without any repository calls
        assertDoesNotThrow(() -> helper.validateToolServers(List.of()));
        verifyNoInteractions(toolServerRepository);
    }

    @Test
    void testValidateToolServers_validNames_ok() {
        // valid, existing, active servers should pass
        Map<String, Object> row = new HashMap<>();
        row.put("name", "jira-tools");
        row.put("status", "active");

        when(toolServerRepository.findByTenantAndNames(eq("default"), eq(List.of("jira-tools"))))
                .thenReturn(List.of(row));

        assertDoesNotThrow(() -> helper.validateToolServers(List.of("jira-tools")));
        verify(toolServerRepository).findByTenantAndNames("default", List.of("jira-tools"));
    }

    @Test
    void testValidateToolServers_multipleValidNames_ok() {
        // multiple valid, existing, active servers should pass
        Map<String, Object> row1 = new HashMap<>();
        row1.put("name", "jira-tools");
        row1.put("status", "active");

        Map<String, Object> row2 = new HashMap<>();
        row2.put("name", "slack-tools");
        row2.put("status", "active");

        when(toolServerRepository.findByTenantAndNames(eq("default"), anyList()))
                .thenReturn(List.of(row1, row2));

        assertDoesNotThrow(() -> helper.validateToolServers(List.of("jira-tools", "slack-tools")));
    }

    @Test
    void testValidateToolServers_duplicateName_throws() {
        // duplicate names should throw ValidationException before hitting the repository
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateToolServers(List.of("jira-tools", "jira-tools")));
        assertTrue(ex.getMessage().contains("Duplicate tool server name: jira-tools"));
        verifyNoInteractions(toolServerRepository);
    }

    @Test
    void testValidateToolServers_invalidNameFormat_throws() {
        // uppercase name does not match the required pattern
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateToolServers(List.of("UPPERCASE")));
        assertTrue(ex.getMessage().contains("Invalid tool server name: UPPERCASE"));
        verifyNoInteractions(toolServerRepository);
    }

    @Test
    void testValidateToolServers_nameWithSpaces_throws() {
        // name with spaces does not match the required pattern
        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateToolServers(List.of("invalid name")));
        assertTrue(ex.getMessage().contains("Invalid tool server name: invalid name"));
        verifyNoInteractions(toolServerRepository);
    }

    @Test
    void testValidateToolServers_serverNotFound_throws() {
        // referencing a non-existent server should throw ValidationException
        when(toolServerRepository.findByTenantAndNames(eq("default"), anyList()))
                .thenReturn(List.of()); // empty result — server not found

        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateToolServers(List.of("nonexistent-server")));
        assertTrue(ex.getMessage().contains("Tool server not found: nonexistent-server"));
    }

    @Test
    void testValidateToolServers_serverDisabled_throws() {
        // referencing a disabled server should throw ValidationException
        Map<String, Object> row = new HashMap<>();
        row.put("name", "disabled-server");
        row.put("status", "disabled");

        when(toolServerRepository.findByTenantAndNames(eq("default"), anyList()))
                .thenReturn(List.of(row));

        ValidationException ex = assertThrows(ValidationException.class,
                () -> helper.validateToolServers(List.of("disabled-server")));
        assertTrue(ex.getMessage().contains("Tool server 'disabled-server' is disabled"));
    }
}
