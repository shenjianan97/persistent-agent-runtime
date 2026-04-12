package com.persistentagent.api.service;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class S3StorageServiceTest {

    @Test
    void serviceClassExists() {
        assertDoesNotThrow(() -> Class.forName("com.persistentagent.api.service.S3StorageService"));
    }
}
