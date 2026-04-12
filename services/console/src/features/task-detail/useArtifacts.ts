import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';

export function useTaskArtifacts(taskId: string, enabled: boolean = true) {
    return useQuery({
        queryKey: ['task-artifacts', taskId],
        queryFn: () => api.listArtifacts(taskId),
        enabled: !!taskId && enabled,
    });
}
