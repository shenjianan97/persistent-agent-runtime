import { CheckpointEvent, CheckpointResponse } from '@/types';
import { ScrollArea } from '@/components/ui/scroll-area';
import { CheckCircle2, MoveRight, User, Wrench, Zap } from 'lucide-react';
import { useEffect, useRef } from 'react';
import { formatUsd } from '@/lib/utils';

interface CheckpointTimelineProps {
    checkpoints: CheckpointResponse[];
    isRunning: boolean;
}

const EVENT_STYLES: Record<CheckpointEvent['type'], { label: string; chipClassName: string }> = {
    system: {
        label: 'System',
        chipClassName: 'border-border/40 bg-white/5 text-muted-foreground',
    },
    checkpoint: {
        label: 'Checkpoint',
        chipClassName: 'border-border/40 bg-white/5 text-muted-foreground',
    },
    input: {
        label: 'Input',
        chipClassName: 'border-primary/30 bg-primary/10 text-primary',
    },
    tool_call: {
        label: 'Tool Call',
        chipClassName: 'border-warning/30 bg-warning/10 text-warning',
    },
    tool_result: {
        label: 'Tool Result',
        chipClassName: 'border-success/30 bg-success/10 text-success',
    },
    output: {
        label: 'Output',
        chipClassName: 'border-primary/30 bg-primary/10 text-primary',
    },
};

function formatJson(value: unknown) {
    if (value == null) {
        return '';
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

function getEventStyle(event?: CheckpointEvent) {
    return EVENT_STYLES[event?.type ?? 'checkpoint'];
}

function getEventIcon(event?: CheckpointEvent) {
    switch (event?.type) {
        case 'input':
            return User;
        case 'tool_call':
        case 'tool_result':
            return Wrench;
        case 'output':
            return CheckCircle2;
        default:
            return Zap;
    }
}

function getContentLabel(event?: CheckpointEvent) {
    switch (event?.type) {
        case 'input':
            return 'Input Text';
        case 'output':
            return 'Response';
        case 'checkpoint':
            return 'Content';
        default:
            return 'Details';
    }
}

export function CheckpointTimeline({ checkpoints, isRunning }: CheckpointTimelineProps) {
    const scrollRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
            const viewport = scrollRef.current.querySelector('[data-radix-scroll-area-viewport]');
            if (viewport) {
                viewport.scrollTop = viewport.scrollHeight;
            }
        }
    }, [checkpoints.length]);

    return (
        <div className="border border-border/40 bg-black/40 backdrop-blur flex flex-col h-[720px]">
            <div className="p-4 border-b border-border/40 bg-black/60 shrink-0">
                <h3 className="font-display text-sm uppercase tracking-widest text-primary flex items-center gap-2">
                    <Zap className="w-4 h-4" /> Execution Timeline
                </h3>
                <p className="mt-2 text-xs text-muted-foreground uppercase tracking-wider">
                    Parsed execution events from durable checkpoints.
                </p>
            </div>

            <ScrollArea className="flex-1" ref={scrollRef}>
                <div className="p-6">
                    {checkpoints.length === 0 ? (
                        <div className="h-full flex items-center justify-center pt-20">
                            <span className="text-muted-foreground text-sm tracking-widest uppercase animate-pulse">
                                Waiting for checkpoints...
                            </span>
                        </div>
                    ) : (
                        <div className="relative border-l border-border/40 ml-4 space-y-8 pl-8 pb-8">
                            {checkpoints.map((cp, idx) => {
                                const prevWorker = idx > 0 ? checkpoints[idx - 1].worker_id : null;
                                const isHandoff = prevWorker && prevWorker !== cp.worker_id;
                                const event = cp.event;
                                const style = getEventStyle(event);
                                const EventIcon = getEventIcon(event);
                                const content = formatJson(event?.content);
                                const toolArgs = formatJson(event?.tool_args);
                                const toolResult = formatJson(event?.tool_result);
                                const showSummary = Boolean(event?.summary) && !content && !toolArgs && !toolResult;

                                return (
                                    <div key={cp.checkpoint_id} className="relative animate-in slide-in-from-left-4 fade-in duration-300">
                                        <div className="absolute -left-[45px] top-1 h-6 w-6 rounded-full border-2 border-background bg-primary shadow-[0_0_8px_var(--color-primary)] ring-2 ring-primary/20 flex items-center justify-center">
                                            <EventIcon className="w-3 h-3 text-black" />
                                        </div>

                                        <div className="space-y-3 border border-border/30 bg-black/35 p-4">
                                            <div className="flex flex-col gap-2">
                                                <div className="flex flex-wrap items-center gap-2">
                                                    <span className="text-xs font-bold text-primary tracking-wider uppercase">
                                                        Step {cp.step_number}
                                                    </span>
                                                    <span className={`border px-2 py-0.5 text-[10px] font-bold uppercase tracking-[0.2em] ${style.chipClassName}`}>
                                                        {style.label}
                                                    </span>
                                                    <span className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground">
                                                        {cp.node_name}
                                                    </span>
                                                </div>
                                                <div className="flex items-start justify-between gap-3">
                                                    <div>
                                                        <div className="text-sm font-display uppercase tracking-wide text-foreground">
                                                            {event?.title ?? 'Checkpoint Saved'}
                                                        </div>
                                                        {showSummary && (
                                                            <p className="mt-2 text-sm leading-6 text-muted-foreground whitespace-pre-wrap break-all">
                                                                {event?.summary}
                                                            </p>
                                                        )}
                                                    </div>
                                                    <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                                                        {new Date(cp.created_at).toLocaleTimeString()}
                                                    </span>
                                                </div>
                                            </div>

                                            {isHandoff && (
                                                <div className="bg-warning/10 border border-warning/20 p-2 text-xs text-warning flex items-center gap-2">
                                                    <MoveRight className="w-3 h-3 shrink-0" />
                                                    <span className="font-bold tracking-widest uppercase shrink-0">Handoff</span>
                                                    <span className="opacity-80 truncate" title={`${prevWorker} → ${cp.worker_id}`}>
                                                        {prevWorker} → {cp.worker_id}
                                                    </span>
                                                </div>
                                            )}

                                            {!!content && (
                                                <div className="border border-primary/20 bg-primary/5">
                                                    <div className="border-b border-primary/20 px-3 py-2 text-[10px] font-bold uppercase tracking-[0.2em] text-primary">
                                                        {getContentLabel(event)}
                                                    </div>
                                                    <pre className="overflow-auto p-3 text-xs font-mono text-primary whitespace-pre-wrap break-all">
                                                        {content}
                                                    </pre>
                                                </div>
                                            )}

                                            {!!toolArgs && (
                                                <div className="border border-warning/20 bg-warning/5">
                                                    <div className="border-b border-warning/20 px-3 py-2 text-[10px] font-bold uppercase tracking-[0.2em] text-warning">
                                                        Tool Arguments
                                                    </div>
                                                    <pre className="overflow-auto p-3 text-xs font-mono text-warning whitespace-pre-wrap break-all">
                                                        {toolArgs}
                                                    </pre>
                                                </div>
                                            )}

                                            {!!toolResult && (
                                                <div className="border border-success/20 bg-success/5">
                                                    <div className="border-b border-success/20 px-3 py-2 text-[10px] font-bold uppercase tracking-[0.2em] text-success">
                                                        Tool Result
                                                    </div>
                                                    <pre className="overflow-auto p-3 text-xs font-mono text-success whitespace-pre-wrap break-all">
                                                        {toolResult}
                                                    </pre>
                                                </div>
                                            )}

                                            {!!event?.usage && (
                                                <div className="grid grid-cols-3 gap-3 text-xs font-mono">
                                                    <div className="border border-border/20 bg-black/50 p-3">
                                                        <span className="block text-muted-foreground mb-1 uppercase tracking-wider">Input</span>
                                                        <span>{event.usage.input_tokens ?? '-'}</span>
                                                    </div>
                                                    <div className="border border-border/20 bg-black/50 p-3">
                                                        <span className="block text-muted-foreground mb-1 uppercase tracking-wider">Output</span>
                                                        <span>{event.usage.output_tokens ?? '-'}</span>
                                                    </div>
                                                    <div className="border border-border/20 bg-black/50 p-3">
                                                        <span className="block text-muted-foreground mb-1 uppercase tracking-wider">Total</span>
                                                        <span>{event.usage.total_tokens ?? '-'}</span>
                                                    </div>
                                                </div>
                                            )}

                                            <div className="grid grid-cols-3 gap-3 text-xs font-mono bg-black/50 p-3 border border-border/20">
                                                <div>
                                                    <span className="text-muted-foreground block mb-1 uppercase tracking-wider">Worker</span>
                                                    <span className="break-all opacity-80">{cp.worker_id}</span>
                                                </div>
                                                <div>
                                                    <span className="text-muted-foreground block mb-1 uppercase tracking-wider">Cost Delta</span>
                                                    <span className="text-success">+${formatUsd(cp.cost_microdollars)}</span>
                                                </div>
                                                <div>
                                                    <span className="text-muted-foreground block mb-1 uppercase tracking-wider">Node</span>
                                                    <span className="opacity-80">{cp.node_name}</span>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                );
                            })}

                            {isRunning && (
                                <div className="relative pt-4">
                                    <div className="absolute -left-[41px] top-6 h-4 w-4 rounded-full border border-primary bg-primary/20 animate-ping" />
                                    <div className="absolute -left-[39px] top-[26px] h-3 w-3 rounded-full bg-primary" />
                                    <div className="pl-2">
                                        <span className="text-xs tracking-widest font-bold uppercase text-primary animate-pulse">
                                            Running Compute...
                                        </span>
                                    </div>
                                </div>
                            )}
                        </div>
                    )}
                </div>
            </ScrollArea>
        </div>
    );
}
