import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';

export function useDeadLetters(agentId?: string, limit?: number) {
    return useQuery({
        queryKey: ['dead-letters', agentId, limit],
        queryFn: () => api.listDeadLetterTasks(agentId, limit),
        refetchInterval: 15000,
    });
}

export function useRedriveTask() {
    const queryClient = useQueryClient();
    return useMutation({
        mutationFn: (taskId: string) => api.redriveTask(taskId),
        onSuccess: (response, taskId) => {
            queryClient.invalidateQueries({ queryKey: ['dead-letters'] });
            queryClient.invalidateQueries({ queryKey: ['tasks'] });
            queryClient.invalidateQueries({ queryKey: ['task', taskId] });
            queryClient.invalidateQueries({ queryKey: ['checkpoints', taskId] });
            if (response.task_id !== taskId) {
                queryClient.invalidateQueries({ queryKey: ['task', response.task_id] });
                queryClient.invalidateQueries({ queryKey: ['checkpoints', response.task_id] });
            }
        },
    });
}
