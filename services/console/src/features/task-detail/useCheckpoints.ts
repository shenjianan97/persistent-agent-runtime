import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';
import { TaskStatus } from '@/types';

export function useCheckpoints(taskId: string, taskStatus?: TaskStatus, expectedCheckpointCount?: number) {
    return useQuery({
        queryKey: ['checkpoints', taskId],
        queryFn: () => api.getCheckpoints(taskId),
        refetchInterval: (query) => {
            if (taskStatus === 'running' || taskStatus === 'queued') {
                return 3000;
            }

            const loadedCheckpointCount = query.state.data?.checkpoints?.length ?? 0;
            if ((expectedCheckpointCount ?? 0) > loadedCheckpointCount) {
                return 1000;
            }

            return false;
        },
        enabled: !!taskId,
    });
}
