import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';

export function useModels() {
    return useQuery({
        queryKey: ['models'],
        queryFn: () => api.getModels(),
        staleTime: 60 * 1000,
    });
}
