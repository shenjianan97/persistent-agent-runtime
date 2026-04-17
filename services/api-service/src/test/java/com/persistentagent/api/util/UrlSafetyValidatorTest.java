package com.persistentagent.api.util;

import com.persistentagent.api.exception.ValidationException;
import org.junit.jupiter.api.Test;

import java.net.InetAddress;
import java.net.UnknownHostException;

import static org.junit.jupiter.api.Assertions.*;

class UrlSafetyValidatorTest {

    /** Resolver returning fixed IPs — keeps tests hermetic (no real DNS). */
    private static UrlSafetyValidator.Resolver resolverReturning(String... ipLiterals) {
        return host -> {
            InetAddress[] out = new InetAddress[ipLiterals.length];
            for (int i = 0; i < ipLiterals.length; i++) {
                out[i] = InetAddress.getByName(ipLiterals[i]);
            }
            return out;
        };
    }

    private static UrlSafetyValidator.Resolver failingResolver() {
        return host -> { throw new UnknownHostException(host); };
    }

    // --- scheme / format ---

    @Test
    void rejectsNull() {
        assertThrows(ValidationException.class, () -> UrlSafetyValidator.validate(null));
    }

    @Test
    void rejectsBlank() {
        assertThrows(ValidationException.class, () -> UrlSafetyValidator.validate("   "));
    }

    @Test
    void rejectsFileScheme() {
        ValidationException ex = assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("file:///etc/passwd"));
        assertTrue(ex.getMessage().toLowerCase().contains("scheme"));
    }

    @Test
    void rejectsGopherScheme() {
        assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("gopher://example.com/"));
    }

    @Test
    void rejectsDataScheme() {
        assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("data:text/plain,hi"));
    }

    @Test
    void rejectsSchemeless() {
        assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("example.com/path"));
    }

    @Test
    void rejectsCredentialsInUrl() {
        ValidationException ex = assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("https://alice:secret@example.com/",
                        resolverReturning("93.184.216.34")));
        assertTrue(ex.getMessage().toLowerCase().contains("credentials"));
    }

    // --- hostname suffix blocks ---

    @Test
    void rejectsDotLocalSuffix() {
        assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("http://printer.local/",
                        resolverReturning("93.184.216.34")));
    }

    @Test
    void rejectsDotInternalSuffix() {
        assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("http://metadata.internal/",
                        resolverReturning("93.184.216.34")));
    }

    // --- resolved-IP blocks (the ones that actually matter for SSRF) ---

    @Test
    void rejectsAwsMetadataLiteral() {
        ValidationException ex = assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("http://169.254.169.254/latest/meta-data/",
                        resolverReturning("169.254.169.254")));
        assertTrue(ex.getMessage().toLowerCase().contains("reserved"));
    }

    @Test
    void rejectsAlibabaMetadataLiteral() {
        // 100.100.100.200 — Alibaba Cloud IMDS, inside CGNAT range
        assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("http://100.100.100.200/",
                        resolverReturning("100.100.100.200")));
    }

    @Test
    void rejectsDnsPointingAtMetadata() {
        // DNS rebind attempt: public-looking hostname resolves to metadata IP
        assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("http://attacker.example.com/",
                        resolverReturning("169.254.169.254")));
    }

    @Test
    void rejectsZeroNetwork() {
        assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("http://a.example.com/",
                        resolverReturning("0.0.0.0")));
    }

    @Test
    void rejectsIpv6UniqueLocal() {
        assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("http://a.example.com/",
                        resolverReturning("fd00::1")));
    }

    @Test
    void rejectsIpv4MappedMetadata() {
        // ::ffff:169.254.169.254 — IPv4-mapped form must not bypass the v4 check
        assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("http://a.example.com/",
                        resolverReturning("::ffff:169.254.169.254")));
    }

    @Test
    void rejectsMixedResolutionIfAnyIsReserved() {
        // DNS returns both a public and a metadata IP — must reject.
        assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("http://a.example.com/",
                        resolverReturning("93.184.216.34", "169.254.169.254")));
    }

    @Test
    void rejectsUnresolvable() {
        ValidationException ex = assertThrows(ValidationException.class,
                () -> UrlSafetyValidator.validate("http://nx.example.com/",
                        failingResolver()));
        assertTrue(ex.getMessage().toLowerCase().contains("could not be resolved"));
    }

    // --- dev-friendly positive cases ---

    @Test
    void acceptsPublicHttps() {
        assertDoesNotThrow(() -> UrlSafetyValidator.validate(
                "https://cloud.langfuse.com/api/public/health",
                resolverReturning("93.184.216.34")));
    }

    @Test
    void acceptsLocalhost() {
        // Dev workflow: Langfuse/MCP running locally. Loopback is allowed.
        assertDoesNotThrow(() -> UrlSafetyValidator.validate(
                "http://localhost:3300/api/public/health",
                resolverReturning("127.0.0.1")));
    }

    @Test
    void acceptsLoopbackLiteral() {
        assertDoesNotThrow(() -> UrlSafetyValidator.validate(
                "http://127.0.0.1:8080/mcp",
                resolverReturning("127.0.0.1")));
    }

    @Test
    void acceptsRfc1918() {
        // Dev workflow: VPN-reachable internal service. Not blocked.
        assertDoesNotThrow(() -> UrlSafetyValidator.validate(
                "https://tools.internal-10/mcp",
                resolverReturning("10.20.30.40")));
    }
}
