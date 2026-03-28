import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';
import { TaskStatus } from '@/types';

export function useTaskObservability(taskId: string, taskStatus?: TaskStatus) {
    return useQuery({
        queryKey: ['task-observability', taskId],
        queryFn: () => api.getTaskObservability(taskId),
        refetchInterval: (query) => {
            const status = taskStatus ?? query.state.data?.status;
            if (status === 'queued' || status === 'running') {
                return 3000;
            }
            return false;
        },
        enabled: !!taskId,
        refetchOnWindowFocus: true,
    });
}
