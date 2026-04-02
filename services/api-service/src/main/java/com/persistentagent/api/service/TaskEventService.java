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

    private static final Logger log = LoggerFactory.getLogger(TaskEventService.class);

    private final TaskEventRepository taskEventRepository;

    public TaskEventService(TaskEventRepository taskEventRepository) {
        this.taskEventRepository = taskEventRepository;
    }

    /**
     * Records a task lifecycle event. Failures propagate to the caller so that
     * the paired state-transition mutation can be rolled back atomically.
     */
    public void recordEvent(String tenantId, UUID taskId, String agentId, String eventType,
                            String statusBefore, String statusAfter, String workerId,
                            String errorCode, String errorMessage, String detailsJson) {
        taskEventRepository.insertEvent(tenantId, taskId, agentId, eventType,
                statusBefore, statusAfter, workerId, errorCode, errorMessage, detailsJson);
        log.debug("Recorded task event: type={}, taskId={}, agentId={}", eventType, taskId, agentId);
    }

    /**
     * Lists events for a task in chronological order.
     *
     * @param taskId   the task to query
     * @param tenantId tenant scope
     * @param limit    maximum number of events to return (default 100)
     * @return wrapped list of events
     */
    public TaskEventListResponse listEvents(UUID taskId, String tenantId, int limit) {
        int effectiveLimit = limit > 0 ? limit : 100;
        List<TaskEventResponse> events = taskEventRepository.listEvents(taskId, tenantId, effectiveLimit);
        return new TaskEventListResponse(events);
    }
}
