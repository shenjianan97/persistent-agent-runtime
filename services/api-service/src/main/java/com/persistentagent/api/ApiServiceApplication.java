package com.persistentagent.api;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.core.env.Environment;

@SpringBootApplication
public class ApiServiceApplication {

    private static final Logger log = LoggerFactory.getLogger(ApiServiceApplication.class);

    public static void main(String[] args) {
        SpringApplication.run(ApiServiceApplication.class, args);
    }

    @Bean
    ApplicationRunner logConfiguredEndpoints(Environment environment) {
        return args -> {
            String dbEndpoint = environment.getProperty("spring.datasource.url", "<unset>");
            String serverPort = environment.getProperty("server.port", "8080");
            log.info("API DB endpoint: {}", dbEndpoint);
            log.info("API listening on: http://localhost:{}", serverPort);
        };
    }
}
