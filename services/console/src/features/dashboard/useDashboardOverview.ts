import { useMemo } from 'react';
import { useQueries } from '@tanstack/react-query';

import { api } from '@/api/client';
import { useTaskList } from '@/features/task-list/useTaskList';
import { useDeadLetters } from '@/features/dead-letter/useDeadLetter';
import { TaskSummaryResponse } from '@/types';

const RECENT_RUN_LIMIT = 5;
const DEAD_LETTER_PREVIEW_LIMIT = 5;

function byNewest(a: TaskSummaryResponse, b: TaskSummaryResponse) {
    return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
}

export function useDashboardOverview() {
    const tasksQuery = useTaskList(undefined, undefined);
    const deadLettersQuery = useDeadLetters(undefined, DEAD_LETTER_PREVIEW_LIMIT);

    const tasks = tasksQuery.data?.items ?? [];
    const deadLetters = deadLettersQuery.data?.items ?? [];

    const derived = useMemo(() => {
        const inProgressTasks = tasks
            .filter((task) => task.status === 'queued' || task.status === 'running')
            .sort(byNewest);
        const completedTasks = tasks
            .filter((task) => task.status === 'completed')
            .sort(byNewest);
        const inProgress = inProgressTasks.slice(0, RECENT_RUN_LIMIT);
        const recentCompleted = completedTasks.slice(0, RECENT_RUN_LIMIT);

        return {
            inProgress,
            recentCompleted,
            inProgressCount: inProgressTasks.length,
            deadLetterCount: deadLetters.length,
            completedCount: completedTasks.length,
        };
    }, [deadLetters.length, tasks]);

    const recentCompletedQueries = useQueries({
        queries: derived.recentCompleted.map((task) => ({
            queryKey: ['task', task.task_id, 'dashboard-cost'],
            queryFn: () => api.getTaskStatus(task.task_id),
            enabled: task.status === 'completed',
            staleTime: 30_000,
        })),
    });

    const recentRuns = useMemo(() => {
        return derived.recentCompleted.map((task, index) => {
            const detail = recentCompletedQueries[index]?.data;
            if (!detail) {
                return task;
            }

            return {
                ...task,
                total_cost_microdollars: detail.total_cost_microdollars,
            };
        });
    }, [derived.recentCompleted, recentCompletedQueries]);

    const recentCostMicrodollars = useMemo(
        () => recentRuns.reduce((total, task) => total + task.total_cost_microdollars, 0),
        [recentRuns],
    );

    return {
        isLoading: tasksQuery.isLoading || deadLettersQuery.isLoading,
        isError: tasksQuery.isError || deadLettersQuery.isError,
        inProgress: derived.inProgress,
        recentRuns,
        deadLetters,
        summary: {
            inProgressCount: derived.inProgressCount,
            deadLetterCount: derived.deadLetterCount,
            completedCount: derived.completedCount,
            recentCostMicrodollars,
        },
    };
}
