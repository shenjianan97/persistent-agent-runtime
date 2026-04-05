import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';

export function useTaskList(status?: string, agentId?: string, pauseReason?: string) {
    return useQuery({
        queryKey: ['tasks', status, agentId, pauseReason],
        queryFn: () => api.listTasks(status || undefined, agentId || undefined, 50, pauseReason || undefined),
        refetchInterval: 3000,
    });
}
