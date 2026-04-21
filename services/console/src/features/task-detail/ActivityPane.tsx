import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
    User as UserIcon,
    Sparkles,
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
import type { ActivityEvent, ActivityToolCall, TaskStatus } from '@/types';

/**
 * Phase 2 Track 7 Follow-up Task 8 (C) — unified Activity pane.
 *
 * Chat-bubble turns, colored fold-boxes for tool calls + tool results, dashed
 * dividers for infra markers. Matches the legacy ConversationPane visual
 * language so operators don't re-learn the page.
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
    if (typeof value === 'string') {
        try {
            return JSON.stringify(JSON.parse(value), null, 2);
        } catch {
            return value;
        }
    }
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

function formatTokenCount(n: number | undefined | null): string {
    if (n == null || !Number.isFinite(n)) return '–';
    if (n < 1000) return String(n);
    if (n < 100_000) return `${(n / 1000).toFixed(1)}k`;
    return `${Math.round(n / 1000)}k`;
}

function formatMicroUsd(microdollars: number | undefined | null): string | null {
    if (microdollars == null || !Number.isFinite(microdollars) || microdollars <= 0) {
        return null;
    }
    const usd = microdollars / 1_000_000;
    if (usd < 0.01) return `$${usd.toFixed(4)}`;
    if (usd < 1) return `$${usd.toFixed(3)}`;
    return `$${usd.toFixed(2)}`;
}

// The Activity API stringifies Anthropic content-block lists as Python reprs:
//   [{text=..., type=text}, {id=..., name=..., type=tool_use, input={...}}]
// For the assistant bubble we only want the prose text; tool_use blocks are
// surfaced separately via `tool_calls`. This extractor returns text-only
// content when the string looks like a list-of-blocks repr — including the
// pure-tool-use case (no `type=text` at all), which must yield `""` so the
// caller renders no bubble rather than leaking the raw repr.
function extractAssistantText(content: string | null | undefined): string {
    if (!content) return '';
    const trimmed = content.trim();
    const looksLikeBlocksRepr =
        trimmed.startsWith('[{') &&
        trimmed.endsWith('}]') &&
        (trimmed.includes('type=text') || trimmed.includes('type=tool_use'));
    if (!looksLikeBlocksRepr) return content;
    const textParts: string[] = [];
    const re = /text=([\s\S]*?), type=text\b/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(trimmed))) {
        textParts.push(m[1].trim());
    }
    return textParts.join('\n\n').trim();
}

function isTurn(kind: string): boolean {
    return kind.startsWith('turn.');
}

function isHitlPause(kind: string): boolean {
    return (
        kind === 'marker.hitl.paused' ||
        kind === 'marker.hitl.approval_requested' ||
        kind === 'marker.hitl.input_requested'
    );
}

function isHitlResume(kind: string): boolean {
    return (
        kind === 'marker.hitl.approved' ||
        kind === 'marker.hitl.rejected' ||
        kind === 'marker.hitl.input_received' ||
        kind === 'marker.hitl.resumed'
    );
}

function hasDetailsPayload(event: ActivityEvent): boolean {
    return !!(event.details && Object.keys(event.details).length > 0);
}

// ─── Primitives ────────────────────────────────────────────────────

interface FoldProps {
    label: React.ReactNode;
    tone?: 'warning' | 'success' | 'destructive' | 'muted';
    defaultOpen?: boolean;
    children: React.ReactNode;
}

function Fold({ label, tone = 'muted', defaultOpen = false, children }: FoldProps) {
    const [open, setOpen] = useState(defaultOpen);
    const toneClasses = {
        warning: 'border-warning/30 bg-warning/5 text-warning',
        success: 'border-success/30 bg-success/5 text-success',
        destructive: 'border-destructive/40 bg-destructive/5 text-destructive',
        muted: 'border-border/30 bg-black/20 text-muted-foreground',
    }[tone];
    return (
        <div className={`border rounded ${toneClasses}`}>
            <button
                type="button"
                onClick={() => setOpen((o) => !o)}
                aria-expanded={open}
                className="w-full flex items-center gap-2 px-3 py-2 text-[10px] font-bold uppercase tracking-[0.2em] hover:saturate-150 transition-colors"
            >
                {open ? (
                    <ChevronDown className="w-3 h-3 shrink-0" />
                ) : (
                    <ChevronRight className="w-3 h-3 shrink-0" />
                )}
                <span className="flex-1 text-left truncate">{label}</span>
            </button>
            {/* Always rendered (hidden when closed) so data-testids inside the
                fold remain queryable by tests + screen readers. */}
            <div
                hidden={!open}
                className={open ? 'border-t border-current/20 p-3' : ''}
            >
                {children}
            </div>
        </div>
    );
}

/**
 * Row-level "Show payload" affordance — reveals `event.details` JSON for
 * markers. Preserved verbatim for the existing Playwright + vitest contract
 * (`activity-row-<i>-expand` / `activity-row-<i>-details`).
 */
function DetailsAffordance({ event, index }: { event: ActivityEvent; index: number }) {
    const [open, setOpen] = useState(false);
    if (!hasDetailsPayload(event)) return null;
    return (
        <div>
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
                    {formatJson(event.details)}
                </pre>
            )}
        </div>
    );
}

// ─── Row renderers ─────────────────────────────────────────────────

interface RowProps {
    event: ActivityEvent;
    index: number;
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
            <div className="max-w-[85%] bg-primary/15 border border-primary/30 rounded-2xl rounded-br-sm px-4 py-3">
                <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-primary mb-1">
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
                    {truncate(text) || (
                        <span className="text-muted-foreground">(empty)</span>
                    )}
                </div>
            </div>
        </li>
    );
}

function AssistantTurnRow({ event, index }: RowProps) {
    const text = extractAssistantText(event.content);
    const toolCalls: ActivityToolCall[] = event.tool_calls ?? [];
    const hasText = !!text;
    const hasToolCalls = toolCalls.length > 0;
    const usage = event.usage ?? undefined;
    const cost = formatMicroUsd(event.cost_microdollars);
    const showUsage = !!usage && (usage.input_tokens != null || usage.output_tokens != null);

    // Tool-call folds render full-width (alongside the TOOL RESULT folds
    // that follow), while the pill + prose bubble stay constrained to the
    // 85% reading column that distinguishes "agent speech" from "tool
    // invocations". Wrapping them in the same <li> keeps them semantically
    // bound to one turn.
    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            className="list-none animate-in fade-in duration-300 space-y-2"
        >
            <div className="flex justify-start">
                <div className="max-w-[85%] w-full space-y-2">
                    <div
                        data-testid={`activity-row-${index}-pill`}
                        className="inline-flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-muted-foreground bg-muted/5 border border-border/30 rounded-full px-3 py-1"
                    >
                        <Sparkles className="w-3 h-3" /> Agent
                        {event.timestamp && (
                            <span className="ml-1 text-muted-foreground/80 font-normal normal-case tracking-normal">
                                {formatTime(event.timestamp)}
                            </span>
                        )}
                        {showUsage && (
                            <span
                                data-testid={`activity-row-${index}-usage`}
                                className="ml-1 font-mono normal-case tracking-normal text-primary/80"
                                title={`input_tokens=${usage!.input_tokens ?? '?'}, output_tokens=${usage!.output_tokens ?? '?'}, total_tokens=${usage!.total_tokens ?? '?'}`}
                            >
                                {formatTokenCount(usage!.input_tokens)} in
                                {' → '}
                                {formatTokenCount(usage!.output_tokens)} out
                            </span>
                        )}
                        {cost && (
                            <span
                                data-testid={`activity-row-${index}-cost`}
                                className="font-mono normal-case tracking-normal text-success/80"
                            >
                                · {cost}
                            </span>
                        )}
                    </div>
                    {hasText && (
                        <div className="bg-muted/5 border border-border/30 rounded-2xl rounded-bl-sm px-4 py-3">
                            <div
                                data-testid={`activity-row-${index}-content`}
                                className="text-sm text-foreground whitespace-pre-wrap break-words leading-6"
                            >
                                {truncate(text)}
                            </div>
                        </div>
                    )}
                    {!hasText && (
                        // Still expose the testid so tests / a11y consumers
                        // can locate the turn even when the assistant produced
                        // only tool_use blocks (empty prose).
                        <div
                            data-testid={`activity-row-${index}-content`}
                            className="sr-only"
                        >
                            {event.content ?? ''}
                        </div>
                    )}
                </div>
            </div>
            {hasToolCalls && (
                <div className="space-y-2">
                    {toolCalls.map((tc, i) => (
                        <Fold
                            key={tc.id ?? `${tc.name}-${i}`}
                            tone="warning"
                            label={
                                <>
                                    Tool call → <span className="font-mono normal-case tracking-normal">{tc.name || '(tool)'}</span>
                                </>
                            }
                        >
                            <pre className="text-xs font-mono text-warning whitespace-pre-wrap break-all">
                                {formatJson(tc.args ?? {})}
                            </pre>
                        </Fold>
                    ))}
                </div>
            )}
        </li>
    );
}

function ToolTurnRow({ event, index }: RowProps) {
    const text = event.content ?? '';
    const err = !!event.is_error;
    const toolName = event.tool_name || '(tool)';

    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            className="list-none animate-in fade-in duration-300"
        >
            <Fold
                tone={err ? 'destructive' : 'success'}
                label={
                    <span className="flex items-center gap-2">
                        Tool result ← <span className="font-mono normal-case tracking-normal">{toolName}</span>
                        {err && (
                            <span className="inline-flex items-center gap-1 text-destructive normal-case tracking-normal font-semibold">
                                <AlertTriangle className="w-3 h-3" /> error
                            </span>
                        )}
                        {event.timestamp && (
                            <span
                                data-testid={`activity-row-${index}-timestamp`}
                                className="ml-auto font-mono normal-case tracking-normal text-muted-foreground tabular-nums"
                            >
                                {formatTime(event.timestamp)}
                            </span>
                        )}
                    </span>
                }
            >
                <pre
                    data-testid={`activity-row-${index}-content`}
                    className={`text-xs font-mono whitespace-pre-wrap break-all ${err ? 'text-destructive' : 'text-success'}`}
                >
                    {truncate(text)}
                </pre>
            </Fold>
            {/* Tool result rows don't normally carry a separate `details`
                payload, but if the server attaches one keep the chevron
                available so the API contract never silently swallows it. */}
            {hasDetailsPayload(event) && (
                <div className="mt-1 px-2">
                    <DetailsAffordance event={event} index={index} />
                </div>
            )}
            {!hasDetailsPayload(event) && err && (
                <span
                    data-testid={`activity-row-${index}-content-err-marker`}
                    className="sr-only"
                >
                    error
                </span>
            )}
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
                <DetailsAffordance event={event} index={index} />
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
                <span data-testid={`activity-row-${index}-content`} className="flex-1">
                    — Memory note injected{event.summary_text ? `: ${event.summary_text}` : ''} —
                </span>
                {event.timestamp && (
                    <span className="tabular-nums">{formatTime(event.timestamp)}</span>
                )}
            </div>
            <div className="mx-4 pt-1">
                <DetailsAffordance event={event} index={index} />
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
                <span data-testid={`activity-row-${index}-content`} className="flex-1 truncate">
                    — {summary} —
                </span>
                {event.timestamp && (
                    <span className="tabular-nums">{formatTime(event.timestamp)}</span>
                )}
            </div>
            <div className="mx-4">
                <DetailsAffordance event={event} index={index} />
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
                <DetailsAffordance event={event} index={index} />
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
                <span data-testid={`activity-row-${index}-content`} className="flex-1">
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
                <DetailsAffordance event={event} index={index} />
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
                <span data-testid={`activity-row-${index}-content`} className="flex-1">
                    ⏸ Paused — {reason}
                    {tool ? ` (tool: ${tool})` : ''}
                </span>
                {event.timestamp && (
                    <span className="ml-auto text-xs font-mono font-normal text-amber-200/70 tabular-nums">
                        {formatTime(event.timestamp)}
                    </span>
                )}
            </div>
            <DetailsAffordance event={event} index={index} />
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
                <span data-testid={`activity-row-${index}-content`} className="flex-1">
                    ▶ Resumed — {resolution}
                </span>
                {event.timestamp && (
                    <span className="ml-auto text-xs font-mono font-normal text-green-200/70 tabular-nums">
                        {formatTime(event.timestamp)}
                    </span>
                )}
            </div>
            <DetailsAffordance event={event} index={index} />
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

    const turnCount = useMemo(
        () => events.reduce((n, e) => (isTurn(e.kind) ? n + 1 : n), 0),
        [events],
    );

    return (
        <div
            data-testid="activity-pane"
            className="console-surface border-white/10 rounded-[24px] flex flex-col h-[640px] relative"
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
                        {turnCount} {turnCount === 1 ? 'turn' : 'turns'}
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
