package com.persistentagent.api.util;

import com.persistentagent.api.exception.ValidationException;

import java.net.InetAddress;
import java.net.URI;
import java.net.URISyntaxException;
import java.net.UnknownHostException;
import java.util.Set;

/**
 * Validates outbound URLs that the API service will dereference on behalf of
 * customer configuration (Langfuse hosts, tool server URLs).
 *
 * <p>Goal: block the SSRF vectors that matter while keeping dev workflows
 * (running Langfuse/MCP on localhost or a VPN-reachable RFC1918 host) intact.
 *
 * <h3>What this blocks</h3>
 * <ul>
 *   <li>Non-http(s) schemes (file://, gopher://, data://, etc.)</li>
 *   <li>Credentials embedded in the URL ({@code user:pass@host})</li>
 *   <li>Hostnames ending in {@code .local} or {@code .internal}</li>
 *   <li>Link-local IPs (169.254.0.0/16) — covers AWS/GCP/Azure instance metadata
 *       at {@code 169.254.169.254} and the Azure IMDS subnet</li>
 *   <li>CGNAT range (100.64.0.0/10) — covers Alibaba Cloud metadata at
 *       {@code 100.100.100.200}</li>
 *   <li>0.0.0.0/8, multicast, and 240.0.0.0/4 reserved</li>
 *   <li>IPv6 ULA (fc00::/7) and IPv4-mapped IPv6 variants of every v4 block above</li>
 * </ul>
 *
 * <h3>What this deliberately allows (dev-friendly)</h3>
 * <ul>
 *   <li>{@code localhost} and loopback IPs — devs run Langfuse/MCP locally</li>
 *   <li>RFC1918 (10/8, 172.16/12, 192.168/16) — VPN-reachable internal services</li>
 * </ul>
 *
 * <p>When the platform onboards external tenants, tighten this by adding a
 * "strict" mode that also rejects loopback and RFC1918.
 */
public final class UrlSafetyValidator {

    private static final Set<String> ALLOWED_SCHEMES = Set.of("http", "https");

    /** DNS resolver seam for testing. */
    @FunctionalInterface
    public interface Resolver {
        InetAddress[] resolve(String host) throws UnknownHostException;
    }

    private static final Resolver DEFAULT_RESOLVER = InetAddress::getAllByName;

    private UrlSafetyValidator() {
    }

    public static void validate(String url) {
        validate(url, DEFAULT_RESOLVER);
    }

    public static void validate(String url, Resolver resolver) {
        if (url == null || url.isBlank()) {
            throw new ValidationException("URL is required");
        }

        URI uri;
        try {
            uri = new URI(url.trim());
        } catch (URISyntaxException e) {
            throw new ValidationException("Invalid URL: " + e.getMessage());
        }

        String scheme = uri.getScheme();
        if (scheme == null || !ALLOWED_SCHEMES.contains(scheme.toLowerCase())) {
            throw new ValidationException("URL must use http or https scheme");
        }

        if (uri.getUserInfo() != null) {
            throw new ValidationException("Credentials embedded in URLs are not allowed");
        }

        String host = uri.getHost();
        if (host == null || host.isBlank()) {
            throw new ValidationException("URL must include a hostname");
        }

        String normalized = host.toLowerCase();
        if (normalized.endsWith(".local") || normalized.endsWith(".internal")) {
            throw new ValidationException("Hostnames ending in .local or .internal are not allowed");
        }

        InetAddress[] addresses;
        try {
            addresses = resolver.resolve(host);
        } catch (UnknownHostException e) {
            throw new ValidationException("Hostname could not be resolved: " + host);
        }

        if (addresses == null || addresses.length == 0) {
            throw new ValidationException("Hostname could not be resolved: " + host);
        }

        for (InetAddress address : addresses) {
            assertSafeAddress(address);
        }
    }

    private static void assertSafeAddress(InetAddress address) {
        if (address.isLinkLocalAddress()
                || address.isMulticastAddress()
                || address.isAnyLocalAddress()) {
            throw new ValidationException("URL resolves to a reserved address range");
        }

        byte[] bytes = address.getAddress();

        if (bytes.length == 4) {
            assertSafeIpv4(bytes);
            return;
        }

        if (bytes.length == 16) {
            // fc00::/7 — IPv6 unique local addresses
            if ((bytes[0] & 0xFE) == 0xFC) {
                throw new ValidationException("URL resolves to a reserved address range");
            }
            // ::ffff:0:0/96 — IPv4-mapped IPv6; re-check embedded v4 so mapped
            // forms of metadata / CGNAT / 0.0.0.0 are not a bypass.
            boolean prefixAllZero = true;
            for (int i = 0; i < 10; i++) {
                if (bytes[i] != 0) {
                    prefixAllZero = false;
                    break;
                }
            }
            boolean mappedPrefix = prefixAllZero
                    && (bytes[10] & 0xFF) == 0xFF
                    && (bytes[11] & 0xFF) == 0xFF;
            if (mappedPrefix) {
                assertSafeIpv4(new byte[]{bytes[12], bytes[13], bytes[14], bytes[15]});
            }
        }
    }

    private static void assertSafeIpv4(byte[] bytes) {
        int first = bytes[0] & 0xFF;
        int second = bytes[1] & 0xFF;

        // 0.0.0.0/8 — "this" network / unspecified
        if (first == 0) {
            throw new ValidationException("URL resolves to a reserved address range");
        }
        // 169.254.0.0/16 — link-local; includes cloud instance-metadata endpoints
        if (first == 169 && second == 254) {
            throw new ValidationException("URL resolves to a reserved address range");
        }
        // 100.64.0.0/10 — CGNAT; includes Alibaba Cloud IMDS (100.100.100.200)
        if (first == 100 && second >= 64 && second <= 127) {
            throw new ValidationException("URL resolves to a reserved address range");
        }
        // 224.0.0.0/4 — multicast (covers mapped-IPv6 bypass)
        if (first >= 224 && first <= 239) {
            throw new ValidationException("URL resolves to a reserved address range");
        }
        // 240.0.0.0/4 — reserved / future use
        if (first >= 240) {
            throw new ValidationException("URL resolves to a reserved address range");
        }
    }
}
