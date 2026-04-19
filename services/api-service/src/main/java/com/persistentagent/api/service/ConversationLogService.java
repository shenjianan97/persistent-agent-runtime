package com.persistentagent.api.service;

import com.persistentagent.api.config.ValidationConstants;
import com.persistentagent.api.exception.TaskNotFoundException;
import com.persistentagent.api.exception.ValidationException;
import com.persistentagent.api.model.response.ConversationEntryResponse;
import com.persistentagent.api.repository.ConversationLogRepository;
import com.persistentagent.api.repository.TaskRepository;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.UUID;

/**
 * Phase 2 Track 7 Task 13 — serves the user-facing conversation log to the
 * Console.
 *
 * <p>Tenant isolation is enforced on every read path: the {@code tenantId}
 * used for both the task-existence check and the log query is resolved
 * internally from the request context (Phase 1 uses {@link
 * ValidationConstants#DEFAULT_TENANT_ID}; future phases replace this with a
 * principal-derived value). The client NEVER supplies a tenant id.
 *
 * <p>When the {@code (task_id, tenant_id)} pair does not exist, the service
 * throws {@link TaskNotFoundException} → HTTP 404. The response for "task
 * belongs to another tenant" and "task does not exist" is deliberately
 * indistinguishable to prevent task-id enumeration across tenants.
 */
@Service
public class ConversationLogService {

    /** Spec §API endpoint — default page size when the client omits {@code limit}. */
    public static final int DEFAULT_LIMIT = 200;

    /** Spec §API endpoint — hard cap on {@code limit}; lowered from an earlier
     *  draft after review (5s polling makes large pages unnecessary). */
    public static final int MAX_LIMIT = 1000;

    private final TaskRepository taskRepository;
    private final ConversationLogRepository conversationLogRepository;

    public ConversationLogService(
            TaskRepository taskRepository,
            ConversationLogRepository conversationLogRepository) {
        this.taskRepository = taskRepository;
        this.conversationLogRepository = conversationLogRepository;
    }

    /**
     * Returns one page of conversation-log entries for the given task.
     *
     * @param taskId          the task to read
     * @param afterSequence   exclusive lower bound on {@code sequence}; pass
     *                        {@code null} for the first page (treated as
     *                        {@code 0})
     * @param limit           client-requested page size; {@code null} → use
     *                        {@link #DEFAULT_LIMIT}. Values outside
     *                        {@code [1, MAX_LIMIT]} are rejected with a 400.
     * @throws TaskNotFoundException when the task does not exist or belongs
     *                               to another tenant (indistinguishable —
     *                               see class javadoc).
     * @throws ValidationException   when {@code limit} is non-positive or
     *                               exceeds {@link #MAX_LIMIT}.
     */
    public ConversationEntryResponse.Page getConversation(
            UUID taskId, Long afterSequence, Integer limit) {
        // Cheap validation first — no DB work for malformed requests.
        int effectiveLimit = resolveLimit(limit);
        long effectiveAfter = afterSequence != null ? afterSequence : 0L;

        String tenantId = ValidationConstants.DEFAULT_TENANT_ID;

        // Tenant-scoped existence check before any log query. Indistinguishable
        // 404 for "task does not exist" vs "wrong tenant" — see class javadoc.
        taskRepository.findByIdAndTenant(taskId, tenantId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));

        List<ConversationEntryResponse> entries = conversationLogRepository.findByTask(
                tenantId, taskId, effectiveAfter, effectiveLimit);

        Long nextSequence = null;
        if (entries.size() == effectiveLimit && !entries.isEmpty()) {
            // Page is full → there may be more. The repository returns entries
            // ordered by sequence ASC, so the last entry carries max(sequence).
            nextSequence = entries.get(entries.size() - 1).sequence();
        }

        return new ConversationEntryResponse.Page(entries, nextSequence);
    }

    /**
     * Validates and clamps the requested page size. Rejected values surface
     * as {@link ValidationException} → HTTP 400 so clients learn the
     * expected bounds rather than silently receiving a clamped response.
     */
    private int resolveLimit(Integer limit) {
        if (limit == null) {
            return DEFAULT_LIMIT;
        }
        if (limit < 1) {
            throw new ValidationException("limit must be >= 1");
        }
        if (limit > MAX_LIMIT) {
            throw new ValidationException("limit must be <= " + MAX_LIMIT);
        }
        return limit;
    }
}
