import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';
import { useTaskStatus } from './useTaskStatus';

export function useCheckpoints(taskId: string) {
    const { data: task } = useTaskStatus(taskId);

    return useQuery({
        queryKey: ['checkpoints', taskId],
        queryFn: () => api.getCheckpoints(taskId),
        refetchInterval: () => {
            if (task?.status === 'running') {
                return 3000;
            }
            return false;
        },
        enabled: !!taskId,
    });
}
