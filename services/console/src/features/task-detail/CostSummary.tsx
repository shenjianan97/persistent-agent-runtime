import { TaskObservabilityResponse, TaskStatusResponse } from '@/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { DollarSign, Cpu, Timer, Coins } from 'lucide-react';
import { formatUsd } from '@/lib/utils';

interface CostSummaryProps {
    observability?: TaskObservabilityResponse;
    checkpointCount: number;
    totalCostMicrodollars: number;
    task?: TaskStatusResponse;
}

function computeDurationLabel(observability?: TaskObservabilityResponse, task?: TaskStatusResponse): string {
    if (observability?.duration_ms != null) {
        return `${observability.duration_ms}ms`;
    }
    if (task?.created_at && task?.updated_at) {
        const delta = new Date(task.updated_at).getTime() - new Date(task.created_at).getTime();
        if (delta > 0) {
            return `${delta}ms`;
        }
    }
    return 'N/A';
}

export function CostSummary({ observability, checkpointCount, totalCostMicrodollars, task }: CostSummaryProps) {
    const effectiveCost = observability?.total_cost_microdollars ?? totalCostMicrodollars;
    const formattedCost = formatUsd(effectiveCost);

    const durationLabel = computeDurationLabel(observability, task);

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
                        {durationLabel !== 'N/A' ? 'Total runtime' : 'Unavailable'}
                    </p>
                </CardContent>
            </Card>

        </div>
    );
}
