package com.persistentagent.api.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

@Configuration
public class CorsConfig implements WebMvcConfigurer {

    @Value("${app.cors.allowed-origins:}")
    private String allowedOriginsRaw;

    @Override
    @SuppressWarnings("null")
    public void addCorsMappings(CorsRegistry registry) {
        if (allowedOriginsRaw != null && !allowedOriginsRaw.isBlank()) {
            // Only register CORS mappings when explicit origins are configured (local dev).
            // In deployed environments (same-origin behind ALB), no CORS config is needed —
            // omitting the mapping means Spring won't activate its CORS interceptor at all.
            registry.addMapping("/v1/**")
                    .allowedOrigins(allowedOriginsRaw.split(","))
                    .allowedMethods("GET", "POST", "PUT", "DELETE", "OPTIONS")
                    .allowedHeaders("*");
        }
    }
}
