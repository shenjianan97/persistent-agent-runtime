package com.persistentagent.api.exception;

/**
 * Raised when the memory search endpoint is invoked with {@code mode=vector}
 * but the embedding provider call fails. {@code mode=hybrid} silently
 * degrades to BM25 instead and does NOT raise this exception.
 */
public class EmbeddingProviderUnavailableException extends RuntimeException {

    public EmbeddingProviderUnavailableException(String message) {
        super(message);
    }

    public EmbeddingProviderUnavailableException(String message, Throwable cause) {
        super(message, cause);
    }
}
