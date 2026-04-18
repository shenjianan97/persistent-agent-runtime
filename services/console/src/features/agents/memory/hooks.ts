import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    deleteAgentMemoryEntry,
    getAgentMemoryEntry,
    listAgentMemory,
    searchAgentMemory,
    type MemoryListParams,
    type MemorySearchParams,
} from './api';

const MEMORY_ROOT_KEY = 'agent-memory';

/** Stable query key for list-mode pages — all tuned by filter state. */
export function agentMemoryListKey(agentId: string, params?: MemoryListParams) {
    return [
        MEMORY_ROOT_KEY,
        'list',
        agentId,
        params?.outcome ?? 'all',
        params?.from ?? '',
        params?.to ?? '',
        params?.limit ?? '',
        params?.cursor ?? '',
    ] as const;
}

export function agentMemorySearchKey(
    agentId: string,
    query: string,
    params?: MemorySearchParams
) {
    return [
        MEMORY_ROOT_KEY,
        'search',
        agentId,
        query,
        params?.mode ?? 'hybrid',
        params?.limit ?? '',
        params?.outcome ?? 'all',
        params?.from ?? '',
        params?.to ?? '',
    ] as const;
}

export function agentMemoryDetailKey(agentId: string, memoryId: string) {
    return [MEMORY_ROOT_KEY, 'detail', agentId, memoryId] as const;
}

/**
 * List memory entries for an agent. Pass `enabled: false` via the caller when
 * the tab isn't active yet — React Query won't hit the API.
 */
export function useAgentMemoryList(
    agentId: string,
    params?: MemoryListParams,
    options?: { enabled?: boolean }
) {
    return useQuery({
        queryKey: agentMemoryListKey(agentId, params),
        queryFn: () => listAgentMemory(agentId, params),
        enabled: (options?.enabled ?? true) && !!agentId,
    });
}

/**
 * Hybrid search over an agent's memory. Enabled only when `query` is non-empty.
 * Empty query means "not in search mode" — the caller should show the list view.
 */
export function useAgentMemorySearch(
    agentId: string,
    query: string,
    params?: MemorySearchParams,
    options?: { enabled?: boolean }
) {
    const trimmed = query.trim();
    return useQuery({
        queryKey: agentMemorySearchKey(agentId, trimmed, params),
        queryFn: () => searchAgentMemory(agentId, trimmed, params),
        enabled: (options?.enabled ?? true) && !!agentId && trimmed.length > 0,
    });
}

export function useAgentMemoryDetail(
    agentId: string,
    memoryId: string | null | undefined,
    options?: { enabled?: boolean }
) {
    return useQuery({
        queryKey: agentMemoryDetailKey(agentId, memoryId ?? ''),
        queryFn: () => getAgentMemoryEntry(agentId, memoryId!),
        enabled: (options?.enabled ?? true) && !!agentId && !!memoryId,
        staleTime: 30_000,
    });
}

/**
 * Delete mutation. On success, invalidates every list/search/detail query for
 * this agent so the removed entry disappears from the UI on the next render.
 */
export function useDeleteAgentMemoryEntry(agentId: string) {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: (memoryId: string) => deleteAgentMemoryEntry(agentId, memoryId),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: [MEMORY_ROOT_KEY] });
        },
    });
}
