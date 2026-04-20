import { CheckpointEvent, CheckpointResponse, TaskEventResponse, TaskEventType } from '@/types';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
    AlertCircle, BrainCircuit, MoveRight, RotateCcw, User, Wrench, Zap,
    Pause, PlayCircle, CheckCircle2, ShieldCheck, ShieldX,
    MessageSquare, MessageCircle, Ban, RefreshCw, Scissors,
} from 'lucide-react';
import { useEffect, useMemo, useRef } from 'react';
import { formatUsd } from '@/lib/utils';
import { TaskStatus } from '@/types';

// ─── HITL event types to show as inline markers ────────────────────

const HITL_EVENT_TYPES = new Set<TaskEventType>([
    'task_approval_requested',
    'task_approved',
    'task_rejected',
    'task_input_requested',
    'task_input_received',
    'task_paused',
    'task_resumed',
    'task_cancelled',
    'task_redriven',
    'task_completed',
    'task_follow_up',
    'task_compaction_fired',
]);

interface HitlMarkerStyle {
    label: string;
    colorClass: string;
    bgClass: string;
    icon: typeof Pause;
}

const HITL_STYLES: Partial<Record<TaskEventType, HitlMarkerStyle>> = {
    task_approval_requested: { label: 'Approval Requested',  colorClass: 'text-amber-400',  bgClass: 'bg-amber-500',  icon: ShieldCheck },
    task_approved:           { label: 'Approved',            colorClass: 'text-green-400',  bgClass: 'bg-green-500',  icon: ShieldCheck },
    task_rejected:           { label: 'Rejected',            colorClass: 'text-red-400',    bgClass: 'bg-red-500',    icon: ShieldX },
    task_input_requested:    { label: 'Input Requested',     colorClass: 'text-amber-400',  bgClass: 'bg-amber-500',  icon: MessageSquare },
    task_input_received:     { label: 'Input Received',      colorClass: 'text-amber-400',  bgClass: 'bg-amber-500',  icon: MessageCircle },
    task_paused:             { label: 'Task Paused',         colorClass: 'text-amber-400',  bgClass: 'bg-amber-500',  icon: Pause },
    task_resumed:            { label: 'Task Resumed',        colorClass: 'text-green-400',  bgClass: 'bg-green-500',  icon: PlayCircle },
    task_cancelled:          { label: 'Task Cancelled',      colorClass: 'text-red-400',    bgClass: 'bg-red-500',    icon: Ban },
    task_redriven:           { label: 'Task Redriven',       colorClass: 'text-blue-400',   bgClass: 'bg-blue-500',   icon: RefreshCw },
    task_completed:          { label: 'Task Completed',      colorClass: 'text-emerald-400', bgClass: 'bg-emerald-500', icon: CheckCircle2 },
    task_follow_up:          { label: 'Follow Up',           colorClass: 'text-primary',     bgClass: 'bg-primary',     icon: MessageSquare },
    task_compaction_fired:   { label: 'Context Compacted',   colorClass: 'text-purple-400',  bgClass: 'bg-purple-500',  icon: Scissors },
};

// ─── Unified timeline entry ────────────────────────────────────────

type TimelineEntry =
    | { kind: 'checkpoint'; data: CheckpointResponse; ts: number }
    | { kind: 'hitl'; data: TaskEventResponse; ts: number };

function buildTimeline(
    checkpoints: CheckpointResponse[],
    hitlEvents: TaskEventResponse[],
): TimelineEntry[] {
    const entries: TimelineEntry[] = [];

    for (const cp of checkpoints) {
        entries.push({ kind: 'checkpoint', data: cp, ts: new Date(cp.created_at).getTime() });
    }
    for (const ev of hitlEvents) {
        if (HITL_EVENT_TYPES.has(ev.event_type)) {
            entries.push({ kind: 'hitl', data: ev, ts: new Date(ev.created_at).getTime() });
        }
    }

    entries.sort((a, b) => a.ts - b.ts);
    return entries;
}

// ─── Props ─────────────────────────────────────────────────────────

interface CheckpointTimelineProps {
    checkpoints: CheckpointResponse[];
    hitlEvents?: TaskEventResponse[];
    isRunning: boolean;
    retryHistory?: string[];
    status?: TaskStatus;
    deadLetterReason?: string;
    lastErrorCode?: string;
    lastErrorMessage?: string;
    deadLetteredAt?: string;
}

// ─── Resume / failure markers (unchanged logic) ────────────────────

interface ResumeMarker {
    resumedAfterStep: number | null;
}

interface TerminalFailureMarker {
    failedAfterStep: number | null;
    reason?: string;
    errorCode?: string;
    failedAt?: string;
    failedBeforeNextCheckpoint: boolean;
}

function getTimestamp(value: string) {
    const timestamp = Date.parse(value);
    return Number.isNaN(timestamp) ? null : timestamp;
}

export function getResumeMarkers(checkpoints: CheckpointResponse[], retryHistory: string[] = []) {
    const markers = new Map<string, ResumeMarker>();
    if (checkpoints.length === 0 || retryHistory.length === 0) {
        return markers;
    }

    const checkpointTimes = checkpoints.map((checkpoint) => getTimestamp(checkpoint.created_at));

    retryHistory.forEach((retryAt, retryIndex) => {
        const retryTimestamp = getTimestamp(retryAt);
        if (retryTimestamp === null) {
            return;
        }

        const nextRetryTimestamp = retryIndex + 1 < retryHistory.length
            ? getTimestamp(retryHistory[retryIndex + 1])
            : null;

        const checkpointIndex = checkpoints.findIndex((_, index) => {
            const checkpointTimestamp = checkpointTimes[index];
            if (checkpointTimestamp === null || checkpointTimestamp <= retryTimestamp) {
                return false;
            }

            return nextRetryTimestamp === null || checkpointTimestamp <= nextRetryTimestamp;
        });

        if (checkpointIndex === -1) {
            return;
        }

        markers.set(checkpoints[checkpointIndex].checkpoint_id, {
            resumedAfterStep: checkpointIndex > 0 ? checkpoints[checkpointIndex - 1].step_number : null,
        });
    });

    return markers;
}

export function getTerminalFailureMarker(
    checkpoints: CheckpointResponse[],
    status?: TaskStatus,
    retryHistory: string[] = [],
    deadLetterReason?: string,
    lastErrorCode?: string,
    _lastErrorMessage?: string,
    deadLetteredAt?: string,
) {
    if (status !== 'dead_letter') {
        return null;
    }

    const lastCheckpoint = checkpoints.at(-1);
    const lastCheckpointTimestamp = lastCheckpoint ? getTimestamp(lastCheckpoint.created_at) : null;
    const latestRetryTimestamp = retryHistory.length > 0 ? getTimestamp(retryHistory[retryHistory.length - 1]) : null;

    return {
        failedAfterStep: lastCheckpoint?.step_number ?? null,
        reason: deadLetterReason,
        errorCode: lastErrorCode,
        failedAt: deadLetteredAt,
        failedBeforeNextCheckpoint: (
            latestRetryTimestamp !== null &&
            lastCheckpointTimestamp !== null &&
            latestRetryTimestamp > lastCheckpointTimestamp
        ),
    } satisfies TerminalFailureMarker;
}

// ─── Checkpoint event styling ──────────────────────────────────────

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
        label: 'Input + Inference',
        chipClassName: 'border-primary/30 bg-primary/10 text-primary',
    },
    tool_call: {
        label: 'Tool Request',
        chipClassName: 'border-warning/30 bg-warning/10 text-warning',
    },
    tool_result: {
        label: 'Tool Result',
        chipClassName: 'border-success/30 bg-success/10 text-success',
    },
    output: {
        label: 'Model Response',
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
            return BrainCircuit;
        case 'tool_result':
            return Wrench;
        case 'output':
            return BrainCircuit;
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

// ─── Main component ────────────────────────────────────────────────

export function CheckpointTimeline({
    checkpoints,
    hitlEvents = [],
    isRunning,
    retryHistory = [],
    status,
    deadLetterReason,
    lastErrorCode,
    lastErrorMessage,
    deadLetteredAt,
}: CheckpointTimelineProps) {
    const scrollRef = useRef<HTMLDivElement>(null);
    const resumeMarkers = getResumeMarkers(checkpoints, retryHistory);
    const terminalFailureMarker = getTerminalFailureMarker(
        checkpoints,
        status,
        retryHistory,
        deadLetterReason,
        lastErrorCode,
        lastErrorMessage,
        deadLetteredAt,
    );

    const timeline = buildTimeline(checkpoints, hitlEvents);

    // Pre-compute handoff markers: for each checkpoint, record whether
    // the worker changed compared to the previous checkpoint.
    const handoffMap = useMemo(() => {
        const map = new Map<string, { prevWorker: string }>();
        let prevWorkerId: string | null = null;
        for (const entry of timeline) {
            if (entry.kind === 'checkpoint') {
                const cp = entry.data as CheckpointResponse;
                if (prevWorkerId && prevWorkerId !== cp.worker_id) {
                    map.set(cp.checkpoint_id, { prevWorker: prevWorkerId });
                }
                prevWorkerId = cp.worker_id;
            }
        }
        return map;
    }, [timeline]);

    // Pre-compute cumulative costs and step durations for checkpoints
    const cumulativeCosts = new Map<string, number>();
    const stepDurations = new Map<string, number>();
    let runningCost = 0;
    let prevCheckpointTs: number | null = null;

    // Collect pause intervals from HITL events
    const pauseIntervals: { start: number; end: number }[] = [];
    let pauseStart: number | null = null;
    for (const ev of hitlEvents) {
        if (ev.event_type === 'task_paused') {
            pauseStart = new Date(ev.created_at).getTime();
        } else if (ev.event_type === 'task_resumed' && pauseStart !== null) {
            pauseIntervals.push({ start: pauseStart, end: new Date(ev.created_at).getTime() });
            pauseStart = null;
        }
    }

    for (const entry of timeline) {
        if (entry.kind === 'checkpoint') {
            const cp = entry.data as CheckpointResponse;
            runningCost += cp.cost_microdollars;
            cumulativeCosts.set(cp.checkpoint_id, runningCost);

            const cpTs = new Date(cp.created_at).getTime();
            if (prevCheckpointTs !== null) {
                let delta = cpTs - prevCheckpointTs;
                // Subtract any pause intervals that overlap this step
                for (const pause of pauseIntervals) {
                    const overlapStart = Math.max(pause.start, prevCheckpointTs);
                    const overlapEnd = Math.min(pause.end, cpTs);
                    if (overlapStart < overlapEnd) {
                        delta -= (overlapEnd - overlapStart);
                    }
                }
                stepDurations.set(cp.checkpoint_id, Math.max(0, delta));
            }
            prevCheckpointTs = cpTs;
        }
    }

    useEffect(() => {
        if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
            const viewport = scrollRef.current.querySelector('[data-radix-scroll-area-viewport]');
            if (viewport) {
                viewport.scrollTop = viewport.scrollHeight;
            }
        }
    }, [timeline.length]);

    const isWaiting = status === 'waiting_for_approval' || status === 'waiting_for_input';

    return (
        <Card className="console-surface border-white/10 flex flex-col h-[480px]">
            <CardHeader className="border-b border-white/8 shrink-0">
                <CardTitle className="text-sm font-display uppercase tracking-widest text-muted-foreground">
                    Execution Timeline
                </CardTitle>
            </CardHeader>

            <ScrollArea className="flex-1" ref={scrollRef}>
                <div className="p-6">
                    {timeline.length === 0 ? (
                        <div className="h-full flex items-center justify-center pt-20">
                            <span className="text-muted-foreground text-sm tracking-widest uppercase animate-pulse">
                                Waiting for checkpoints...
                            </span>
                        </div>
                    ) : (
                        <div className="relative border-l border-border/40 ml-10 space-y-8 pl-8 pb-8">
                            {timeline.map((entry) => {
                                // ── HITL marker row ──
                                if (entry.kind === 'hitl') {
                                    const ev = entry.data;
                                    const hitlStyle = HITL_STYLES[ev.event_type];
                                    if (!hitlStyle) return null;
                                    const HitlIcon = hitlStyle.icon;

                                    // Extract displayable detail from event
                                    const detail = ev.details as Record<string, unknown> | undefined;
                                    let detailText: string | null =
                                        (detail?.message as string) ||   // input received
                                        (detail?.prompt as string) ||    // input requested
                                        (detail?.reason as string) ||    // rejected
                                        null;

                                    // Budget pause/resume events
                                    if (detail?.pause_reason) {
                                        const limit = (detail.budget_max_per_task as number) || (detail.budget_max_per_hour as number);
                                        const observed = (detail.observed_task_cost_microdollars as number) || (detail.observed_hour_cost_microdollars as number);
                                        if (limit && observed) {
                                            detailText = `$${formatUsd(observed)} / $${formatUsd(limit)} limit`;
                                        }
                                    } else if (detail?.resume_trigger) {
                                        if ((detail.resume_trigger as string) === 'automatic_hourly_recovery') {
                                            detailText = 'Hourly budget window reset';
                                            if (detail.budget_max_per_hour) {
                                                detailText += ` — limit $${formatUsd(detail.budget_max_per_hour as number)}/hr`;
                                            }
                                        } else {
                                            // manual_operator_resume
                                            const budgetAtResume = detail.budget_max_per_task_at_resume as number | undefined;
                                            const taskCost = detail.task_cost_microdollars as number | undefined;
                                            if (budgetAtResume && taskCost) {
                                                detailText = `Budget raised to $${formatUsd(budgetAtResume)} (task cost: $${formatUsd(taskCost)})`;
                                            } else if (budgetAtResume) {
                                                detailText = `Budget raised to $${formatUsd(budgetAtResume)}`;
                                            } else {
                                                detailText = 'Resumed by operator';
                                            }
                                        }
                                    } else if (ev.event_type === 'task_compaction_fired') {
                                        const tokensIn = detail?.tokens_in as number | undefined;
                                        const tokensOut = detail?.tokens_out as number | undefined;
                                        const turns = detail?.turns_summarized as number | undefined;
                                        const first = detail?.first_turn_index as number | undefined;
                                        const last = detail?.last_turn_index as number | undefined;
                                        const model = detail?.summarizer_model_id as string | undefined;
                                        const parts: string[] = [];
                                        if (turns !== undefined && first !== undefined && last !== undefined) {
                                            parts.push(`${turns} turns (${first}→${last})`);
                                        }
                                        if (tokensIn !== undefined && tokensOut !== undefined) {
                                            parts.push(`${tokensIn.toLocaleString()} in → ${tokensOut.toLocaleString()} out`);
                                        }
                                        if (model) parts.push(model);
                                        if (parts.length > 0) detailText = parts.join(' · ');
                                    }

                                    return (
                                        <div key={`hitl-${ev.event_id}`} className="relative animate-in slide-in-from-left-4 fade-in duration-300">
                                            <div className={`absolute -left-[45px] top-0.5 h-6 w-6 rounded-full border-2 border-background ${hitlStyle.bgClass} shadow-[0_0_8px_rgba(0,0,0,0.3)] flex items-center justify-center`}>
                                                <HitlIcon className="w-3 h-3 text-black" />
                                            </div>

                                            <div className="border border-border/30 bg-black/35 px-4 py-3 space-y-2">
                                                <div className="flex items-center justify-between gap-3">
                                                    <span className={`text-xs font-bold tracking-wider uppercase ${hitlStyle.colorClass}`}>
                                                        {hitlStyle.label}
                                                    </span>
                                                    <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                                                        {new Date(ev.created_at).toLocaleTimeString()}
                                                    </span>
                                                </div>
                                                {detailText && (
                                                    <pre className="text-xs font-mono text-muted-foreground whitespace-pre-wrap break-all leading-5">
                                                        {detailText}
                                                    </pre>
                                                )}
                                            </div>
                                        </div>
                                    );
                                }

                                // ── Checkpoint row ──
                                const cp = entry.data;
                                const handoff = handoffMap.get(cp.checkpoint_id);
                                const isHandoff = !!handoff;
                                const resumeMarker = resumeMarkers.get(cp.checkpoint_id);
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
                                            {resumeMarker && (
                                                <div className="bg-primary/10 border border-primary/20 p-3 text-xs text-primary space-y-2">
                                                    <div className="flex items-center gap-2 min-w-0">
                                                        <RotateCcw className="w-3 h-3 shrink-0" />
                                                        <span className="font-bold tracking-widest uppercase">
                                                            Resumed From Saved Progress
                                                        </span>
                                                    </div>
                                                    <div className="pl-5 opacity-80 leading-5 wrap-break-word">
                                                        {resumeMarker.resumedAfterStep === null
                                                            ? 'Execution continued from the latest saved checkpoint instead of restarting.'
                                                            : `Execution continued from the checkpoint saved after step ${resumeMarker.resumedAfterStep}, so earlier progress was preserved.`}
                                                    </div>
                                                </div>
                                            )}

                                            <div className="flex flex-col gap-2">
                                                <div className="flex flex-wrap items-center gap-2">
                                                    <span className="text-xs font-bold text-primary tracking-wider uppercase">
                                                        Step {cp.step_number}
                                                    </span>
                                                    <span className={`border px-2 py-0.5 text-[10px] font-bold uppercase tracking-[0.2em] ${style.chipClassName}`}>
                                                        {style.label}
                                                    </span>
                                                </div>
                                                <div className="flex items-start justify-between gap-3">
                                                    <div>
                                                        <div className="text-sm font-display uppercase tracking-wide text-foreground">
                                                            {event?.type === 'input'
                                                                ? 'Input Sent to Model'
                                                                : event?.type === 'tool_call'
                                                                ? 'Model Requested Tool'
                                                                : (event?.title ?? 'Checkpoint Saved')}
                                                        </div>
                                                        {event?.type === 'tool_call' && event?.tool_name && (
                                                            <p className="mt-1 text-xs text-warning tracking-wider">
                                                                ↳ Tool: <span className="font-semibold">{event.tool_name}</span>
                                                            </p>
                                                        )}
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
                                                <div className="bg-warning/10 border border-warning/20 p-2 text-xs text-warning flex flex-wrap items-start gap-2 min-w-0">
                                                    <MoveRight className="w-3 h-3 shrink-0 mt-0.5" />
                                                    <span className="font-bold tracking-widest uppercase shrink-0">Handoff</span>
                                                    <span className="opacity-80 min-w-0 flex-1 break-all" title={`${handoff?.prevWorker} → ${cp.worker_id}`}>
                                                        {handoff?.prevWorker} → {cp.worker_id}
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
                                                        Tool Arguments Sent
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

                                            <div className="grid grid-cols-4 gap-3 text-xs font-mono bg-black/50 p-3 border border-border/20">
                                                <div>
                                                    <span className="text-muted-foreground block mb-1 uppercase tracking-wider">Worker</span>
                                                    <span className="break-all opacity-80">{cp.worker_id}</span>
                                                </div>
                                                <div>
                                                    <span className="text-muted-foreground block mb-1 uppercase tracking-wider">Step Cost</span>
                                                    <span className="text-success">+${formatUsd(cp.cost_microdollars)}</span>
                                                </div>
                                                <div>
                                                    <span className="text-muted-foreground block mb-1 uppercase tracking-wider">Total Cost</span>
                                                    <span className="text-success">${formatUsd(cumulativeCosts.get(cp.checkpoint_id) ?? 0)}</span>
                                                </div>
                                                <div>
                                                    <span className="text-muted-foreground block mb-1 uppercase tracking-wider">Duration</span>
                                                    <span className="text-primary">
                                                        {stepDurations.has(cp.checkpoint_id)
                                                            ? `${((stepDurations.get(cp.checkpoint_id)!) / 1000).toFixed(1)}s`
                                                            : '—'}
                                                    </span>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                );
                            })}

                            {terminalFailureMarker && (
                                <div className="relative animate-in slide-in-from-left-4 fade-in duration-300">
                                    <div className="absolute -left-[45px] top-1 h-6 w-6 rounded-full border-2 border-background bg-destructive shadow-[0_0_8px_var(--color-destructive)] ring-2 ring-destructive/20 flex items-center justify-center">
                                        <AlertCircle className="w-3 h-3 text-black" />
                                    </div>

                                    <div className="space-y-3 border border-destructive/30 bg-destructive/10 p-4">
                                        <div className="flex flex-wrap items-center gap-2">
                                            <span className="text-xs font-bold text-destructive tracking-wider uppercase">
                                                Execution Failed
                                            </span>
                                            {terminalFailureMarker.reason && (
                                                <span className="border border-destructive/30 bg-destructive/10 px-2 py-0.5 text-[10px] font-bold uppercase tracking-[0.2em] text-destructive">
                                                    {terminalFailureMarker.reason}
                                                </span>
                                            )}
                                            {terminalFailureMarker.failedAt && (
                                                <span className="text-xs text-muted-foreground tabular-nums">
                                                    {new Date(terminalFailureMarker.failedAt).toLocaleTimeString()}
                                                </span>
                                            )}
                                        </div>

                                        <div className="text-sm leading-6 text-destructive/90 whitespace-pre-wrap wrap-break-word">
                                            {terminalFailureMarker.failedBeforeNextCheckpoint
                                                ? 'A later attempt failed before another checkpoint could be saved, so the timeline ends at the last durable step below.'
                                                : 'Execution ended in a failure after the last recorded checkpoint.'}
                                        </div>

                                        {terminalFailureMarker.failedAfterStep !== null && (
                                            <div className="text-xs font-mono uppercase tracking-widest text-destructive/70">
                                                Last durable checkpoint: step {terminalFailureMarker.failedAfterStep}
                                            </div>
                                        )}
                                        {terminalFailureMarker.errorCode && (
                                            <div className="text-xs font-mono uppercase tracking-widest text-destructive/70">
                                                Error code: {terminalFailureMarker.errorCode}
                                            </div>
                                        )}
                                    </div>
                                </div>
                            )}

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

                            {isWaiting && (
                                <div className="relative pt-4">
                                    <div className="absolute -left-[41px] top-6 h-4 w-4 rounded-full border border-amber-500 bg-amber-500/20 animate-ping" />
                                    <div className="absolute -left-[39px] top-[26px] h-3 w-3 rounded-full bg-amber-500" />
                                    <div className="pl-2">
                                        <span className="text-xs tracking-widest font-bold uppercase text-amber-400 animate-pulse">
                                            {status === 'waiting_for_approval' ? 'Awaiting Approval...' : 'Awaiting Human Input...'}
                                        </span>
                                    </div>
                                </div>
                            )}
                        </div>
                    )}
                </div>
            </ScrollArea>
        </Card>
    );
}
