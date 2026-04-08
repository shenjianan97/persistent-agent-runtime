import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';
import { TaskStatus } from '@/types';

export function getCheckpointRefetchInterval(
    taskStatus: TaskStatus | undefined,
    expectedCheckpointCount: number | undefined,
    loadedCheckpointCount: number,
) {
    // Refetch while the task is actively executing or could resume at any moment
    if (taskStatus === 'running' || taskStatus === 'queued' ||
        taskStatus === 'paused' || taskStatus === 'waiting_for_input' ||
        taskStatus === 'waiting_for_approval') {
        return 3000;
    }

    // Task reached a terminal state but we haven't loaded all checkpoints yet
    if ((expectedCheckpointCount ?? 0) > loadedCheckpointCount) {
        return 1000;
    }

    return false;
}

export function useCheckpoints(taskId: string, taskStatus?: TaskStatus, expectedCheckpointCount?: number) {
    return useQuery({
        queryKey: ['checkpoints', taskId],
        queryFn: () => api.getCheckpoints(taskId),
        refetchInterval: (query) => {
            const loadedCheckpointCount = query.state.data?.checkpoints?.length ?? 0;
            return getCheckpointRefetchInterval(taskStatus, expectedCheckpointCount, loadedCheckpointCount);
        },
        enabled: !!taskId,
        refetchOnWindowFocus: true,
    });
}
