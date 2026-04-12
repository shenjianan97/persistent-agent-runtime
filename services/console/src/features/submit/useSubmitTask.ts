import { useMutation } from '@tanstack/react-query';
import { api } from '@/api/client';
import { TaskSubmissionRequest } from '@/types';

interface SubmitTaskInput {
    request: TaskSubmissionRequest;
    files?: File[];
}

export function useSubmitTask() {
    return useMutation({
        mutationFn: ({ request, files }: SubmitTaskInput) => {
            if (files && files.length > 0) {
                return api.submitTaskMultipart(request, files);
            }
            return api.submitTask(request);
        },
    });
}
