import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
    User as UserIcon,
    Sparkles,
    Wrench,
    Scissors,
    Archive,
    PauseCircle,
    PlayCircle,
    StickyNote,
    Info,
    ChevronDown,
    ChevronRight,
    AlertTriangle,
    Activity as ActivityIcon,
} from 'lucide-react';

import { api } from '@/api/client';
import type { ActivityEvent, TaskStatus } from '@/types';

/**
 * Phase 2 Track 7 Follow-up Task 8 (C) — unified Activity pane.
 *
 * Single view over `GET /v1/tasks/{taskId}/activity`. Turns render chat-bubble
 * style; infrastructure markers render as dashed dividers / banners. Mirrors
 * the legacy ConversationPane visual language so operators can switch tasks
 * without re-learning the page.
 */

const TERMINAL_STATUSES: ReadonlySet<TaskStatus> = new Set<TaskStatus>([
    'completed',
    'cancelled',
    'dead_letter',
]);

function isTerminalStatus(status?: TaskStatus): boolean {
    return !!status && TERMINAL_STATUSES.has(status);
}

function formatJson(value: unknown): string {
    if (value == null) return '';
    if (typeof value === 'string') return value;
    try {
        return JSON.stringify(value, null, 2);
    } catch {
        return String(value);
    }
}

function formatTime(raw?: string | null): string {
    if (!raw) return '';
    try {
        const d = new Date(raw);
        if (Number.isNaN(d.getTime())) return raw;
        return d.toLocaleTimeString();
    } catch {
        return raw;
    }
}

function truncate(value: string, max = 4000): string {
    if (value.length <= max) return value;
    return `${value.slice(0, max)}…`;
}

function isTurn(kind: string): boolean {
    return kind.startsWith('turn.');
}

function isHitlResume(kind: string): boolean {
    return (
        kind === 'marker.hitl.approved' ||
        kind === 'marker.hitl.rejected' ||
        kind === 'marker.hitl.input_received' ||
        kind === 'marker.hitl.resumed'
    );
}

function isHitlPause(kind: string): boolean {
    return (
        kind === 'marker.hitl.paused' ||
        kind === 'marker.hitl.approval_requested' ||
        kind === 'marker.hitl.input_requested'
    );
}

function hasDetailsPayload(event: ActivityEvent): boolean {
    return (
        !!(event.details && Object.keys(event.details).length > 0) ||
        !!(event.tool_calls && event.tool_calls.length > 0)
    );
}

function detailsBlockJson(event: ActivityEvent): string {
    return formatJson({
        tool_calls: event.tool_calls ?? undefined,
        details: event.details ?? undefined,
    });
}

// ─── Row renderers ─────────────────────────────────────────────────

interface RowProps {
    event: ActivityEvent;
    index: number;
}

/**
 * Shared chevron + details `<pre>` that every row with a payload exposes,
 * so `activity-row-<i>-expand` / `activity-row-<i>-details` stay a stable
 * Playwright contract regardless of role-specific visual chrome above.
 */
function ExpandAffordance({
    event,
    index,
    className = '',
}: RowProps & { className?: string }) {
    const [open, setOpen] = useState(false);
    if (!hasDetailsPayload(event)) return null;
    return (
        <div className={className}>
            <button
                type="button"
                data-testid={`activity-row-${index}-expand`}
                aria-expanded={open}
                aria-label={open ? 'Hide details' : 'Show details'}
                onClick={() => setOpen((v) => !v)}
                className="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-[0.2em] text-muted-foreground hover:text-foreground transition-colors"
            >
                {open ? (
                    <ChevronDown className="w-3 h-3" />
                ) : (
                    <ChevronRight className="w-3 h-3" />
                )}
                {open ? 'Hide payload' : 'Show payload'}
            </button>
            {open && (
                <pre
                    data-testid={`activity-row-${index}-details`}
                    className="mt-2 text-[11px] font-mono bg-black/40 border border-border/30 rounded p-2 overflow-auto max-h-72 whitespace-pre-wrap break-all"
                >
                    {detailsBlockJson(event)}
                </pre>
            )}
        </div>
    );
}

function UserTurnRow({ event, index }: RowProps) {
    const text = event.content ?? '';
    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            className="list-none flex justify-end animate-in fade-in duration-300"
        >
            <div className="max-w-[85%] bg-primary/15 border border-primary/30 rounded-2xl rounded-br-sm px-4 py-3 space-y-1">
                <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-primary">
                    <UserIcon className="w-3 h-3" /> You
                    {event.timestamp && (
                        <span className="ml-2 text-muted-foreground font-normal normal-case tracking-normal">
                            {formatTime(event.timestamp)}
                        </span>
                    )}
                </div>
                <div
                    data-testid={`activity-row-${index}-content`}
                    className="text-sm text-foreground whitespace-pre-wrap break-words leading-6"
                >
                    {truncate(text) || <span className="text-muted-foreground">(empty)</span>}
                </div>
                <ExpandAffordance event={event} index={index} className="pt-1" />
            </div>
        </li>
    );
}

function AssistantTurnRow({ event, index }: RowProps) {
    const text = event.content ?? '';
    const requested = (event.tool_calls ?? [])
        .map((c) => c.name || 'tool')
        .join(', ');
    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            className="list-none flex justify-start animate-in fade-in duration-300"
        >
            <div className="max-w-[85%] bg-muted/5 border border-border/30 rounded-2xl rounded-bl-sm px-4 py-3 space-y-1">
                <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-muted-foreground">
                    <Sparkles className="w-3 h-3" /> Agent
                    {event.timestamp && (
                        <span className="ml-2 font-normal normal-case tracking-normal">
                            {formatTime(event.timestamp)}
                        </span>
                    )}
                </div>
                <div
                    data-testid={`activity-row-${index}-content`}
                    className="text-sm text-foreground whitespace-pre-wrap break-words leading-6"
                >
                    {truncate(text) || (
                        <span className="text-muted-foreground italic">
                            (no text — see tool calls)
                        </span>
                    )}
                </div>
                {requested && (
                    <div className="text-[11px] font-mono text-warning/80">
                        → requested: {requested}
                    </div>
                )}
                <ExpandAffordance event={event} index={index} className="pt-1" />
            </div>
        </li>
    );
}

function ToolTurnRow({ event, index }: RowProps) {
    const text = event.content ?? '';
    const err = !!event.is_error;
    const toneBorder = err ? 'border-destructive/40' : 'border-success/30';
    const toneBg = err ? 'bg-destructive/5' : 'bg-success/5';
    const toneLabel = err ? 'text-destructive' : 'text-success';
    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            className={`list-none animate-in fade-in duration-300 border ${toneBorder} ${toneBg} rounded-lg px-4 py-3 space-y-2`}
        >
            <div
                className={`flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] ${toneLabel}`}
            >
                <Wrench className="w-3 h-3" />
                <span>Tool result</span>
                {event.tool_name && (
                    <span className="font-mono normal-case tracking-normal text-foreground">
                        ← {event.tool_name}
                    </span>
                )}
                {err && (
                    <span className="ml-2 inline-flex items-center gap-1 text-destructive normal-case tracking-normal font-semibold">
                        <AlertTriangle className="w-3 h-3" /> error
                    </span>
                )}
                {event.timestamp && (
                    <span className="ml-auto text-muted-foreground font-normal normal-case tracking-normal tabular-nums">
                        {formatTime(event.timestamp)}
                    </span>
                )}
            </div>
            <pre
                data-testid={`activity-row-${index}-content`}
                className={`text-xs font-mono whitespace-pre-wrap break-all ${err ? 'text-destructive' : 'text-success'}`}
            >
                {truncate(text)}
            </pre>
            <ExpandAffordance event={event} index={index} />
        </li>
    );
}

function CompactionMarkerRow({ event, index }: RowProps) {
    const first = (event.details?.first_turn_index as unknown) ?? '?';
    const last = (event.details?.last_turn_index as unknown) ?? '?';
    const turns = (event.details?.turns_summarized as unknown) ?? '?';
    const summary = event.summary_text ?? '';
    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            className="list-none animate-in fade-in duration-300 space-y-2"
        >
            <div className="flex items-center gap-2 text-xs font-mono text-muted-foreground py-3 border-y border-dashed border-border/40">
                <Scissors className="w-3 h-3 shrink-0" />
                <span className="flex-1 text-left tracking-wide">
                    — Context summarized (turns {String(first)}–{String(last)},{' '}
                    {String(turns)} turns) —
                </span>
                {event.timestamp && (
                    <span className="tabular-nums">{formatTime(event.timestamp)}</span>
                )}
            </div>
            {summary && (
                <div
                    data-testid={`activity-row-${index}-content`}
                    className="mx-4 text-sm text-foreground whitespace-pre-wrap break-words leading-6 border border-border/30 bg-black/30 p-3 rounded"
                >
                    {truncate(summary, 1600)}
                </div>
            )}
            <div className="mx-4">
                <ExpandAffordance event={event} index={index} />
            </div>
        </li>
    );
}

function MemoryFlushMarkerRow({ event, index }: RowProps) {
    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            className="list-none animate-in fade-in duration-300"
        >
            <div className="flex items-center gap-2 text-xs font-mono text-muted-foreground py-2 border-y border-dashed border-border/40">
                <StickyNote className="w-3 h-3 shrink-0" />
                <span
                    data-testid={`activity-row-${index}-content`}
                    className="flex-1"
                >
                    — Memory note injected{event.summary_text ? `: ${event.summary_text}` : ''} —
                </span>
                {event.timestamp && (
                    <span className="tabular-nums">{formatTime(event.timestamp)}</span>
                )}
            </div>
            <div className="mx-4 pt-1">
                <ExpandAffordance event={event} index={index} />
            </div>
        </li>
    );
}

function OffloadMarkerRow({ event, index }: RowProps) {
    const count = (event.details?.count as number | undefined) ?? null;
    const bytes = (event.details?.total_bytes as number | undefined) ?? null;
    const summary =
        event.summary_text ??
        (count != null
            ? `${count} tool output${count === 1 ? '' : 's'} archived${bytes ? ` (${bytes} B)` : ''}`
            : 'tool outputs archived');
    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            className="list-none animate-in fade-in duration-300"
        >
            <div className="flex items-center gap-2 text-xs font-mono text-muted-foreground/80 py-1.5 px-2">
                <Archive className="w-3 h-3 shrink-0" />
                <span
                    data-testid={`activity-row-${index}-content`}
                    className="flex-1 truncate"
                >
                    — {summary} —
                </span>
                {event.timestamp && (
                    <span className="tabular-nums">{formatTime(event.timestamp)}</span>
                )}
            </div>
            <div className="mx-4">
                <ExpandAffordance event={event} index={index} />
            </div>
        </li>
    );
}

function SystemNoteMarkerRow({ event, index }: RowProps) {
    const text = event.summary_text ?? event.content ?? '';
    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            className="list-none animate-in fade-in duration-300"
        >
            <div className="border border-border/40 bg-muted/5 px-4 py-2 text-xs text-muted-foreground font-mono flex items-start gap-2 rounded">
                <Info className="w-3 h-3 shrink-0 mt-0.5" />
                <span
                    data-testid={`activity-row-${index}-content`}
                    className="whitespace-pre-wrap break-words flex-1"
                >
                    {truncate(text, 600) || '(system note)'}
                </span>
                {event.timestamp && (
                    <span className="tabular-nums text-muted-foreground/70">
                        {formatTime(event.timestamp)}
                    </span>
                )}
            </div>
            <div className="mx-4 pt-1">
                <ExpandAffordance event={event} index={index} />
            </div>
        </li>
    );
}

function LifecycleMarkerRow({ event, index }: RowProps) {
    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            className="list-none animate-in fade-in duration-300"
        >
            <div className="flex items-center gap-2 text-xs font-mono text-muted-foreground py-2 border-y border-dashed border-border/40">
                <ActivityIcon className="w-3 h-3 shrink-0" />
                <span
                    data-testid={`activity-row-${index}-content`}
                    className="flex-1"
                >
                    — {event.event_type ?? 'lifecycle'}
                    {event.status_before && event.status_after
                        ? ` · ${event.status_before} → ${event.status_after}`
                        : ''}
                    {event.summary_text ? ` — ${event.summary_text}` : ''} —
                </span>
                {event.timestamp && (
                    <span className="tabular-nums">{formatTime(event.timestamp)}</span>
                )}
            </div>
            <div className="mx-4 pt-1">
                <ExpandAffordance event={event} index={index} />
            </div>
        </li>
    );
}

function HitlPauseMarkerRow({ event, index }: RowProps) {
    const reason =
        (event.details?.reason as string | undefined) ??
        event.summary_text ??
        'awaiting operator';
    const tool = event.details?.tool_name as string | undefined;
    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            className="list-none animate-in fade-in duration-300 border border-amber-500/30 bg-amber-500/10 rounded-lg px-4 py-3 space-y-2"
        >
            <div className="flex items-center gap-2 text-sm font-semibold text-amber-300">
                <PauseCircle className="w-4 h-4 shrink-0" />
                <span
                    data-testid={`activity-row-${index}-content`}
                    className="flex-1"
                >
                    ⏸ Paused — {reason}
                    {tool ? ` (tool: ${tool})` : ''}
                </span>
                {event.timestamp && (
                    <span className="ml-auto text-xs font-mono font-normal text-amber-200/70 tabular-nums">
                        {formatTime(event.timestamp)}
                    </span>
                )}
            </div>
            <ExpandAffordance event={event} index={index} />
        </li>
    );
}

function HitlResumeMarkerRow({ event, index }: RowProps) {
    const resolution =
        (event.details?.resolution as string | undefined) ??
        event.summary_text ??
        'resumed';
    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            className="list-none animate-in fade-in duration-300 border border-green-500/30 bg-green-500/10 rounded-lg px-4 py-3 space-y-2"
        >
            <div className="flex items-center gap-2 text-sm font-semibold text-green-300">
                <PlayCircle className="w-4 h-4 shrink-0" />
                <span
                    data-testid={`activity-row-${index}-content`}
                    className="flex-1"
                >
                    ▶ Resumed — {resolution}
                </span>
                {event.timestamp && (
                    <span className="ml-auto text-xs font-mono font-normal text-green-200/70 tabular-nums">
                        {formatTime(event.timestamp)}
                    </span>
                )}
            </div>
            <ExpandAffordance event={event} index={index} />
        </li>
    );
}

function ActivityRow({ event, index }: RowProps) {
    switch (event.kind) {
        case 'turn.user':
            return <UserTurnRow event={event} index={index} />;
        case 'turn.assistant':
            return <AssistantTurnRow event={event} index={index} />;
        case 'turn.tool':
            return <ToolTurnRow event={event} index={index} />;
        case 'marker.compaction_fired':
            return <CompactionMarkerRow event={event} index={index} />;
        case 'marker.memory_flush':
            return <MemoryFlushMarkerRow event={event} index={index} />;
        case 'marker.offload_emitted':
            return <OffloadMarkerRow event={event} index={index} />;
        case 'marker.system_note':
            return <SystemNoteMarkerRow event={event} index={index} />;
        case 'marker.lifecycle':
            return <LifecycleMarkerRow event={event} index={index} />;
        default:
            if (isHitlPause(event.kind)) {
                return <HitlPauseMarkerRow event={event} index={index} />;
            }
            if (isHitlResume(event.kind)) {
                return <HitlResumeMarkerRow event={event} index={index} />;
            }
            return <SystemNoteMarkerRow event={event} index={index} />;
    }
}

// ─── Pane ──────────────────────────────────────────────────────────

interface ActivityPaneProps {
    taskId: string;
    status?: TaskStatus;
}

export function ActivityPane({ taskId, status }: ActivityPaneProps) {
    const [showDetails, setShowDetails] = useState(false);

    const query = useQuery({
        queryKey: ['task-activity', taskId, showDetails],
        queryFn: () => api.listActivity(taskId, showDetails),
        enabled: !!taskId,
        refetchInterval: isTerminalStatus(status) ? false : 3_000,
        refetchOnWindowFocus: false,
    });

    const events = query.data?.events ?? [];
    const visibleCount = events.length;
    const isEmpty = !query.isLoading && visibleCount === 0;

    const summary = useMemo(() => {
        const counts: Record<string, number> = { turns: 0, markers: 0 };
        for (const e of events) {
            const bucket = isTurn(e.kind) ? 'turns' : 'markers';
            counts[bucket] = (counts[bucket] ?? 0) + 1;
        }
        return counts;
    }, [events]);

    return (
        <div
            data-testid="activity-pane"
            className="console-surface border-white/10 rounded-[24px] flex flex-col h-[560px] relative"
        >
            <div className="border-b border-white/8 px-6 py-4 shrink-0 flex items-start justify-between gap-4">
                <div>
                    <h3 className="text-sm font-display uppercase tracking-widest text-muted-foreground">
                        Activity
                    </h3>
                    <p className="text-xs text-muted-foreground/70 mt-1">
                        What the agent did
                    </p>
                </div>
                <div className="flex items-center gap-4 pt-1">
                    <span
                        data-testid="activity-summary"
                        className="text-[11px] font-mono text-muted-foreground tabular-nums"
                    >
                        {summary.turns ?? 0} turns · {summary.markers ?? 0} markers
                    </span>
                    <label
                        className="flex items-center gap-2 text-[11px] font-mono text-muted-foreground cursor-pointer select-none hover:text-foreground transition-colors"
                        data-testid="activity-details-toggle-label"
                    >
                        <input
                            type="checkbox"
                            data-testid="activity-details-toggle"
                            checked={showDetails}
                            onChange={(e) => setShowDetails(e.target.checked)}
                            className="accent-primary"
                        />
                        Show details
                    </label>
                </div>
            </div>

            <div className="flex-1 overflow-auto px-6 py-4 space-y-3">
                {query.isLoading && (
                    <div
                        data-testid="activity-loading"
                        className="h-full flex items-center justify-center pt-20"
                    >
                        <span className="text-muted-foreground text-sm tracking-widest uppercase animate-pulse">
                            Loading activity...
                        </span>
                    </div>
                )}

                {query.isError && (
                    <div
                        data-testid="activity-error"
                        role="alert"
                        className="flex items-start gap-3 text-destructive border border-destructive/40 bg-destructive/5 rounded-lg px-4 py-3"
                    >
                        <AlertTriangle className="w-5 h-5 shrink-0 mt-0.5" />
                        <div className="space-y-1">
                            <div className="text-sm font-bold uppercase tracking-widest">
                                Failed to load activity
                            </div>
                            <div className="text-xs font-mono opacity-80 break-all">
                                {(query.error as Error)?.message ?? 'unknown error'}
                            </div>
                        </div>
                    </div>
                )}

                {isEmpty && !query.isError && (
                    <div
                        data-testid="activity-empty"
                        className="h-full flex items-center justify-center pt-20"
                    >
                        <span className="text-muted-foreground text-sm tracking-widest uppercase">
                            No activity yet.
                        </span>
                    </div>
                )}

                {visibleCount > 0 && (
                    <ul role="list" className="space-y-3">
                        {events.map((event, index) => (
                            <ActivityRow
                                key={`${event.kind}-${event.timestamp ?? 'null'}-${index}`}
                                event={event}
                                index={index}
                            />
                        ))}
                    </ul>
                )}
            </div>
        </div>
    );
}

export default ActivityPane;
