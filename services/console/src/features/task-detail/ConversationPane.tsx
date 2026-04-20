import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
    User as UserIcon,
    Sparkles,
    PauseCircle,
    PlayCircle,
    StickyNote,
    Scissors,
    Info,
    ChevronDown,
    ChevronRight,
    AlertTriangle,
    Archive,
} from 'lucide-react';

import { api } from '@/api/client';
import type {
    ConversationEntry,
    ConversationEntryKind,
    ConversationListResponse,
    TaskStatus,
} from '@/types';

// ─── Terminal-status gate (drives polling) ─────────────────────────

const TERMINAL_STATUSES: ReadonlySet<TaskStatus> = new Set<TaskStatus>([
    'completed',
    'cancelled',
    'dead_letter',
]);

function isTerminalStatus(status?: TaskStatus): boolean {
    return !!status && TERMINAL_STATUSES.has(status);
}

// ─── Helpers ───────────────────────────────────────────────────────

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

function asString(value: unknown, fallback = ''): string {
    if (typeof value === 'string') return value;
    if (value == null) return fallback;
    return String(value);
}

const KNOWN_KINDS: ReadonlySet<string> = new Set<ConversationEntryKind>([
    'user_turn',
    'agent_turn',
    'tool_call',
    'tool_result',
    'compaction_boundary',
    'memory_flush',
    'hitl_pause',
    'hitl_resume',
    'system_note',
    'offload_emitted',
]);

function formatByteCount(bytes: number): string {
    if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
    if (bytes < 1024) return `${Math.round(bytes)} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ─── Expand/collapse fold ──────────────────────────────────────────

interface FoldProps {
    label: string;
    defaultOpen?: boolean;
    testId?: string;
    children: React.ReactNode;
}

function Fold({ label, defaultOpen = false, testId, children }: FoldProps) {
    const [open, setOpen] = useState(defaultOpen);
    return (
        <div className="border border-border/30 bg-black/30" data-testid={testId}>
            <button
                type="button"
                onClick={() => setOpen((o) => !o)}
                className="w-full flex items-center gap-2 px-3 py-2 text-[10px] font-bold uppercase tracking-[0.2em] text-muted-foreground hover:text-foreground transition-colors"
                aria-expanded={open}
            >
                {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                {label}
            </button>
            {open && <div className="border-t border-border/30 p-3">{children}</div>}
        </div>
    );
}

// ─── Individual entry renderers ────────────────────────────────────

function UserTurn({ entry }: { entry: ConversationEntry }) {
    const text = asString(entry.content.text, '(empty)');
    return (
        <div
            data-testid="conversation-entry-user_turn"
            className="flex justify-end animate-in fade-in duration-300"
        >
            <div className="max-w-[85%] bg-primary/15 border border-primary/30 rounded-2xl rounded-br-sm px-4 py-3">
                <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-primary mb-1">
                    <UserIcon className="w-3 h-3" /> You
                    <span className="ml-2 text-muted-foreground font-normal normal-case tracking-normal">
                        {new Date(entry.created_at).toLocaleTimeString()}
                    </span>
                </div>
                <div className="text-sm text-foreground whitespace-pre-wrap break-words leading-6">
                    {text}
                </div>
            </div>
        </div>
    );
}

function AgentTurn({ entry }: { entry: ConversationEntry }) {
    const text = asString(entry.content.text, '');
    return (
        <div
            data-testid="conversation-entry-agent_turn"
            className="flex justify-start animate-in fade-in duration-300"
        >
            <div className="max-w-[85%] bg-muted/5 border border-border/30 rounded-2xl rounded-bl-sm px-4 py-3">
                <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-muted-foreground mb-1">
                    <Sparkles className="w-3 h-3" /> Agent
                    <span className="ml-2 font-normal normal-case tracking-normal">
                        {new Date(entry.created_at).toLocaleTimeString()}
                    </span>
                </div>
                <div className="text-sm text-foreground whitespace-pre-wrap break-words leading-6">
                    {text}
                </div>
            </div>
        </div>
    );
}

function ToolCall({ entry }: { entry: ConversationEntry }) {
    const toolName = asString(entry.content.tool_name, '(unnamed tool)');
    const args = entry.content.args ?? {};
    return (
        <div
            data-testid="conversation-entry-tool_call"
            className="border border-warning/30 bg-warning/5 animate-in fade-in duration-300"
        >
            <Fold label={`Tool call → ${toolName}`}>
                <pre className="text-xs font-mono text-warning whitespace-pre-wrap break-all">
                    {formatJson(args)}
                </pre>
            </Fold>
        </div>
    );
}

function ToolResult({ entry }: { entry: ConversationEntry }) {
    const toolName = asString(entry.content.tool_name, '(unnamed tool)');
    const output = entry.content.output ?? entry.content.text ?? '';
    const capped = entry.metadata?.capped === true;
    const origBytes = entry.metadata?.orig_bytes;

    return (
        <div
            data-testid="conversation-entry-tool_result"
            className="border border-success/30 bg-success/5 animate-in fade-in duration-300 space-y-0"
        >
            <Fold label={`Tool result ← ${toolName}`}>
                {capped && (
                    <div
                        data-testid="conversation-tool-result-capped-notice"
                        className="mb-2 border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300 leading-5"
                    >
                        Tool returned {origBytes ?? '?'} bytes; showing head+tail capped at 25KB
                        (same view the model had).
                    </div>
                )}
                <pre className="text-xs font-mono text-success whitespace-pre-wrap break-all">
                    {typeof output === 'string' ? output : formatJson(output)}
                </pre>
            </Fold>
        </div>
    );
}

function CompactionBoundary({ entry }: { entry: ConversationEntry }) {
    const [open, setOpen] = useState(false);
    const first = entry.metadata?.first_turn_index ?? entry.content.first_turn_index;
    const last = entry.metadata?.last_turn_index ?? entry.content.last_turn_index;
    const turns = entry.metadata?.turns_summarized ?? entry.content.turns_summarized;
    const summaryText = asString(entry.content.summary_text, '');

    return (
        <div
            data-testid="conversation-entry-compaction_boundary"
            className="animate-in fade-in duration-300"
        >
            <button
                type="button"
                data-testid="conversation-compaction-divider"
                onClick={() => setOpen((o) => !o)}
                className="w-full flex items-center gap-2 text-xs font-mono text-muted-foreground hover:text-foreground transition-colors py-3 border-y border-dashed border-border/40"
                aria-expanded={open}
            >
                <Scissors className="w-3 h-3 shrink-0" />
                <span className="flex-1 text-left tracking-wide">
                    — Context summarized (turns {String(first ?? '?')}–{String(last ?? '?')},{' '}
                    {String(turns ?? '?')} turns) —
                </span>
                {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            </button>
            {open && (
                <div className="space-y-3 p-4 border border-border/30 bg-black/30 mt-2">
                    {summaryText && (
                        <div className="text-sm text-foreground whitespace-pre-wrap break-words leading-6">
                            {summaryText}
                        </div>
                    )}
                    <Fold
                        label="Operator details"
                        testId="conversation-operator-fold"
                    >
                        <dl className="grid grid-cols-2 gap-2 text-xs font-mono">
                            <dt className="text-muted-foreground">Summarizer model</dt>
                            <dd className="text-foreground break-all">
                                {asString(entry.metadata?.summarizer_model, '—')}
                            </dd>
                            <dt className="text-muted-foreground">Summary bytes</dt>
                            <dd className="text-foreground">
                                {entry.metadata?.summary_bytes ?? '—'}
                            </dd>
                            <dt className="text-muted-foreground">Cost (µUSD)</dt>
                            <dd className="text-foreground">
                                {entry.metadata?.cost_microdollars ?? '—'}
                            </dd>
                            <dt className="text-muted-foreground">Tier 3 firing index</dt>
                            <dd className="text-foreground">
                                {entry.metadata?.tier3_firing_index ?? '—'}
                            </dd>
                        </dl>
                    </Fold>
                </div>
            )}
        </div>
    );
}

function MemoryFlushBanner({ entry }: { entry: ConversationEntry }) {
    return (
        <div
            data-testid="conversation-entry-memory_flush"
            className="flex items-center gap-2 text-xs font-mono text-muted-foreground py-2 border-y border-dashed border-border/40 animate-in fade-in duration-300"
        >
            <StickyNote className="w-3 h-3 shrink-0" />
            <span>— Memory note injected —</span>
            <span className="ml-auto tabular-nums">
                {new Date(entry.created_at).toLocaleTimeString()}
            </span>
        </div>
    );
}

function HitlPauseBanner({ entry }: { entry: ConversationEntry }) {
    const reason = asString(entry.content.reason, 'awaiting operator');
    const prompt = asString(entry.content.prompt_to_user, '');
    return (
        <div
            data-testid="conversation-entry-hitl_pause"
            className="border border-amber-500/30 bg-amber-500/10 px-4 py-3 animate-in fade-in duration-300 space-y-2"
        >
            <div className="flex items-center gap-2 text-sm font-semibold text-amber-300">
                <PauseCircle className="w-4 h-4 shrink-0" />
                <span>⏸ Paused awaiting human approval: {reason}</span>
            </div>
            {prompt && (
                <div className="text-sm text-amber-200/80 whitespace-pre-wrap break-words leading-6 pl-6">
                    {prompt}
                </div>
            )}
        </div>
    );
}

function HitlResumeBanner({ entry }: { entry: ConversationEntry }) {
    const resolution = asString(entry.content.resolution, 'resumed');
    const note = asString(entry.content.user_note, '');
    return (
        <div
            data-testid="conversation-entry-hitl_resume"
            className="border border-green-500/30 bg-green-500/10 px-4 py-3 animate-in fade-in duration-300 space-y-2"
        >
            <div className="flex items-center gap-2 text-sm font-semibold text-green-300">
                <PlayCircle className="w-4 h-4 shrink-0" />
                <span>▶ Resumed: {resolution}</span>
            </div>
            {note && (
                <div className="text-sm text-green-200/80 whitespace-pre-wrap break-words leading-6 pl-6">
                    {note}
                </div>
            )}
        </div>
    );
}

function OffloadEmittedBanner({ entry }: { entry: ConversationEntry }) {
    // Payload shape (Task 5 §8): {count, total_bytes, step_index}.
    const rawCount = entry.content.count;
    const rawBytes = entry.content.total_bytes;
    const count = typeof rawCount === 'number' ? rawCount : Number(rawCount) || 0;
    const totalBytes = typeof rawBytes === 'number' ? rawBytes : Number(rawBytes) || 0;
    const label =
        count === 1
            ? `1 older tool output archived (${formatByteCount(totalBytes)})`
            : `${count} older tool outputs archived (${formatByteCount(totalBytes)})`;
    return (
        <div
            data-testid="conversation-entry-offload_emitted"
            className="flex items-center gap-2 text-xs font-mono text-muted-foreground/80 py-1.5 px-2 animate-in fade-in duration-300"
        >
            <Archive className="w-3 h-3 shrink-0" />
            <span className="truncate">— {label} —</span>
            <span className="ml-auto tabular-nums">
                {new Date(entry.created_at).toLocaleTimeString()}
            </span>
        </div>
    );
}

function SystemNoteBanner({ entry }: { entry: ConversationEntry }) {
    const text = asString(entry.content.text, '');
    return (
        <div
            data-testid="conversation-entry-system_note"
            className="border border-border/40 bg-muted/5 px-4 py-2 text-xs text-muted-foreground font-mono animate-in fade-in duration-300 flex items-start gap-2"
        >
            <Info className="w-3 h-3 shrink-0 mt-0.5" />
            <span className="whitespace-pre-wrap break-words">{text}</span>
        </div>
    );
}

function UnknownEntryBanner({ entry }: { entry: ConversationEntry }) {
    return (
        <div
            data-testid="conversation-entry-unknown"
            className="border border-border/40 bg-muted/5 animate-in fade-in duration-300"
        >
            <Fold
                label={`System event (kind="${entry.kind}", v${entry.content_version})`}
                testId="conversation-entry-unknown-fold"
            >
                <pre className="text-xs font-mono text-muted-foreground whitespace-pre-wrap break-all">
                    {formatJson({
                        sequence: entry.sequence,
                        kind: entry.kind,
                        content_version: entry.content_version,
                        content: entry.content,
                        metadata: entry.metadata,
                    })}
                </pre>
            </Fold>
        </div>
    );
}

function ConversationEntryRow({ entry }: { entry: ConversationEntry }) {
    // Fall back to neutral debug banner when content_version is ahead of this client.
    if (entry.content_version > 1 || !KNOWN_KINDS.has(String(entry.kind))) {
        return <UnknownEntryBanner entry={entry} />;
    }
    switch (entry.kind as ConversationEntryKind) {
        case 'user_turn':
            return <UserTurn entry={entry} />;
        case 'agent_turn':
            return <AgentTurn entry={entry} />;
        case 'tool_call':
            return <ToolCall entry={entry} />;
        case 'tool_result':
            return <ToolResult entry={entry} />;
        case 'compaction_boundary':
            return <CompactionBoundary entry={entry} />;
        case 'memory_flush':
            return <MemoryFlushBanner entry={entry} />;
        case 'hitl_pause':
            return <HitlPauseBanner entry={entry} />;
        case 'hitl_resume':
            return <HitlResumeBanner entry={entry} />;
        case 'system_note':
            return <SystemNoteBanner entry={entry} />;
        case 'offload_emitted':
            return <OffloadEmittedBanner entry={entry} />;
        default:
            return <UnknownEntryBanner entry={entry} />;
    }
}

// ─── Pane ──────────────────────────────────────────────────────────

interface ConversationPaneProps {
    taskId: string;
    status?: TaskStatus;
}

export function ConversationPane({ taskId, status }: ConversationPaneProps) {
    const terminal = isTerminalStatus(status);

    const { data, isLoading, isError, error } = useQuery<ConversationListResponse, Error>({
        queryKey: ['task-conversation', taskId],
        queryFn: () => api.listConversation(taskId),
        refetchInterval: terminal ? false : 5000,
        enabled: !!taskId,
    });

    const entries = useMemo<ConversationEntry[]>(() => data?.entries ?? [], [data]);
    const entryCount = entries.length;

    // Scroll / "new entries" pill state
    const scrollRef = useRef<HTMLDivElement>(null);
    const [atBottom, setAtBottom] = useState(true);
    const [newCount, setNewCount] = useState(0);
    const lastSeenCountRef = useRef(0);

    const handleScroll = useCallback(() => {
        const el = scrollRef.current;
        if (!el) return;
        const threshold = 32; // px from bottom considered "at bottom"
        const nearBottom = el.scrollHeight - el.clientHeight - el.scrollTop <= threshold;
        setAtBottom(nearBottom);
        if (nearBottom) {
            lastSeenCountRef.current = entryCount;
            setNewCount(0);
        }
    }, [entryCount]);

    // Autoscroll when already at bottom; otherwise track pending-new count.
    useEffect(() => {
        const el = scrollRef.current;
        if (!el) return;
        if (atBottom) {
            el.scrollTop = el.scrollHeight;
            lastSeenCountRef.current = entryCount;
            setNewCount(0);
        } else {
            const diff = entryCount - lastSeenCountRef.current;
            setNewCount(diff > 0 ? diff : 0);
        }
        // Intentionally depend only on entryCount + atBottom flag.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [entryCount]);

    const jumpToLatest = useCallback(() => {
        const el = scrollRef.current;
        if (!el) return;
        el.scrollTop = el.scrollHeight;
        lastSeenCountRef.current = entryCount;
        setNewCount(0);
        setAtBottom(true);
    }, [entryCount]);

    if (isError) {
        return (
            <div
                data-testid="conversation-pane"
                className="console-surface border-destructive/40 rounded-[24px] p-6"
                role="alert"
            >
                <div
                    data-testid="conversation-pane-error"
                    className="flex items-start gap-3 text-destructive"
                >
                    <AlertTriangle className="w-5 h-5 shrink-0 mt-0.5" />
                    <div className="space-y-1">
                        <div className="text-sm font-bold uppercase tracking-widest">
                            Failed to load conversation
                        </div>
                        <div className="text-xs font-mono opacity-80 break-all">
                            {error?.message ?? 'Unknown error'}
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div
            data-testid="conversation-pane"
            className="console-surface border-white/10 rounded-[24px] flex flex-col h-[560px] relative"
        >
            <div className="border-b border-white/8 px-6 py-4 shrink-0">
                <h3 className="text-sm font-display uppercase tracking-widest text-muted-foreground">
                    Conversation
                </h3>
                <p className="text-xs text-muted-foreground/70 mt-1">What the agent did</p>
            </div>

            <div
                ref={scrollRef}
                onScroll={handleScroll}
                className="flex-1 overflow-auto px-6 py-4 space-y-3"
                data-testid="conversation-scroll"
            >
                {isLoading && entries.length === 0 ? (
                    <div className="h-full flex items-center justify-center pt-20">
                        <span className="text-muted-foreground text-sm tracking-widest uppercase animate-pulse">
                            Loading conversation...
                        </span>
                    </div>
                ) : entries.length === 0 ? (
                    <div className="h-full flex items-center justify-center pt-20">
                        <span className="text-muted-foreground text-sm tracking-widest uppercase">
                            No conversation entries yet.
                        </span>
                    </div>
                ) : (
                    entries.map((entry) => (
                        <ConversationEntryRow key={entry.sequence} entry={entry} />
                    ))
                )}
            </div>

            {newCount > 0 && !atBottom && (
                <button
                    type="button"
                    data-testid="conversation-new-activity-pill"
                    onClick={jumpToLatest}
                    className="absolute bottom-4 left-1/2 -translate-x-1/2 bg-primary text-black text-xs font-bold uppercase tracking-widest px-4 py-2 rounded-full shadow-lg hover:saturate-150 transition-all"
                >
                    {newCount} new {newCount === 1 ? 'entry' : 'entries'} ↓
                </button>
            )}
        </div>
    );
}
