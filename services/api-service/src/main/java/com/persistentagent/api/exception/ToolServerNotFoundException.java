package com.persistentagent.api.exception;

public class ToolServerNotFoundException extends RuntimeException {

    private final String serverId;

    public ToolServerNotFoundException(String serverId) {
        super("Tool server not found: " + serverId);
        this.serverId = serverId;
    }

    public String getServerId() {
        return serverId;
    }
}
