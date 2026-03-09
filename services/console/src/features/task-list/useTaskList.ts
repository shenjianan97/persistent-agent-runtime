import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';

export function useTaskList(status?: string, agentId?: string) {
    return useQuery({
        queryKey: ['tasks', status, agentId],
        queryFn: () => api.listTasks(status || undefined, agentId || undefined, 50),
        refetchInterval: 3000,
    });
}
