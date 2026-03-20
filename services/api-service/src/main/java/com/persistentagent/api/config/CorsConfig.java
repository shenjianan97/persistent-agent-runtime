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
        if (allowedOriginsRaw == null || allowedOriginsRaw.isBlank()) {
            // No explicit origins configured — allow any origin (Phase 1: internal ALB, no auth)
            registry.addMapping("/v1/**")
                    .allowedOriginPatterns("*")
                    .allowedMethods("GET", "POST", "OPTIONS")
                    .allowedHeaders("*");
        } else {
            registry.addMapping("/v1/**")
                    .allowedOrigins(allowedOriginsRaw.split(","))
                    .allowedMethods("GET", "POST", "OPTIONS")
                    .allowedHeaders("*");
        }
    }
}
