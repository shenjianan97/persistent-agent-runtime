import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';

export function useTaskStatus(taskId: string) {
    return useQuery({
        queryKey: ['task', taskId],
        queryFn: () => api.getTaskStatus(taskId),
        refetchInterval: (query) => {
            const status = query.state.data?.status;
            if (status === 'queued' || status === 'running' ||
                status === 'waiting_for_approval' || status === 'waiting_for_input' || status === 'paused') {
                return 2000;
            }
            return false; // Terminal state
        },
        enabled: !!taskId,
    });
}

export function useCancelTask() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: (taskId: string) => api.cancelTask(taskId),
        onSuccess: (_, taskId) => {
            queryClient.invalidateQueries({ queryKey: ['task', taskId] });
        },
    });
}
