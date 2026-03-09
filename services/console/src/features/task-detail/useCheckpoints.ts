import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';
import { TaskStatus } from '@/types';

export function useCheckpoints(taskId: string, taskStatus?: TaskStatus) {
    return useQuery({
        queryKey: ['checkpoints', taskId],
        queryFn: () => api.getCheckpoints(taskId),
        refetchInterval: () => {
            if (taskStatus === 'running') {
                return 3000;
            }
            return false;
        },
        enabled: !!taskId,
    });
}
