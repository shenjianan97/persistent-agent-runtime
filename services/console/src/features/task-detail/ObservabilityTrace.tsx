import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { TaskObservabilityItemResponse, TaskObservabilityResponse } from '@/types';
import { BrainCircuit, CheckCircle2, Clock3, RotateCcw, Wrench, Zap } from 'lucide-react';
import { formatUsd } from '@/lib/utils';

interface ObservabilityTraceProps {
    observability?: TaskObservabilityResponse;
}

function formatJson(value?: unknown) {
    if (value === undefined || value === null || value === '') {
        return 'No payload recorded.';
    }
    if (typeof value === 'string') {
        try {
            return JSON.stringify(JSON.parse(value), null, 2);
        } catch {
            return value;
        }
    }
    return JSON.stringify(value, null, 2);
}

function itemIcon(item: TaskObservabilityItemResponse) {
    switch (item.kind) {
        case 'llm_span':
            return BrainCircuit;
        case 'tool_span':
            return Wrench;
        case 'completed':
            return CheckCircle2;
        case 'resumed_after_retry':
            return RotateCcw;
        case 'checkpoint_persisted':
        case 'dead_lettered':
        case 'system_span':
        default:
            return Zap;
    }
}

function isSpanKind(kind: TaskObservabilityItemResponse['kind']) {
    return kind === 'llm_span' || kind === 'tool_span' || kind === 'system_span';
}

function kindLabel(kind: TaskObservabilityItemResponse['kind']) {
    switch (kind) {
        case 'llm_span':
            return 'Model call';
        case 'tool_span':
            return 'Tool call';
        case 'system_span':
            return 'Runtime';
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
    const llmCalls = items.filter((item) => item.kind === 'llm_span').length;
    const toolCalls = items.filter((item) => item.kind === 'tool_span').length;
    const checkpoints = items.filter((item) => item.kind === 'checkpoint_persisted').length;
    const parts = [
        llmCalls > 0 ? `${llmCalls} model call${llmCalls === 1 ? '' : 's'}` : null,
        toolCalls > 0 ? `${toolCalls} tool call${toolCalls === 1 ? '' : 's'}` : null,
        checkpoints > 0 ? `${checkpoints} checkpoint${checkpoints === 1 ? '' : 's'}` : null,
    ].filter(Boolean);

    if (parts.length === 0) {
        return null;
    }
    return parts.join(' • ');
}

function SectionLabel({ children }: { children: string }) {
    return (
        <div className="text-[10px] uppercase tracking-[0.22em] text-muted-foreground">
            {children}
        </div>
    );
}

interface ExecutionGroup {
    item: TaskObservabilityItemResponse;
    durableProgress: TaskObservabilityItemResponse[];
}

function itemAnchorTime(item: TaskObservabilityItemResponse) {
    const raw = item.started_at;
    if (!raw) {
        return null;
    }
    return new Date(raw).getTime();
}

function buildExecutionGroups(items: TaskObservabilityItemResponse[]) {
    const groups: ExecutionGroup[] = items
        .filter((item) => item.kind !== 'system_span' && item.kind !== 'checkpoint_persisted')
        .map((item) => ({
            item,
            durableProgress: [],
        }));

    const checkpointItems = items.filter((item) => item.kind === 'checkpoint_persisted');

    for (const checkpoint of checkpointItems) {
        if (groups.length === 0) {
            continue;
        }

        const checkpointTime = itemAnchorTime(checkpoint);
        if (checkpointTime == null) {
            groups[groups.length - 1].durableProgress.push(checkpoint);
            continue;
        }

        let bestIndex = 0;
        let bestDistance = Number.POSITIVE_INFINITY;
        for (let index = 0; index < groups.length; index += 1) {
            const anchor = itemAnchorTime(groups[index].item);
            if (anchor == null) {
                continue;
            }
            const distance = Math.abs(anchor - checkpointTime);
            if (distance < bestDistance || (distance === bestDistance && anchor >= checkpointTime)) {
                bestDistance = distance;
                bestIndex = index;
            }
        }
        groups[bestIndex].durableProgress.push(checkpoint);
    }

    return {
        groups,
        unattachedDurableProgress: groups.length === 0 ? checkpointItems : [],
    };
}

function ExecutionItem({
    item,
    durableProgress = [],
}: {
    item: TaskObservabilityItemResponse;
    durableProgress?: TaskObservabilityItemResponse[];
}) {
    const Icon = itemIcon(item);
    const spanKind = isSpanKind(item.kind);
    const showPayloadDetails = item.kind === 'llm_span' || item.kind === 'tool_span';
    const timestamp = formatTimestamp(item.started_at);

    return (
        <div className="border border-border/40 bg-black/50 p-4 space-y-3">
            <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                <div className="space-y-2">
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
                    {durableProgress.length > 0 && (
                        <div className="inline-flex items-center gap-2 border border-dashed border-border/40 bg-black/20 px-2 py-1 text-[10px] uppercase tracking-widest text-muted-foreground">
                            <span className="font-semibold text-foreground/90">
                                {durableProgress.length === 1
                                    ? '1 durable save recorded after this step.'
                                    : `${durableProgress.length} durable saves recorded after this step.`}
                            </span>
                        </div>
                    )}
                </div>
                <div className="flex flex-wrap gap-3 text-[10px] uppercase tracking-widest text-muted-foreground">
                    {timestamp && <span>{timestamp}</span>}
                    {spanKind && (
                        <>
                            <span>Cost ${formatUsd(item.cost_microdollars)}</span>
                            <span>Tokens {item.total_tokens}</span>
                            <span className="inline-flex items-center gap-1">
                                <Clock3 className="h-3 w-3" />
                                {item.duration_ms ?? 0} ms
                            </span>
                        </>
                    )}
                </div>
            </div>

            {showPayloadDetails && (
                <details className="border border-border/40 bg-black/40">
                    <summary className="cursor-pointer list-none px-3 py-2 text-[10px] uppercase tracking-widest text-muted-foreground">
                        View input and output
                    </summary>
                    <div className="grid gap-3 border-t border-border/40 p-3 md:grid-cols-2">
                        <div>
                            <div className="mb-2 text-[10px] uppercase tracking-widest text-muted-foreground">Input</div>
                            <pre className="max-h-48 overflow-auto border border-border/40 bg-black/60 p-3 text-xs text-muted-foreground whitespace-pre-wrap">
                                {formatJson(item.input)}
                            </pre>
                        </div>
                        <div>
                            <div className="mb-2 text-[10px] uppercase tracking-widest text-muted-foreground">Output</div>
                            <pre className="max-h-48 overflow-auto border border-border/40 bg-black/60 p-3 text-xs text-muted-foreground whitespace-pre-wrap">
                                {formatJson(item.output)}
                            </pre>
                        </div>
                    </div>
                </details>
            )}
        </div>
    );
}

function DurableProgressItem({ item }: { item: TaskObservabilityItemResponse }) {
    const timestamp = formatTimestamp(item.started_at);

    return (
        <div className="flex flex-col gap-2 border border-dashed border-border/40 bg-black/20 px-4 py-3 md:flex-row md:items-center md:justify-between">
            <div className="space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                    <div className="text-xs font-semibold uppercase tracking-widest text-foreground">
                        {item.title}
                    </div>
                    {item.step_number != null && (
                        <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                            Step {item.step_number}
                        </div>
                    )}
                </div>
                <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
                    {item.summary}
                </div>
            </div>
            {timestamp && (
                <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                    {timestamp}
                </div>
            )}
        </div>
    );
}

export function ObservabilityTrace({ observability }: ObservabilityTraceProps) {
    if (!observability) {
        return (
            <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
                <CardHeader className="border-b border-border/40">
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
    const hasTrace = !!observability.trace_id;
    const hasSpanData = (observability.spans ?? []).length > 0;
    const isTerminal = observability.status === 'completed' || observability.status === 'dead_letter' || observability.status === 'cancelled';
    const { groups, unattachedDurableProgress } = buildExecutionGroups(items);
    const hasTracedCalls = items.some((item) => item.kind === 'llm_span' || item.kind === 'tool_span');
    const headline = summaryLine(items);

    return (
        <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
            <CardHeader className="border-b border-border/40">
                <CardTitle className="text-sm font-display uppercase tracking-widest text-muted-foreground">
                    Execution
                </CardTitle>
            </CardHeader>
            <CardContent className="pt-4">
                {items.length === 0 ? (
                    <div className="border border-dashed border-border/40 p-4 text-xs uppercase tracking-widest text-muted-foreground">
                        {!hasTrace && isTerminal && !hasSpanData
                            ? 'No execution trace was recorded for this task.'
                            : 'Awaiting execution details...'}
                    </div>
                ) : (
                    <div className="space-y-4">
                        {observability.trace_id && (
                            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
                                Trace ID: <span className="text-foreground">{observability.trace_id}</span>
                            </div>
                        )}
                        <div className="border border-border/40 bg-black/30 p-3 text-xs uppercase tracking-wider text-muted-foreground">
                            {!hasTrace && isTerminal && !hasSpanData
                                ? 'No traced model or tool calls were recorded for this task. Durable progress is shown below.'
                                : headline ?? 'Execution details captured for this task.'}
                        </div>
                        {groups.length > 0 && (
                            <div className="space-y-3">
                                <SectionLabel>Key steps</SectionLabel>
                                {groups.map((group) => (
                                    <ExecutionItem
                                        key={group.item.item_id}
                                        item={group.item}
                                        durableProgress={hasTracedCalls ? group.durableProgress : []}
                                    />
                                ))}
                            </div>
                        )}
                        {!hasTracedCalls && unattachedDurableProgress.length > 0 && (
                            <div className="space-y-3">
                                <SectionLabel>Durable progress</SectionLabel>
                                {unattachedDurableProgress.map((item) => (
                                    <DurableProgressItem key={item.item_id} item={item} />
                                ))}
                            </div>
                        )}
                        {!hasTracedCalls && groups.length > 0 && groups.some((group) => group.durableProgress.length > 0) && (
                            <div className="space-y-3">
                                <SectionLabel>Durable progress</SectionLabel>
                                {groups.flatMap((group) => group.durableProgress).map((item) => (
                                    <DurableProgressItem key={item.item_id} item={item} />
                                ))}
                            </div>
                        )}
                        {groups.length === 0 && unattachedDurableProgress.length === 0 && (
                            <div className="border border-dashed border-border/40 p-4 text-xs uppercase tracking-widest text-muted-foreground">
                                Awaiting execution details...
                            </div>
                        )}
                    </div>
                )}
            </CardContent>
        </Card>
    );
}
