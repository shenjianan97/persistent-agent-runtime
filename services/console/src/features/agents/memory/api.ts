/**
 * Memory REST client helpers for the Console (Phase 2 Track 5 Task 9).
 *
 * This module re-exports the memory endpoints from the shared `api` singleton
 * in `@/api/client`, scoped to the four endpoints the Memory tab consumes:
 *
 *   - `GET /v1/agents/{agent_id}/memory`            — list + storage stats
 *   - `GET /v1/agents/{agent_id}/memory/search`     — hybrid / text / vector
 *   - `GET /v1/agents/{agent_id}/memory/{memory_id}` — full entry detail
 *   - `DELETE /v1/agents/{agent_id}/memory/{memory_id}` — hard delete
 *
 * The 404-not-403 rule is enforced at the API layer — the Console surfaces any
 * non-2xx as a toast and refetches; it does not branch on specific status codes.
 */
import { api } from '@/api/client';
import type { MemoryEntryResponse, MemoryListResponse, MemorySearchResponse } from '@/types';

export type MemoryOutcomeFilter = 'all' | 'succeeded' | 'failed';

export interface MemoryListParams {
    outcome?: MemoryOutcomeFilter;
    /** ISO-8601 lower bound on created_at (inclusive). */
    from?: string;
    /** ISO-8601 upper bound on created_at (inclusive). */
    to?: string;
    limit?: number;
    /** Opaque cursor from a previous list response; absent on the first page. */
    cursor?: string;
}

export interface MemorySearchParams {
    mode?: 'hybrid' | 'text' | 'vector';
    limit?: number;
    outcome?: MemoryOutcomeFilter;
    from?: string;
    to?: string;
}

function toOutcomeParam(outcome?: MemoryOutcomeFilter): string | undefined {
    if (!outcome || outcome === 'all') return undefined;
    return outcome;
}

export function listAgentMemory(
    agentId: string,
    params?: MemoryListParams
): Promise<MemoryListResponse> {
    return api.listAgentMemory(agentId, {
        outcome: toOutcomeParam(params?.outcome),
        from: params?.from,
        to: params?.to,
        limit: params?.limit,
        cursor: params?.cursor,
    });
}

export function searchAgentMemory(
    agentId: string,
    query: string,
    params?: MemorySearchParams
): Promise<MemorySearchResponse> {
    return api.searchAgentMemory(agentId, query, {
        mode: params?.mode,
        limit: params?.limit,
        outcome: toOutcomeParam(params?.outcome),
        from: params?.from,
        to: params?.to,
    });
}

export function getAgentMemoryEntry(
    agentId: string,
    memoryId: string
): Promise<MemoryEntryResponse> {
    return api.getAgentMemoryEntry(agentId, memoryId);
}

export function deleteAgentMemoryEntry(agentId: string, memoryId: string): Promise<void> {
    return api.deleteAgentMemoryEntry(agentId, memoryId);
}
