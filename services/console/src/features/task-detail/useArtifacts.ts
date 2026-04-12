import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';

export function useTaskArtifacts(taskId: string, taskStatus?: string) {
    return useQuery({
        queryKey: ['task-artifacts', taskId, taskStatus],
        queryFn: () => api.listArtifacts(taskId),
        enabled: !!taskId && !!taskStatus,
    });
}
