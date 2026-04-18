import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';
import type {
    MemoryListResponse,
    MemorySearchResponse,
    MemoryEntryResponse,
} from '@/types';

/**
 * Memory data hooks for the Agent Memory tab (Task 9) and the Submit-page
 * attach widget (Task 10). Phase 2 Track 5.
 *
 * The hooks are thin wrappers over `src/api/client.ts` — auth, tenant/agent
 * scoping, and 404-not-403 disclosure are enforced server-side.
 *
 * Task 10 primarily consumes `useAgentMemoryList`, `useAgentMemorySearch`, and
 * `useAgentMemoryDetail`. Task 9 additionally uses `useDeleteAgentMemoryEntry`.
 * Creating this module under `features/agents/memory/hooks.ts` matches the
 * import path documented in the Task 10 spec.
 */

export type MemoryOutcomeFilter = 'all' | 'succeeded' | 'failed';

export interface MemoryListFilters {
    outcome?: MemoryOutcomeFilter;
    /** ISO-8601 lower bound on created_at (inclusive). */
    from?: string;
    /** ISO-8601 upper bound on created_at (inclusive). */
    to?: string;
}

/** Stable query keys, exported so tests and consumers can invalidate precisely. */
const MEMORY_ROOT_KEY = 'agent-memory';

export function agentMemoryListKey(
    agentId: string,
    filters?: MemoryListFilters,
    cursor?: string
) {
    return [
        MEMORY_ROOT_KEY,
        'list',
        agentId,
        filters?.outcome ?? 'all',
        filters?.from ?? '',
        filters?.to ?? '',
        cursor ?? '',
    ] as const;
}

export function agentMemorySearchKey(
    agentId: string,
    query: string,
    filters?: MemoryListFilters
) {
    return [
        MEMORY_ROOT_KEY,
        'search',
        agentId,
        query,
        filters?.outcome ?? 'all',
        filters?.from ?? '',
        filters?.to ?? '',
    ] as const;
}

export function agentMemoryDetailKey(agentId: string, memoryId: string) {
    return [MEMORY_ROOT_KEY, 'detail', agentId, memoryId] as const;
}

function toOutcomeParam(outcome?: MemoryOutcomeFilter): string | undefined {
    if (!outcome || outcome === 'all') return undefined;
    return outcome;
}

/**
 * Paginated list of memory entries for an agent. First page includes
 * `agent_storage_stats`. Disabled until `enabled` AND `agentId` are truthy —
 * Task 10 uses this to gate the fetch on the attach-widget being expanded.
 */
export function useAgentMemoryList(
    agentId: string,
    opts?: {
        filters?: MemoryListFilters;
        cursor?: string;
        limit?: number;
        enabled?: boolean;
    }
) {
    const { filters, cursor, limit, enabled = true } = opts ?? {};
    return useQuery<MemoryListResponse>({
        queryKey: agentMemoryListKey(agentId, filters, cursor),
        queryFn: () =>
            api.listAgentMemory(agentId, {
                outcome: toOutcomeParam(filters?.outcome),
                from: filters?.from,
                to: filters?.to,
                limit,
                cursor,
            }),
        enabled: enabled && !!agentId,
        staleTime: 10_000,
    });
}

/**
 * Hybrid / text / vector search over the agent's memory. Enabled only when
 * `query` is non-empty — the picker uses the list endpoint when the search
 * box is blank.
 */
export function useAgentMemorySearch(
    agentId: string,
    query: string,
    opts?: {
        filters?: MemoryListFilters;
        mode?: 'hybrid' | 'text' | 'vector';
        limit?: number;
        enabled?: boolean;
    }
) {
    const { filters, mode, limit, enabled = true } = opts ?? {};
    const trimmed = query.trim();
    return useQuery<MemorySearchResponse>({
        queryKey: agentMemorySearchKey(agentId, trimmed, filters),
        queryFn: () =>
            api.searchAgentMemory(agentId, trimmed, {
                mode,
                limit,
                outcome: toOutcomeParam(filters?.outcome),
                from: filters?.from,
                to: filters?.to,
            }),
        enabled: enabled && !!agentId && trimmed.length > 0,
        staleTime: 5_000,
    });
}

/**
 * Full detail for one entry. Used for deep-link pre-selection (fetch the
 * entry that `?attachMemoryId=` references) and by the token-footprint
 * indicator to read `summary` + `observations` lengths.
 */
export function useAgentMemoryDetail(
    agentId: string,
    memoryId: string | null | undefined,
    opts?: { enabled?: boolean }
) {
    const { enabled = true } = opts ?? {};
    return useQuery<MemoryEntryResponse>({
        queryKey: agentMemoryDetailKey(agentId, memoryId ?? ''),
        queryFn: () => api.getAgentMemoryEntry(agentId, memoryId!),
        enabled: enabled && !!agentId && !!memoryId,
        staleTime: 60_000,
    });
}

/**
 * Hard-delete a memory entry. On success, invalidates every list/search/detail
 * query for this agent.
 */
export function useDeleteAgentMemoryEntry(agentId: string) {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: (memoryId: string) => api.deleteAgentMemoryEntry(agentId, memoryId),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: [MEMORY_ROOT_KEY] });
        },
    });
}
