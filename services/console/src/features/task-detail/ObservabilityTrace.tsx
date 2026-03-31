import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { TaskObservabilityItemResponse, TaskObservabilityResponse } from '@/types';
import { CheckCircle2, RotateCcw, XCircle, Zap } from 'lucide-react';
import { formatUsd } from '@/lib/utils';

interface ObservabilityTraceProps {
    observability?: TaskObservabilityResponse;
}

function formatTimestamp(value?: string | null) {
    if (!value) return null;
    return new Date(value).toLocaleTimeString();
}

/** Only show items that carry meaningful info for customers */
function isVisible(item: TaskObservabilityItemResponse) {
    // Always show terminal and retry events
    if (item.kind === 'completed' || item.kind === 'dead_lettered' || item.kind === 'resumed_after_retry') {
        return true;
    }
    // Only show checkpoints that have LLM data (model, tokens, or cost)
    return item.total_tokens > 0 || item.cost_microdollars > 0 || !!item.model_name;
}

function LlmStepItem({ item }: { item: TaskObservabilityItemResponse }) {
    const timestamp = formatTimestamp(item.started_at);
    const costLabel = item.cost_microdollars > 0 ? `$${formatUsd(item.cost_microdollars)}` : null;

    return (
        <div className="flex items-center justify-between border border-border/40 bg-black/50 px-4 py-3">
            <div className="flex items-center gap-4">
                <Zap className="h-3.5 w-3.5 text-primary shrink-0" />
                <span className="text-sm font-semibold text-foreground">
                    {item.model_name ?? 'LLM Call'}
                </span>
                {item.total_tokens > 0 && (
                    <span className="text-xs font-mono text-muted-foreground">
                        {item.input_tokens ?? 0} in / {item.output_tokens ?? 0} out
                    </span>
                )}
                {costLabel && (
                    <span className="text-xs font-mono font-semibold text-success">{costLabel}</span>
                )}
            </div>
            {timestamp && <span className="font-mono text-[10px] text-muted-foreground">{timestamp}</span>}
        </div>
    );
}

function TerminalItem({ item }: { item: TaskObservabilityItemResponse }) {
    const isCompleted = item.kind === 'completed';
    const Icon = isCompleted ? CheckCircle2 : XCircle;
    const colorClass = isCompleted ? 'text-success' : 'text-destructive';
    const timestamp = formatTimestamp(item.started_at);

    return (
        <div className="flex items-center justify-between border border-border/40 bg-black/50 px-4 py-3">
            <div className="flex items-center gap-4">
                <Icon className={`h-3.5 w-3.5 ${colorClass} shrink-0`} />
                <span className={`text-sm font-semibold uppercase tracking-widest ${colorClass}`}>
                    {isCompleted ? 'Completed' : 'Failed'}
                </span>
            </div>
            {timestamp && <span className="font-mono text-[10px] text-muted-foreground">{timestamp}</span>}
        </div>
    );
}

function RetryItem({ item }: { item: TaskObservabilityItemResponse }) {
    const timestamp = formatTimestamp(item.started_at);
    return (
        <div className="flex items-center justify-between border border-border/40 bg-black/50 px-4 py-3">
            <div className="flex items-center gap-4">
                <RotateCcw className="h-3.5 w-3.5 text-yellow-500 shrink-0" />
                <span className="text-sm font-semibold uppercase tracking-widest text-yellow-500">
                    Retry
                </span>
                <span className="text-xs text-muted-foreground">{item.summary}</span>
            </div>
            {timestamp && <span className="font-mono text-[10px] text-muted-foreground">{timestamp}</span>}
        </div>
    );
}

function TimelineItem({ item }: { item: TaskObservabilityItemResponse }) {
    if (item.kind === 'completed' || item.kind === 'dead_lettered') {
        return <TerminalItem item={item} />;
    }
    if (item.kind === 'resumed_after_retry') {
        return <RetryItem item={item} />;
    }
    return <LlmStepItem item={item} />;
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

    const items = (observability.items ?? []).filter(isVisible);
    const isTerminal = observability.status === 'completed' || observability.status === 'dead_letter' || observability.status === 'cancelled';

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
                    <div className="space-y-1">
                        {items.map((item) => (
                            <TimelineItem key={item.item_id} item={item} />
                        ))}
                    </div>
                )}
            </CardContent>
        </Card>
    );
}
