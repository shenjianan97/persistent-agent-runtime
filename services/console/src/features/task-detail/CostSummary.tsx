import { TaskEventResponse, TaskObservabilityResponse, TaskStatusResponse } from '@/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { DollarSign, Cpu, Timer, Coins } from 'lucide-react';
import { formatUsd } from '@/lib/utils';

interface CostSummaryProps {
    observability?: TaskObservabilityResponse;
    checkpointCount: number;
    totalCostMicrodollars: number;
    task?: TaskStatusResponse;
    hitlEvents?: TaskEventResponse[];
}

function formatDuration(ms: number): string {
    if (ms < 1000) return `${ms}ms`;
    const seconds = ms / 1000;
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = Math.round(seconds % 60);
    return `${minutes}m ${remainingSeconds}s`;
}

function computePausedMs(events: TaskEventResponse[]): number {
    let pausedMs = 0;
    let pausedAt: number | null = null;
    for (const ev of events) {
        if (ev.event_type === 'task_paused') {
            pausedAt = new Date(ev.created_at).getTime();
        } else if (ev.event_type === 'task_resumed' && pausedAt !== null) {
            pausedMs += new Date(ev.created_at).getTime() - pausedAt;
            pausedAt = null;
        }
    }
    return pausedMs;
}

function computeDurationLabel(observability?: TaskObservabilityResponse, task?: TaskStatusResponse, hitlEvents?: TaskEventResponse[]): string {
    let rawMs: number | null = null;
    if (observability?.duration_ms != null) {
        rawMs = observability.duration_ms;
    } else if (task?.created_at && task?.updated_at) {
        const delta = new Date(task.updated_at).getTime() - new Date(task.created_at).getTime();
        if (delta > 0) rawMs = delta;
    }
    if (rawMs == null) return 'N/A';

    const pausedMs = hitlEvents?.length ? computePausedMs(hitlEvents) : 0;
    return formatDuration(Math.max(0, rawMs - pausedMs));
}

export function CostSummary({ observability, checkpointCount, totalCostMicrodollars, task, hitlEvents }: CostSummaryProps) {
    const effectiveCost = observability?.total_cost_microdollars ?? totalCostMicrodollars;
    const formattedCost = formatUsd(effectiveCost);

    const durationLabel = computeDurationLabel(observability, task, hitlEvents);

    return (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-6">
            <Card className="console-surface border-white/10">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-success">Total Cost</CardTitle>
                    <DollarSign className="h-4 w-4 text-success" />
                </CardHeader>
                <CardContent>
                    <div className="text-3xl font-display font-medium text-success drop-shadow-[0_0_8px_var(--color-success)]">
                        ${formattedCost}
                    </div>
                    <p className="text-xs text-muted-foreground mt-1 tracking-wider uppercase">USD</p>
                </CardContent>
            </Card>

            <Card className="console-surface border-white/10">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Checkpoints</CardTitle>
                    <Cpu className="h-4 w-4 text-primary" />
                </CardHeader>
                <CardContent>
                    <div className="text-3xl font-display font-medium text-primary drop-shadow-[0_0_8px_var(--color-primary)]">
                        {checkpointCount}
                    </div>
                    <p className="text-xs text-muted-foreground mt-1 tracking-wider uppercase">Saved states</p>
                </CardContent>
            </Card>

            <Card className="console-surface border-white/10">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Tokens</CardTitle>
                    <Coins className="h-4 w-4 text-primary" />
                </CardHeader>
                <CardContent>
                    <div className="text-3xl font-display font-medium text-primary drop-shadow-[0_0_8px_var(--color-primary)]">
                        {observability?.total_tokens ?? 0}
                    </div>
                    <p className="text-xs text-muted-foreground mt-1 tracking-wider uppercase">Prompt + completion</p>
                </CardContent>
            </Card>

            <Card className="console-surface border-white/10">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Duration</CardTitle>
                    <Timer className="h-4 w-4 text-primary" />
                </CardHeader>
                <CardContent>
                    <div className="text-3xl font-display font-medium text-primary drop-shadow-[0_0_8px_var(--color-primary)]">
                        {durationLabel}
                    </div>
                    <p className="text-xs text-muted-foreground mt-1 tracking-wider uppercase">
                        {durationLabel !== 'N/A' ? 'Execution time' : 'Unavailable'}
                    </p>
                </CardContent>
            </Card>

        </div>
    );
}
