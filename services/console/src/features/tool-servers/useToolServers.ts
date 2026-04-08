import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';
import type { ToolServerCreateRequest, ToolServerUpdateRequest } from '@/types';

const TOOL_SERVERS_KEY = ['tool-servers'];

export function useToolServers(status?: string) {
    return useQuery({
        queryKey: [...TOOL_SERVERS_KEY, status],
        queryFn: () => api.listToolServers(status),
    });
}

export function useToolServer(serverId: string) {
    return useQuery({
        queryKey: ['tool-server', serverId],
        queryFn: () => api.getToolServer(serverId),
        enabled: !!serverId,
        staleTime: 30_000,
    });
}

export function useCreateToolServer() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: (request: ToolServerCreateRequest) => api.createToolServer(request),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: TOOL_SERVERS_KEY });
        },
    });
}

export function useUpdateToolServer() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: ({ serverId, request }: { serverId: string; request: ToolServerUpdateRequest }) =>
            api.updateToolServer(serverId, request),
        onSuccess: (_data, variables) => {
            queryClient.invalidateQueries({ queryKey: TOOL_SERVERS_KEY });
            queryClient.invalidateQueries({ queryKey: ['tool-server', variables.serverId] });
        },
    });
}

export function useDeleteToolServer() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: (serverId: string) => api.deleteToolServer(serverId),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: TOOL_SERVERS_KEY });
        },
    });
}

export function useDiscoverToolServer() {
    return useMutation({
        mutationFn: (serverId: string) => api.discoverToolServer(serverId),
    });
}
