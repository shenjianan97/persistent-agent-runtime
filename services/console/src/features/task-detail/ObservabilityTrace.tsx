import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { TaskObservabilityItemResponse, TaskObservabilityResponse } from '@/types';
import { CheckCircle2, RotateCcw, Zap } from 'lucide-react';
import { formatUsd } from '@/lib/utils';

interface ObservabilityTraceProps {
    observability?: TaskObservabilityResponse;
}

function itemIcon(item: TaskObservabilityItemResponse) {
    switch (item.kind) {
        case 'completed':
            return CheckCircle2;
        case 'resumed_after_retry':
            return RotateCcw;
        case 'checkpoint_persisted':
        case 'dead_lettered':
        default:
            return Zap;
    }
}

function kindLabel(kind: TaskObservabilityItemResponse['kind']) {
    switch (kind) {
        case 'checkpoint_persisted':
            return 'Checkpoint';
        case 'resumed_after_retry':
            return 'Retry';
        case 'completed':
            return 'Completed';
        case 'dead_lettered':
            return 'Failed';
    }
}

function formatTimestamp(value?: string | null) {
    if (!value) {
        return null;
    }
    return new Date(value).toLocaleString();
}

function summaryLine(items: TaskObservabilityItemResponse[]) {
    const checkpoints = items.filter((item) => item.kind === 'checkpoint_persisted').length;
    const retries = items.filter((item) => item.kind === 'resumed_after_retry').length;
    const parts = [
        checkpoints > 0 ? `${checkpoints} checkpoint${checkpoints === 1 ? '' : 's'}` : null,
        retries > 0 ? `${retries} retr${retries === 1 ? 'y' : 'ies'}` : null,
    ].filter(Boolean);

    if (parts.length === 0) {
        return null;
    }
    return parts.join(' \u2022 ');
}

function CheckpointItem({ item }: { item: TaskObservabilityItemResponse }) {
    const Icon = itemIcon(item);
    const timestamp = formatTimestamp(item.started_at);

    return (
        <div className="flex flex-col gap-2 border border-border/40 bg-black/50 px-4 py-3 md:flex-row md:items-center md:justify-between">
            <div className="space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                    <Icon className="h-4 w-4 text-primary" />
                    <div className="text-sm font-semibold uppercase tracking-widest text-foreground">
                        {item.title}
                    </div>
                    <div className="border border-border/40 px-2 py-0.5 text-[10px] uppercase tracking-widest text-muted-foreground">
                        {kindLabel(item.kind)}
                    </div>
                    {item.step_number != null && (
                        <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                            Step {item.step_number}
                        </div>
                    )}
                </div>
                <div className="text-xs uppercase tracking-wider text-muted-foreground">
                    {item.summary}
                </div>
            </div>
            <div className="flex flex-wrap gap-3 text-[10px] uppercase tracking-widest text-muted-foreground">
                {timestamp && <span>{timestamp}</span>}
                {item.cost_microdollars > 0 && <span>Cost ${formatUsd(item.cost_microdollars)}</span>}
                {item.total_tokens > 0 && (
                    <span>
                        {item.input_tokens ?? 0} in / {item.output_tokens ?? 0} out
                    </span>
                )}
            </div>
        </div>
    );
}

export function ObservabilityTrace({ observability }: ObservabilityTraceProps) {
    if (!observability) {
        return (
            <Card className="console-surface border-white/10">
                <CardHeader className="border-b border-white/8">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-muted-foreground">
                        Execution
                    </CardTitle>
                </CardHeader>
                <CardContent className="pt-4">
                    <div className="border border-dashed border-border/40 p-4 text-xs uppercase tracking-widest text-muted-foreground">
                        Loading execution details...
                    </div>
                </CardContent>
            </Card>
        );
    }

    const items = observability.items ?? [];
    const isTerminal = observability.status === 'completed' || observability.status === 'dead_letter' || observability.status === 'cancelled';
    const headline = summaryLine(items);

    return (
        <Card className="console-surface border-white/10">
            <CardHeader className="border-b border-white/8">
                <CardTitle className="text-sm font-display uppercase tracking-widest text-muted-foreground">
                    Execution
                </CardTitle>
            </CardHeader>
            <CardContent className="pt-4">
                {items.length === 0 ? (
                    <div className="border border-dashed border-border/40 p-4 text-xs uppercase tracking-widest text-muted-foreground">
                        {isTerminal
                            ? 'No execution trace was recorded for this task.'
                            : 'Awaiting execution details...'}
                    </div>
                ) : (
                    <div className="space-y-4">
                        <div className="border border-border/40 bg-black/30 p-3 text-xs uppercase tracking-wider text-muted-foreground">
                            {headline ?? 'Execution details captured for this task.'}
                        </div>
                        <div className="space-y-3">
                            {items.map((item) => (
                                <CheckpointItem key={item.item_id} item={item} />
                            ))}
                        </div>
                    </div>
                )}
            </CardContent>
        </Card>
    );
}
