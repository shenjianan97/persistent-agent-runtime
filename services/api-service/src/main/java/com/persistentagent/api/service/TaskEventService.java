package com.persistentagent.api.service;

import com.persistentagent.api.model.response.TaskEventListResponse;
import com.persistentagent.api.model.response.TaskEventResponse;
import com.persistentagent.api.repository.TaskEventRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.UUID;

@Service
public class TaskEventService {

    private static final Logger logger = LoggerFactory.getLogger(TaskEventService.class);

    private final TaskEventRepository taskEventRepository;

    public TaskEventService(TaskEventRepository taskEventRepository) {
        this.taskEventRepository = taskEventRepository;
    }

    /**
     * Records a task lifecycle event. Failures propagate so the caller can
     * roll back the paired task-state transition.
     */
    public void recordEvent(String tenantId, UUID taskId, String agentId,
                            String eventType, String statusBefore, String statusAfter,
                            String workerId, String errorCode, String errorMessage,
                            String detailsJson) {
        taskEventRepository.insertEvent(tenantId, taskId, agentId, eventType,
                statusBefore, statusAfter, workerId, errorCode, errorMessage, detailsJson);
        logger.debug("Recorded event {} for task {} ({}->{})",
                eventType, taskId, statusBefore, statusAfter);
    }

    /**
     * Lists events for a task in chronological order.
     */
    public TaskEventListResponse listEvents(UUID taskId, String tenantId, int limit) {
        List<TaskEventResponse> events = taskEventRepository.listEvents(taskId, tenantId, limit);
        return new TaskEventListResponse(events);
    }
}
