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
 * <p>Replaces the dual "Conversation" + "Execution Timeline" split with a
 * single view sourced from `GET /v1/tasks/{taskId}/activity`. Turns (user /
 * assistant / tool) render chat-style; markers (compaction, HITL, offload,
 * memory flush, lifecycle) render as inline chips.
 *
 * <p>Header toggle "Show details" requests the server with
 * {@code include_details=true}, un-filtering infrastructure markers. Per-
 * row expander (chevron) reveals the raw payload + timestamps.
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

function formatTs(raw?: string | null): string {
    if (!raw) return '';
    try {
        const d = new Date(raw);
        if (Number.isNaN(d.getTime())) return raw;
        return d.toLocaleString();
    } catch {
        return raw;
    }
}

function truncate(value: string, max = 400): string {
    if (value.length <= max) return value;
    return `${value.slice(0, max)}…`;
}

function kindLabel(kind: string): string {
    switch (kind) {
        case 'turn.user': return 'User';
        case 'turn.assistant': return 'Assistant';
        case 'turn.tool': return 'Tool Result';
        case 'marker.compaction_fired': return 'Compaction';
        case 'marker.memory_flush': return 'Memory Flush';
        case 'marker.offload_emitted': return 'Offload';
        case 'marker.system_note': return 'System Note';
        case 'marker.lifecycle': return 'Lifecycle';
        case 'marker.hitl.paused':
        case 'marker.hitl.approval_requested':
        case 'marker.hitl.input_requested':
            return 'HITL Pause';
        case 'marker.hitl.approved':
        case 'marker.hitl.rejected':
        case 'marker.hitl.input_received':
        case 'marker.hitl.resumed':
            return 'HITL Resume';
        default:
            return kind.startsWith('marker.') ? 'Marker' : 'Turn';
    }
}

function kindIcon(kind: string) {
    if (kind === 'turn.user') return UserIcon;
    if (kind === 'turn.assistant') return Sparkles;
    if (kind === 'turn.tool') return Wrench;
    if (kind === 'marker.compaction_fired') return Scissors;
    if (kind === 'marker.memory_flush') return StickyNote;
    if (kind === 'marker.offload_emitted') return Archive;
    if (kind === 'marker.system_note') return Info;
    if (kind.startsWith('marker.hitl.paused') || kind.endsWith('_requested')) return PauseCircle;
    if (kind.startsWith('marker.hitl.') && (kind.endsWith('_received') || kind.endsWith('_resumed') || kind.endsWith('_approved') || kind.endsWith('_rejected'))) return PlayCircle;
    if (kind === 'marker.lifecycle') return ActivityIcon;
    return Info;
}

function isTurn(kind: string): boolean {
    return kind.startsWith('turn.');
}

interface ActivityRowProps {
    event: ActivityEvent;
    index: number;
}

function ActivityRow({ event, index }: ActivityRowProps) {
    const [expanded, setExpanded] = useState(false);
    const Icon = kindIcon(event.kind);
    const turn = isTurn(event.kind);

    const header = (
        <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider">
            <Icon className="w-3.5 h-3.5" aria-hidden />
            <span>{kindLabel(event.kind)}</span>
            {event.timestamp && (
                <span className="font-normal normal-case tracking-normal text-muted-foreground">
                    {formatTs(event.timestamp)}
                </span>
            )}
        </div>
    );

    const body = turn ? (
        <div className="space-y-1">
            {event.tool_name && (
                <div className="text-xs text-muted-foreground">
                    <span className="font-semibold">{event.tool_name}</span>
                    {event.is_error ? (
                        <span className="ml-2 inline-flex items-center gap-1 text-red-400">
                            <AlertTriangle className="w-3 h-3" aria-hidden /> error
                        </span>
                    ) : null}
                </div>
            )}
            {event.content && (
                <pre
                    className="whitespace-pre-wrap font-normal text-sm"
                    data-testid={`activity-row-${index}-content`}
                >
                    {truncate(event.content)}
                </pre>
            )}
            {event.tool_calls && event.tool_calls.length > 0 && (
                <div className="text-xs text-muted-foreground">
                    Requested: {event.tool_calls.map(c => c.name || 'tool').join(', ')}
                </div>
            )}
        </div>
    ) : (
        <div className="space-y-1">
            {event.summary_text && (
                <div className="text-sm">{truncate(event.summary_text, 600)}</div>
            )}
            {!event.summary_text && event.content && (
                <div className="text-sm">{truncate(event.content)}</div>
            )}
            {event.event_type && (
                <div className="text-[11px] text-muted-foreground">
                    {event.event_type}
                    {event.status_before && event.status_after && (
                        <span> · {event.status_before} → {event.status_after}</span>
                    )}
                </div>
            )}
        </div>
    );

    const hasDetailsToExpand = !!(event.details && Object.keys(event.details).length > 0) || !!event.tool_calls;

    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            className={
                'rounded border border-white/10 p-3 space-y-2 ' +
                (turn ? 'bg-white/2' : 'bg-white/4')
            }
        >
            <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0 space-y-2">
                    {header}
                    {body}
                </div>
                {hasDetailsToExpand && (
                    <button
                        type="button"
                        data-testid={`activity-row-${index}-expand`}
                        onClick={() => setExpanded(v => !v)}
                        aria-expanded={expanded}
                        aria-label={expanded ? 'Hide details' : 'Show details'}
                        className="shrink-0 text-muted-foreground hover:text-foreground"
                    >
                        {expanded
                            ? <ChevronDown className="w-4 h-4" />
                            : <ChevronRight className="w-4 h-4" />}
                    </button>
                )}
            </div>
            {expanded && (
                <pre
                    data-testid={`activity-row-${index}-details`}
                    className="text-[11px] bg-black/20 rounded p-2 overflow-auto max-h-72 whitespace-pre-wrap"
                >
                    {formatJson({
                        tool_calls: event.tool_calls ?? undefined,
                        details: event.details ?? undefined,
                    })}
                </pre>
            )}
        </li>
    );
}

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
        // Poll while running; stop once terminal.
        refetchInterval: isTerminalStatus(status) ? false : 3_000,
        refetchOnWindowFocus: false,
    });

    const events = query.data?.events ?? [];
    const visibleCount = events.length;

    const isEmpty = !query.isLoading && visibleCount === 0;

    const summary = useMemo(() => {
        const counts: Record<string, number> = {};
        for (const e of events) {
            const bucket = isTurn(e.kind) ? 'turns' : 'markers';
            counts[bucket] = (counts[bucket] ?? 0) + 1;
        }
        return counts;
    }, [events]);

    return (
        <div
            data-testid="activity-pane"
            className="space-y-3"
        >
            <div className="flex items-center justify-between">
                <div className="text-xs text-muted-foreground">
                    <span data-testid="activity-summary">
                        {summary.turns ?? 0} turns · {summary.markers ?? 0} markers
                    </span>
                </div>
                <label
                    className="flex items-center gap-2 text-xs text-muted-foreground cursor-pointer select-none"
                    data-testid="activity-details-toggle-label"
                >
                    <input
                        type="checkbox"
                        data-testid="activity-details-toggle"
                        checked={showDetails}
                        onChange={e => setShowDetails(e.target.checked)}
                    />
                    Show details
                </label>
            </div>

            {query.isLoading && (
                <div data-testid="activity-loading" className="text-sm text-muted-foreground">
                    Loading activity…
                </div>
            )}

            {query.isError && (
                <div
                    data-testid="activity-error"
                    role="alert"
                    className="text-sm text-red-400"
                >
                    Failed to load activity: {(query.error as Error)?.message ?? 'unknown error'}
                </div>
            )}

            {isEmpty && !query.isError && (
                <div
                    data-testid="activity-empty"
                    className="text-sm text-muted-foreground"
                >
                    No activity yet.
                </div>
            )}

            {visibleCount > 0 && (
                <ul role="list" className="space-y-2">
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
    );
}

export default ActivityPane;
