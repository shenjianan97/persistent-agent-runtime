import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
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

function formatDuration(ms: number | null): string | null {
    if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
    if (ms < 1000) return `Δ ${Math.round(ms)}ms`;
    if (ms < 60_000) return `Δ ${(ms / 1000).toFixed(1)}s`;
    const totalSec = Math.round(ms / 1000);
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    return `Δ ${m}m ${s}s`;
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
    durationMs?: number | null;
    cumulativeCostMicrodollars?: number | null;
    highlightedToolCallId?: string | null;
    setHighlightedToolCallId?: (id: string | null) => void;
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

function AssistantTurnRow({
    event,
    index,
    durationMs,
    cumulativeCostMicrodollars,
    highlightedToolCallId,
    setHighlightedToolCallId,
}: RowProps) {
    const text = event.content ?? '';
    const toolCalls: ActivityToolCall[] = event.tool_calls ?? [];
    const hasText = !!text;
    const hasToolCalls = toolCalls.length > 0;
    const usage = event.usage ?? undefined;
    const cost = formatMicroUsd(event.cost_microdollars);
    const showUsage = !!usage && (usage.input_tokens != null || usage.output_tokens != null);
    const duration = formatDuration(durationMs ?? null);
    // Cumulative-cost "so far" — only render when cumulative strictly exceeds
    // the current turn's cost (i.e. skip the first assistant turn where both
    // values are identical).
    const cumulative =
        cumulativeCostMicrodollars != null &&
        event.cost_microdollars != null &&
        cumulativeCostMicrodollars > (event.cost_microdollars ?? 0)
            ? formatMicroUsd(cumulativeCostMicrodollars)
            : null;

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
                        {cumulative && (
                            <span
                                data-testid={`activity-row-${index}-cumulative-cost`}
                                className="font-mono normal-case tracking-normal text-success/60"
                            >
                                ({cumulative} so far)
                            </span>
                        )}
                        {duration && (
                            <span
                                data-testid={`activity-row-${index}-duration`}
                                className="font-mono normal-case tracking-normal text-muted-foreground/70"
                            >
                                · {duration}
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
                        // Content arrives pre-normalized from the server
                        // (ActivityProjectionService.extractMessageContent /
                        // MessageContentExtractor on the Java side). A
                        // tool-only assistant turn therefore has content = ''
                        // already — no client-side provider parsing needed.
                        // We keep the empty testid anchor so tests + the
                        // existing a11y contract can still locate the row.
                        <div
                            data-testid={`activity-row-${index}-content`}
                            className="sr-only"
                            aria-hidden="true"
                        />
                    )}
                </div>
            </div>
            {hasToolCalls && (
                <div className="space-y-2">
                    {toolCalls.map((tc, i) => {
                        const highlighted =
                            !!tc.id && !!highlightedToolCallId && tc.id === highlightedToolCallId;
                        return (
                            <div
                                key={tc.id ?? `${tc.name}-${i}`}
                                data-tool-call-id={tc.id ?? undefined}
                                onMouseEnter={
                                    tc.id && setHighlightedToolCallId
                                        ? () => setHighlightedToolCallId(tc.id ?? null)
                                        : undefined
                                }
                                onMouseLeave={
                                    setHighlightedToolCallId
                                        ? () => setHighlightedToolCallId(null)
                                        : undefined
                                }
                                className={`transition-shadow rounded ${highlighted ? 'ring-2 ring-primary/60' : ''}`}
                            >
                                <Fold
                                    tone="warning"
                                    label={
                                        <span className="flex items-center gap-2">
                                            Tool call → <span className="font-mono normal-case tracking-normal">{tc.name || '(tool)'}</span>
                                            {event.timestamp && (
                                                <span
                                                    data-testid={`activity-row-${index}-tool-call-${i}-timestamp`}
                                                    className="ml-auto font-mono normal-case tracking-normal text-muted-foreground tabular-nums"
                                                >
                                                    {formatTime(event.timestamp)}
                                                </span>
                                            )}
                                        </span>
                                    }
                                >
                                    <pre className="text-xs font-mono text-warning whitespace-pre-wrap break-all">
                                        {formatJson(tc.args ?? {})}
                                    </pre>
                                </Fold>
                            </div>
                        );
                    })}
                </div>
            )}
        </li>
    );
}

function ToolTurnRow({
    event,
    index,
    durationMs,
    highlightedToolCallId,
    setHighlightedToolCallId,
}: RowProps) {
    const text = event.content ?? '';
    const err = !!event.is_error;
    const toolName = event.tool_name || '(tool)';
    const duration = formatDuration(durationMs ?? null);
    const origBytes = event.orig_bytes;
    const contentLen = text.length;
    const showByteCapNotice =
        origBytes != null && Number.isFinite(origBytes) && origBytes > contentLen;
    const toolCallId = event.tool_call_id ?? null;
    const highlighted =
        !!toolCallId && !!highlightedToolCallId && toolCallId === highlightedToolCallId;

    return (
        <li
            role="listitem"
            data-testid={`activity-row-${index}`}
            data-kind={event.kind}
            data-tool-call-id={toolCallId ?? undefined}
            onMouseEnter={
                toolCallId && setHighlightedToolCallId
                    ? () => setHighlightedToolCallId(toolCallId)
                    : undefined
            }
            onMouseLeave={
                setHighlightedToolCallId
                    ? () => setHighlightedToolCallId(null)
                    : undefined
            }
            className={`list-none animate-in fade-in duration-300 rounded transition-shadow ${highlighted ? 'ring-2 ring-primary/60' : ''}`}
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
                        {duration && (
                            <span
                                data-testid={`activity-row-${index}-duration`}
                                className="font-mono normal-case tracking-normal text-muted-foreground/70"
                            >
                                {duration}
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
                {showByteCapNotice && (
                    <div
                        data-testid={`activity-row-${index}-byte-cap-notice`}
                        className="mb-2 text-[11px] font-mono text-muted-foreground bg-muted/10 border border-border/30 rounded px-2 py-1"
                    >
                        <Info className="w-3 h-3 inline-block mr-1 align-[-2px]" />
                        Tool returned {origBytes} B; showing head+tail capped view (same view the model saw)
                    </div>
                )}
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
    const et = event.event_type;
    if (et === 'task_dead_lettered') {
        const reason = event.details?.reason as string | undefined;
        const errorCode = event.details?.error_code as string | undefined;
        return (
            <li
                role="listitem"
                data-testid={`activity-row-${index}`}
                data-kind={event.kind}
                className="list-none animate-in fade-in duration-300 border border-destructive/40 bg-destructive/10 rounded-lg px-4 py-3 space-y-2"
            >
                <div className="flex items-center gap-2 text-sm font-semibold text-destructive">
                    <AlertTriangle className="w-4 h-4 shrink-0" />
                    <span data-testid={`activity-row-${index}-content`} className="flex-1">
                        Task failed
                        {reason ? ` — ${reason}` : ''}
                        {errorCode ? ` (${errorCode})` : ''}
                    </span>
                    {event.timestamp && (
                        <span className="ml-auto text-xs font-mono font-normal text-destructive/70 tabular-nums">
                            {formatTime(event.timestamp)}
                        </span>
                    )}
                </div>
                <DetailsAffordance event={event} index={index} />
            </li>
        );
    }
    if (et === 'task_redriven') {
        const resumedFrom = event.details?.resumed_from_step as
            | number
            | string
            | undefined;
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
                        Resumed from checkpoint
                        {resumedFrom != null ? ` — step ${resumedFrom}` : ''}
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
    const budgetPerTask = event.details?.budget_limit_per_task_microdollars as
        | number
        | undefined;
    const budgetPerHour = event.details?.budget_limit_per_hour_microdollars as
        | number
        | undefined;
    const observedCost = event.details?.observed_cost_microdollars as
        | number
        | undefined;
    const perTask = formatMicroUsd(budgetPerTask);
    const perHour = formatMicroUsd(budgetPerHour);
    const observed = formatMicroUsd(observedCost);
    const chips: { k: string; v: string }[] = [];
    if (perTask) chips.push({ k: 'Budget', v: `${perTask}/task` });
    if (perHour) chips.push({ k: '', v: `${perHour}/hr` });
    if (observed) chips.push({ k: 'Observed', v: observed });
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
            {chips.length > 0 && (
                <dl className="flex flex-wrap gap-2 text-[11px] font-mono text-amber-200/80">
                    {chips.map((c, i) => (
                        <div
                            key={i}
                            className="inline-flex items-center gap-1 bg-amber-500/10 border border-amber-500/20 rounded px-2 py-0.5"
                        >
                            {c.k && <dt className="font-semibold">{c.k}:</dt>}
                            <dd>{c.v}</dd>
                        </div>
                    ))}
                </dl>
            )}
            <DetailsAffordance event={event} index={index} />
        </li>
    );
}

function HitlResumeMarkerRow({ event, index }: RowProps) {
    const resolution =
        (event.details?.resolution as string | undefined) ??
        event.summary_text ??
        'resumed';
    const resumeTrigger = event.details?.resume_trigger as string | undefined;
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
            {resumeTrigger && (
                <dl className="flex flex-wrap gap-2 text-[11px] font-mono text-green-200/80">
                    <div className="inline-flex items-center gap-1 bg-green-500/10 border border-green-500/20 rounded px-2 py-0.5">
                        <dt className="font-semibold">Resume:</dt>
                        <dd>{resumeTrigger}</dd>
                    </div>
                </dl>
            )}
            <DetailsAffordance event={event} index={index} />
        </li>
    );
}

function ActivityRow(props: RowProps) {
    const { event } = props;
    switch (event.kind) {
        case 'turn.user':
            return <UserTurnRow {...props} />;
        case 'turn.assistant':
            return <AssistantTurnRow {...props} />;
        case 'turn.tool':
            return <ToolTurnRow {...props} />;
        case 'marker.compaction_fired':
            return <CompactionMarkerRow {...props} />;
        case 'marker.memory_flush':
            return <MemoryFlushMarkerRow {...props} />;
        case 'marker.offload_emitted':
            return <OffloadMarkerRow {...props} />;
        case 'marker.system_note':
            return <SystemNoteMarkerRow {...props} />;
        case 'marker.lifecycle':
            return <LifecycleMarkerRow {...props} />;
        default:
            if (isHitlPause(event.kind)) {
                return <HitlPauseMarkerRow {...props} />;
            }
            if (isHitlResume(event.kind)) {
                return <HitlResumeMarkerRow {...props} />;
            }
            return <SystemNoteMarkerRow {...props} />;
    }
}

// ─── Pane ──────────────────────────────────────────────────────────

interface ActivityPaneProps {
    taskId: string;
    status?: TaskStatus;
}

export function ActivityPane({ taskId, status }: ActivityPaneProps) {
    const [showDetails, setShowDetails] = useState(false);
    const [highlightedToolCallId, setHighlightedToolCallId] = useState<string | null>(
        null,
    );

    const query = useQuery({
        queryKey: ['task-activity', taskId, showDetails],
        queryFn: () => api.listActivity(taskId, showDetails),
        enabled: !!taskId,
        refetchInterval: isTerminalStatus(status) ? false : 3_000,
        refetchOnWindowFocus: false,
    });

    // When the task transitions from non-terminal to terminal, the poll
    // loop stops immediately — but the last actual fetch happened up to
    // 3s before the final checkpoint landed, so the Activity pane renders
    // stale events missing the final turn until the user refreshes.
    // Force one refetch on the transition to pick up the terminal
    // checkpoint. We track the previous status in a ref to fire exactly
    // once per transition, not on every re-render.
    const prevStatusRef = useRef<TaskStatus | undefined>(status);
    useEffect(() => {
        const prev = prevStatusRef.current;
        if (prev && !isTerminalStatus(prev) && isTerminalStatus(status)) {
            query.refetch();
        }
        prevStatusRef.current = status;
    }, [status, query]);

    const events = query.data?.events ?? [];
    const truncated = query.data?.truncated === true;
    const visibleCount = events.length;
    const isEmpty = !query.isLoading && visibleCount === 0;

    const turnCount = useMemo(
        () => events.reduce((n, e) => (isTurn(e.kind) ? n + 1 : n), 0),
        [events],
    );

    // ─── Per-index memos for duration, cumulative cost, handoff ──────
    const durationByIndex = useMemo<(number | null)[]>(() => {
        return events.map((e, i) => {
            if (i === 0) return null;
            const prev = events[i - 1];
            if (!e.timestamp || !prev.timestamp) return null;
            const cur = new Date(e.timestamp).getTime();
            const prv = new Date(prev.timestamp).getTime();
            if (!Number.isFinite(cur) || !Number.isFinite(prv)) return null;
            const delta = cur - prv;
            return delta >= 0 ? delta : null;
        });
    }, [events]);

    const cumulativeCostByIndex = useMemo<(number | null)[]>(() => {
        let running = 0;
        return events.map((e) => {
            if (e.kind === 'turn.assistant' && e.cost_microdollars != null) {
                running += e.cost_microdollars;
                return running;
            }
            return null;
        });
    }, [events]);

    const handoffByIndex = useMemo<(string | null)[]>(() => {
        return events.map((e, i) => {
            if (i === 0) return null;
            const prev = events[i - 1];
            const cur = e.worker_id;
            const prv = prev.worker_id;
            if (!cur || !prv) return null;
            if (cur === prv) return null;
            const shortPrev = prv.slice(0, 8);
            const shortCur = cur.slice(0, 8);
            return `${shortPrev} → ${shortCur}`;
        });
    }, [events]);

    // ─── Scroll tracking: auto-scroll to bottom when at bottom; pause
    //     + show "jump to latest" pill when user scrolls up.
    const scrollRef = useRef<HTMLDivElement | null>(null);
    const [atBottom, setAtBottom] = useState(true);
    const [lastSeenCount, setLastSeenCount] = useState(0);

    const onScroll = () => {
        const el = scrollRef.current;
        if (!el) return;
        const bottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        setAtBottom(bottom);
        if (bottom) {
            setLastSeenCount(visibleCount);
        }
    };

    // Auto-scroll on event growth when user is at bottom. useLayoutEffect so
    // the scroll happens synchronously after DOM updates — avoids visible
    // jank from a frame where the new content is below the viewport.
    useLayoutEffect(() => {
        const el = scrollRef.current;
        if (!el) return;
        if (atBottom) {
            el.scrollTop = el.scrollHeight;
            setLastSeenCount(visibleCount);
        }
    }, [visibleCount, atBottom]);

    // Reset "seen" counter when scrolled-to-bottom transitions to true.
    useEffect(() => {
        if (atBottom) setLastSeenCount(visibleCount);
    }, [atBottom, visibleCount]);

    const newCount = atBottom ? 0 : Math.max(0, visibleCount - lastSeenCount);

    const jumpToLatest = () => {
        const el = scrollRef.current;
        if (!el) return;
        el.scrollTop = el.scrollHeight;
        setAtBottom(true);
        setLastSeenCount(visibleCount);
    };

    // ─── Running / awaiting indicator ────────────────────────────────
    const showRunningIndicator = !isTerminalStatus(status) && visibleCount > 0;
    const lastEvent = visibleCount > 0 ? events[visibleCount - 1] : null;
    let runningLabel = 'Running compute…';
    let runningDotClass = 'bg-primary';
    if (lastEvent?.kind === 'marker.hitl.paused') {
        runningLabel = 'Awaiting approval…';
        runningDotClass = 'bg-amber-400';
    } else if (lastEvent?.kind === 'marker.hitl.input_requested') {
        runningLabel = 'Awaiting input…';
        runningDotClass = 'bg-amber-400';
    }

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

            <div
                ref={scrollRef}
                onScroll={onScroll}
                className="flex-1 overflow-auto px-6 py-4 space-y-3"
            >
                {truncated && (
                    <div
                        data-testid="activity-truncation-notice"
                        className="flex items-start gap-2 text-xs font-mono text-amber-300 border border-amber-500/40 bg-amber-500/10 rounded-lg px-3 py-2"
                    >
                        <Info className="w-4 h-4 shrink-0 mt-0.5" />
                        <span>
                            Showing first 2000 of many events. Pagination coming soon.
                        </span>
                    </div>
                )}

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
                        {events.map((event, index) => {
                            const handoff = handoffByIndex[index];
                            return (
                                <Fragment
                                    key={`${event.kind}-${event.timestamp ?? 'null'}-${index}`}
                                >
                                    {handoff && (
                                        <li
                                            role="listitem"
                                            data-testid={`activity-handoff-${index}`}
                                            className="list-none flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.2em] text-muted-foreground/80 py-1 px-2 border-y border-dashed border-border/30"
                                        >
                                            <ActivityIcon className="w-3 h-3 shrink-0" />
                                            <span>Handoff: {handoff}</span>
                                        </li>
                                    )}
                                    <ActivityRow
                                        event={event}
                                        index={index}
                                        durationMs={durationByIndex[index]}
                                        cumulativeCostMicrodollars={
                                            cumulativeCostByIndex[index]
                                        }
                                        highlightedToolCallId={highlightedToolCallId}
                                        setHighlightedToolCallId={
                                            setHighlightedToolCallId
                                        }
                                    />
                                </Fragment>
                            );
                        })}
                    </ul>
                )}

                {showRunningIndicator && (
                    <div
                        data-testid="activity-running-indicator"
                        className="flex items-center gap-2 text-xs font-mono text-muted-foreground py-2 px-2"
                    >
                        <span
                            className={`inline-block w-2 h-2 rounded-full animate-pulse ${runningDotClass}`}
                        />
                        <span>{runningLabel}</span>
                    </div>
                )}
            </div>

            {newCount > 0 && (
                <button
                    type="button"
                    data-testid="activity-jump-to-latest"
                    onClick={jumpToLatest}
                    className="absolute bottom-4 left-1/2 -translate-x-1/2 inline-flex items-center gap-2 text-xs font-mono bg-primary text-primary-foreground rounded-full px-3 py-1.5 shadow-lg hover:bg-primary/90 transition-colors"
                >
                    {newCount} new
                    <ChevronDown className="w-3 h-3" />
                </button>
            )}
        </div>
    );
}

export default ActivityPane;
