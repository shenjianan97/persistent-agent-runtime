import { useEffect, useMemo, useRef, useState } from 'react';
import { Brain, ChevronDown, Search, X, CheckCircle2, AlertCircle } from 'lucide-react';
import {
    useAgentMemoryList,
    useAgentMemorySearch,
} from '@/features/agents/memory/hooks';
import type { MemoryEntrySummary } from '@/types';
import { Button } from '@/components/ui/button';

/** Selection cap — mirrors Task 4's server-side 50-entry limit. */
export const MAX_ATTACHED_MEMORIES = 50;

interface AttachMemoryPickerProps {
    agentId: string;
    /** Selected memory ids in position / selection order. */
    value: string[];
    onChange: (ids: string[]) => void;
    /**
     * Optional pre-resolved summaries for the current selection. Used so the
     * picker can render the inline "Selected" panel even when the entry isn't
     * in the currently-filtered list (e.g. after deep-link pre-selection).
     */
    selectedSummaries?: Record<string, MemoryEntrySummary | undefined>;
    /** Additional copy — rendered below the "Selected" panel. Usually the token footprint. */
    footer?: React.ReactNode;
}

/** Short preview of a (possibly-empty) summary, capped at 100 chars per task spec. */
function previewSummary(summary?: string | null): string {
    if (!summary) return '';
    const trimmed = summary.trim();
    if (trimmed.length <= 100) return trimmed;
    return trimmed.slice(0, 99) + '\u2026';
}

function formatDate(iso: string): string {
    try {
        return new Date(iso).toLocaleDateString(undefined, {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
        });
    } catch {
        return iso;
    }
}

function OutcomeBadge({ outcome }: { outcome: MemoryEntrySummary['outcome'] }) {
    const succeeded = outcome === 'succeeded';
    return (
        <span
            className={`inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] uppercase tracking-widest font-mono border ${
                succeeded
                    ? 'border-emerald-500/40 text-emerald-400'
                    : 'border-destructive/40 text-destructive'
            }`}
        >
            {succeeded ? (
                <CheckCircle2 className="w-2.5 h-2.5" />
            ) : (
                <AlertCircle className="w-2.5 h-2.5" />
            )}
            {outcome}
        </span>
    );
}

/**
 * Multi-select picker of past memory entries for the current agent.
 *
 * - Lazy: the list API is only hit once the picker has been opened at least once.
 * - Search (non-empty) uses `/memory/search`; empty search falls back to `/memory`.
 * - Selection cap enforced at {@link MAX_ATTACHED_MEMORIES}; attempting to
 *   exceed it silently no-ops (the task spec calls this "defence in depth" —
 *   the API enforces the same cap at submission).
 *
 * This component is rendered by SubmitTaskPage only when the selected agent
 * has `memory.enabled = true`; see the visibility gate there.
 */
export function AttachMemoryPicker({
    agentId,
    value,
    onChange,
    selectedSummaries = {},
    footer,
}: AttachMemoryPickerProps) {
    const [open, setOpen] = useState(false);
    const [searchTerm, setSearchTerm] = useState('');
    const [focusedIndex, setFocusedIndex] = useState(0);
    const searchInputRef = useRef<HTMLInputElement | null>(null);

    // Lazy-load: only fetch list data once the picker has been opened.
    const listQuery = useAgentMemoryList(agentId, {
        enabled: open,
        limit: 50,
    });
    const searchQuery = useAgentMemorySearch(agentId, searchTerm, {
        enabled: open,
        limit: 20,
    });

    const activeItems: MemoryEntrySummary[] = useMemo(() => {
        if (searchTerm.trim().length > 0) {
            return searchQuery.data?.results ?? [];
        }
        return listQuery.data?.items ?? [];
    }, [searchTerm, searchQuery.data, listQuery.data]);

    // Reset focused index whenever the filtered list changes.
    useEffect(() => {
        setFocusedIndex(0);
    }, [searchTerm, activeItems.length]);

    // Auto-focus the search input on open so keyboard users don't have to click.
    useEffect(() => {
        if (open) {
            // Defer so the rendered input exists in the DOM.
            const handle = requestAnimationFrame(() => searchInputRef.current?.focus());
            return () => cancelAnimationFrame(handle);
        }
    }, [open]);

    const selectedSet = useMemo(() => new Set(value), [value]);

    function toggle(memoryId: string) {
        if (selectedSet.has(memoryId)) {
            onChange(value.filter((id) => id !== memoryId));
            return;
        }
        if (value.length >= MAX_ATTACHED_MEMORIES) {
            // Silent no-op — UI renders a warning line below the list.
            return;
        }
        onChange([...value, memoryId]);
    }

    function remove(memoryId: string) {
        onChange(value.filter((id) => id !== memoryId));
    }

    function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
        if (event.key === 'Escape') {
            if (searchTerm) {
                event.preventDefault();
                setSearchTerm('');
            }
            return;
        }
        if (event.key === 'ArrowDown') {
            event.preventDefault();
            setFocusedIndex((idx) =>
                activeItems.length === 0 ? 0 : Math.min(idx + 1, activeItems.length - 1)
            );
            return;
        }
        if (event.key === 'ArrowUp') {
            event.preventDefault();
            setFocusedIndex((idx) => Math.max(idx - 1, 0));
            return;
        }
        if (event.key === 'Enter') {
            event.preventDefault();
            const target = activeItems[focusedIndex];
            if (target) {
                toggle(target.memory_id);
            }
        }
    }

    const isSearching = searchTerm.trim().length > 0;
    const isLoading = isSearching ? searchQuery.isFetching : listQuery.isFetching;
    const errorMessage =
        (isSearching ? searchQuery.error : listQuery.error) instanceof Error
            ? (isSearching ? searchQuery.error : listQuery.error)?.message ?? 'Request failed'
            : null;

    const atCap = value.length >= MAX_ATTACHED_MEMORIES;

    return (
        <div className="space-y-3" data-testid="attach-memory-picker">
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <Brain className="w-4 h-4 text-primary" />
                    <span className="text-xs font-display uppercase tracking-widest">
                        Attach Past Memories
                    </span>
                    <span className="text-[10px] font-mono text-muted-foreground">
                        ({value.length}/{MAX_ATTACHED_MEMORIES})
                    </span>
                </div>
                <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => setOpen((prev) => !prev)}
                    className="text-xs uppercase tracking-widest h-7"
                    aria-expanded={open}
                    aria-controls="attach-memory-picker-panel"
                >
                    {open ? 'Close' : 'Browse'}
                    <ChevronDown
                        className={`ml-1 w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`}
                    />
                </Button>
            </div>

            {open && (
                <div
                    id="attach-memory-picker-panel"
                    className="rounded-lg border border-white/10 bg-black/30 p-3 space-y-3"
                >
                    <div className="relative">
                        <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground pointer-events-none" />
                        <input
                            ref={searchInputRef}
                            type="search"
                            aria-label="Search memory entries"
                            placeholder="Search by title, outcome, or tag\u2026"
                            value={searchTerm}
                            onChange={(e) => setSearchTerm(e.target.value)}
                            onKeyDown={handleKeyDown}
                            className="w-full h-9 pl-7 pr-2 text-sm bg-black/50 border border-border rounded-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary"
                        />
                    </div>

                    {errorMessage && (
                        <div className="flex items-start gap-2 text-xs text-destructive">
                            <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                            <span>{errorMessage}</span>
                        </div>
                    )}

                    <div
                        role="listbox"
                        aria-label="Memory entries"
                        className="max-h-[280px] overflow-y-auto divide-y divide-white/5 border border-white/5"
                        data-testid="attach-memory-picker-list"
                    >
                        {isLoading && activeItems.length === 0 ? (
                            <div className="p-4 text-xs text-muted-foreground animate-pulse">
                                Loading memory entries\u2026
                            </div>
                        ) : activeItems.length === 0 ? (
                            <div className="p-4 text-xs text-muted-foreground">
                                {isSearching
                                    ? 'No matches for this search.'
                                    : 'No memory entries yet.'}
                            </div>
                        ) : (
                            activeItems.map((entry, index) => {
                                const isSelected = selectedSet.has(entry.memory_id);
                                const isFocused = index === focusedIndex;
                                return (
                                    <button
                                        key={entry.memory_id}
                                        type="button"
                                        role="option"
                                        aria-selected={isSelected}
                                        data-memory-id={entry.memory_id}
                                        data-focused={isFocused ? 'true' : undefined}
                                        onClick={() => toggle(entry.memory_id)}
                                        onMouseEnter={() => setFocusedIndex(index)}
                                        className={`w-full text-left px-3 py-2 transition-colors flex items-start gap-3 ${
                                            isFocused ? 'bg-primary/10' : ''
                                        } ${isSelected ? 'border-l-2 border-primary' : ''} hover:bg-primary/5`}
                                    >
                                        <div
                                            className={`mt-0.5 w-3.5 h-3.5 shrink-0 border flex items-center justify-center ${
                                                isSelected
                                                    ? 'bg-primary border-primary'
                                                    : 'border-border'
                                            }`}
                                            aria-hidden="true"
                                        >
                                            {isSelected ? (
                                                <CheckCircle2 className="w-3 h-3 text-black" />
                                            ) : null}
                                        </div>
                                        <div className="flex-1 min-w-0 space-y-1">
                                            <div className="flex items-center gap-2 flex-wrap">
                                                <span className="text-sm font-mono truncate">
                                                    {entry.title}
                                                </span>
                                                <OutcomeBadge outcome={entry.outcome} />
                                                <span className="text-[10px] font-mono text-muted-foreground">
                                                    {formatDate(entry.created_at)}
                                                </span>
                                            </div>
                                            {entry.summary_preview && (
                                                <p className="text-xs text-muted-foreground line-clamp-2">
                                                    {previewSummary(entry.summary_preview)}
                                                </p>
                                            )}
                                        </div>
                                    </button>
                                );
                            })
                        )}
                    </div>

                    {atCap && (
                        <p className="text-[11px] text-amber-400 font-mono">
                            Selection capped at {MAX_ATTACHED_MEMORIES}. Remove an entry to attach another.
                        </p>
                    )}
                </div>
            )}

            {value.length > 0 && (
                <div
                    className="rounded-lg border border-white/5 bg-muted/5 p-3 space-y-2"
                    data-testid="attach-memory-selected-panel"
                >
                    <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                        Selected ({value.length})
                    </div>
                    <ul className="space-y-1.5">
                        {value.map((memoryId, index) => {
                            const entry = selectedSummaries[memoryId];
                            return (
                                <li
                                    key={memoryId}
                                    className="flex items-center gap-2 px-2 py-1.5 bg-black/30 border border-white/5"
                                    data-memory-id={memoryId}
                                    data-position={index}
                                >
                                    <span className="text-[10px] font-mono text-muted-foreground w-5 shrink-0">
                                        {String(index + 1).padStart(2, '0')}
                                    </span>
                                    <div className="flex-1 min-w-0">
                                        <div className="text-sm font-mono truncate">
                                            {entry?.title ?? memoryId}
                                        </div>
                                        {entry && (
                                            <div className="flex items-center gap-2 mt-0.5">
                                                <OutcomeBadge outcome={entry.outcome} />
                                                <span className="text-[10px] font-mono text-muted-foreground">
                                                    {formatDate(entry.created_at)}
                                                </span>
                                            </div>
                                        )}
                                    </div>
                                    <Button
                                        type="button"
                                        variant="ghost"
                                        size="sm"
                                        aria-label={`Remove ${entry?.title ?? memoryId}`}
                                        onClick={() => remove(memoryId)}
                                        className="h-6 w-6 p-0 hover:bg-destructive/20 hover:text-destructive"
                                    >
                                        <X className="w-3 h-3" />
                                    </Button>
                                </li>
                            );
                        })}
                    </ul>
                    {footer}
                </div>
            )}
        </div>
    );
}
