import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';
import { LangfuseEndpointRequest } from '@/types';

const ENDPOINTS_KEY = ['langfuse-endpoints'];

export function useLangfuseEndpoints() {
    return useQuery({
        queryKey: ENDPOINTS_KEY,
        queryFn: () => api.listLangfuseEndpoints(),
    });
}

export function useCreateLangfuseEndpoint() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: (request: LangfuseEndpointRequest) => api.createLangfuseEndpoint(request),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ENDPOINTS_KEY });
        },
    });
}

export function useUpdateLangfuseEndpoint() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: ({ endpointId, request }: { endpointId: string; request: LangfuseEndpointRequest }) =>
            api.updateLangfuseEndpoint(endpointId, request),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ENDPOINTS_KEY });
        },
    });
}

export function useDeleteLangfuseEndpoint() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: (endpointId: string) => api.deleteLangfuseEndpoint(endpointId),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ENDPOINTS_KEY });
        },
    });
}

export function useTestLangfuseEndpoint() {
    return useMutation({
        mutationFn: (endpointId: string) => api.testLangfuseEndpoint(endpointId),
    });
}
