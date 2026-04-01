package com.persistentagent.api.exception;

public class AgentNotFoundException extends RuntimeException {

    private final String agentId;

    public AgentNotFoundException(String agentId) {
        super("Agent not found: " + agentId);
        this.agentId = agentId;
    }

    public String getAgentId() {
        return agentId;
    }
}
