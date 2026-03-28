import { useMemo } from 'react';
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
        const completed = completedTasks.slice(0, RECENT_RUN_LIMIT);
        const recentCostMicrodollars = completedTasks.reduce(
            (total, task) => total + task.total_cost_microdollars,
            0,
        );

        return {
            inProgress,
            completed,
            recentCostMicrodollars,
            inProgressCount: inProgressTasks.length,
            deadLetterCount: deadLetters.length,
            completedCount: completedTasks.length,
        };
    }, [deadLetters.length, tasks]);

    return {
        isLoading: tasksQuery.isLoading || deadLettersQuery.isLoading,
        isError: tasksQuery.isError || deadLettersQuery.isError,
        inProgress: derived.inProgress,
        recentRuns: derived.completed,
        deadLetters,
        summary: {
            inProgressCount: derived.inProgressCount,
            deadLetterCount: derived.deadLetterCount,
            completedCount: derived.completedCount,
            recentCostMicrodollars: derived.recentCostMicrodollars,
        },
    };
}
