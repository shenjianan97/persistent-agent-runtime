import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';
import { AgentCreateRequest, AgentUpdateRequest } from '@/types';

const AGENTS_KEY = ['agents'];

export function useAgents(status?: string) {
    return useQuery({
        queryKey: ['agents', status],
        queryFn: () => api.listAgents(status),
    });
}

export function useAgent(agentId: string) {
    return useQuery({
        queryKey: ['agent', agentId],
        queryFn: () => api.getAgent(agentId),
        enabled: !!agentId,
        staleTime: 30_000,
    });
}

export function useCreateAgent() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: (request: AgentCreateRequest) => api.createAgent(request),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: AGENTS_KEY });
        },
    });
}

export function useUpdateAgent() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: ({ agentId, request }: { agentId: string; request: AgentUpdateRequest }) =>
            api.updateAgent(agentId, request),
        onSuccess: (_data, variables) => {
            queryClient.invalidateQueries({ queryKey: AGENTS_KEY });
            queryClient.invalidateQueries({ queryKey: ['agent', variables.agentId] });
        },
    });
}
