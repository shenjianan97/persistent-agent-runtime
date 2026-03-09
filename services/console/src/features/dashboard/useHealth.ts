import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';

export function useHealth() {
    return useQuery({
        queryKey: ['health'],
        queryFn: api.getHealth,
        refetchInterval: 10000,
    });
}
