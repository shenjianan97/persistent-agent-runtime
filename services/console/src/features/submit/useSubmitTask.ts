import { useMutation } from '@tanstack/react-query';
import { api } from '@/api/client';
import { TaskSubmissionRequest } from '@/types';

export function useSubmitTask() {
    return useMutation({
        mutationFn: (request: TaskSubmissionRequest) => api.submitTask(request),
    });
}
