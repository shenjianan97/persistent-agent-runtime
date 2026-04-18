import { useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
import {
    Brain,
    Search as SearchIcon,
    Trash2,
    AlertTriangle,
    Database,
    Link as LinkIcon,
    Ghost,
    Info,
} from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';

import { useAgent } from '../useAgents';
import {
    useAgentMemoryList,
    useAgentMemorySearch,
    useDeleteAgentMemoryEntry,
} from './hooks';
import type { MemoryOutcomeFilter } from './api';
import { DeleteEntryDialog } from './DeleteEntryDialog';
import { MemoryEntryDetail } from './MemoryEntryDetail';
import type { MemoryEntrySummary, MemoryStorageStats } from '@/types';

/** Platform default soft cap — matches design-doc "Core Decisions". */
const DEFAULT_MAX_ENTRIES = 10_000;
/** Platform hard max — design-doc "Core Decisions" / validation rules. */
const PLATFORM_MAX_ENTRIES = 100_000;
const SEARCH_LIMIT = 20;
const WARNING_THRESHOLD = 0.8;
const TEMPLATE_MODEL_IDS = new Set(['template:fallback', 'template:dead_letter']);

function formatBytes(n: number): string {
    if (!Number.isFinite(n) || n <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let idx = 0;
    let value = n;
    while (value >= 1024 && idx < units.length - 1) {
        value /= 1024;
        idx += 1;
    }
    const decimals = value < 10 && idx > 0 ? 1 : 0;
    return `${value.toFixed(decimals)} ${units[idx]}`;
}

function formatCount(n: number): string {
    return n.toLocaleString();
}

interface OutcomeBadgeProps {
    outcome: MemoryEntrySummary['outcome'];
}

function OutcomeBadge({ outcome }: OutcomeBadgeProps) {
    const className =
        outcome === 'succeeded'
            ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30'
            : 'bg-red-500/20 text-red-400 border-red-500/30';
    return (
        <Badge
            variant="outline"
            className={`${className} text-[10px] px-2 py-0.5 uppercase tracking-widest`}
        >
            {outcome}
        </Badge>
    );
}

interface StatsStripProps {
    stats: MemoryStorageStats | undefined;
    maxEntries: number;
    onDeleteOld: () => void;
}

function StorageStatsStrip({ stats, maxEntries, onDeleteOld }: StatsStripProps) {
    if (!stats) return null;
    const count = stats.entry_count;
    const ratio = maxEntries > 0 ? count / maxEntries : 0;
    const atCap = count >= maxEntries || count >= PLATFORM_MAX_ENTRIES;
    const warn = !atCap && ratio >= WARNING_THRESHOLD;

    const containerClass = atCap
        ? 'border-red-500/40 bg-red-500/10'
        : warn
            ? 'border-amber-500/40 bg-amber-500/10'
            : 'border-white/10 bg-white/2';

    const Icon = atCap ? AlertTriangle : warn ? AlertTriangle : Database;
    const iconColor = atCap ? 'text-red-400' : warn ? 'text-amber-400' : 'text-primary';

    return (
        <div
            className={`rounded-[18px] border ${containerClass} p-4 flex flex-col md:flex-row md:items-center gap-3 justify-between`}
            data-testid="memory-storage-stats"
        >
            <div className="flex items-center gap-3 text-sm">
                <Icon className={`w-5 h-5 shrink-0 ${iconColor}`} />
                <div>
                    <div className="font-mono text-foreground">
                        <span data-testid="memory-stats-entry-count">{formatCount(count)}</span> of{' '}
                        <span data-testid="memory-stats-max-entries">{formatCount(maxEntries)}</span> entries
                        <span className="text-muted-foreground"> · ~{formatBytes(stats.approx_bytes)}</span>
                    </div>
                    {atCap && (
                        <div className="text-xs text-red-300 mt-1">
                            Maximum reached. FIFO trim is removing the oldest entries automatically.
                        </div>
                    )}
                    {warn && !atCap && (
                        <div className="text-xs text-amber-300 mt-1" data-testid="memory-warning-banner">
                            Nearing the per-agent cap. Delete entries you no longer need, or raise the cap
                            on this agent&apos;s configuration.
                        </div>
                    )}
                </div>
            </div>
            {(warn || atCap) && (
                <Button
                    type="button"
                    onClick={onDeleteOld}
                    variant="outline"
                    className="font-bold uppercase tracking-widest text-xs px-4 border-primary/60 text-primary hover:bg-primary hover:text-black shrink-0"
                    data-testid="memory-delete-old-button"
                >
                    <Trash2 className="w-3 h-3 mr-2" />
                    Delete old entries
                </Button>
            )}
        </div>
    );
}

interface EntriesTableProps {
    items: MemoryEntrySummary[];
    showScore: boolean;
    isLoading: boolean;
    emptyState: 'no-entries' | 'no-results';
    onRowClick: (memoryId: string) => void;
    onDeleteClick: (entry: MemoryEntrySummary) => void;
}

function EntriesTable({ items, showScore, isLoading, emptyState, onRowClick, onDeleteClick }: EntriesTableProps) {
    return (
        <div className="console-surface rounded-[28px] overflow-hidden">
            <Table>
                <TableHeader className="sticky top-0 bg-[#0f1727]/90 backdrop-blur-xl">
                    <TableRow className="border-white/8 hover:bg-transparent">
                        <TableHead className="font-display uppercase tracking-widest text-xs h-10">Title</TableHead>
                        <TableHead className="font-display uppercase tracking-widest text-xs h-10">Outcome</TableHead>
                        <TableHead className="font-display uppercase tracking-widest text-xs h-10">Task</TableHead>
                        <TableHead className="font-display uppercase tracking-widest text-xs h-10">Created</TableHead>
                        {showScore && (
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10 text-right">Score</TableHead>
                        )}
                        <TableHead className="font-display uppercase tracking-widest text-xs h-10 text-right">Actions</TableHead>
                    </TableRow>
                </TableHeader>
                <TableBody>
                    {isLoading && (
                        <TableRow>
                            <TableCell colSpan={showScore ? 6 : 5} className="h-24 text-center">
                                <span className="uppercase tracking-widest text-xs font-bold text-muted-foreground animate-pulse">
                                    Loading memory entries...
                                </span>
                            </TableCell>
                        </TableRow>
                    )}

                    {!isLoading && items.length === 0 && (
                        <TableRow>
                            <TableCell
                                colSpan={showScore ? 6 : 5}
                                className="h-48 text-center text-muted-foreground hover:bg-transparent"
                            >
                                <div className="flex flex-col items-center justify-center gap-2">
                                    <Ghost className="w-8 h-8 opacity-20 mb-2" />
                                    {emptyState === 'no-entries' ? (
                                        <>
                                            <span className="uppercase tracking-widest text-xs" data-testid="memory-empty-state">
                                                No memory entries yet
                                            </span>
                                            <span className="text-xs">
                                                Completed tasks on this memory-enabled agent will land here.
                                            </span>
                                        </>
                                    ) : (
                                        <>
                                            <span className="uppercase tracking-widest text-xs" data-testid="memory-no-results">
                                                No results match your filters
                                            </span>
                                            <span className="text-xs">Try widening the date range or clearing the search.</span>
                                        </>
                                    )}
                                </div>
                            </TableCell>
                        </TableRow>
                    )}

                    {!isLoading &&
                        items.map((entry) => {
                            const isTemplate =
                                entry.summary_preview !== undefined &&
                                entry.summary_preview !== null &&
                                false; // preview-based detection not available here; template flag rendered in detail view only
                            void isTemplate;
                            return (
                                <TableRow
                                    key={entry.memory_id}
                                    className="border-border/40 font-mono text-xs hover:bg-white/5 transition-colors cursor-pointer"
                                    onClick={() => onRowClick(entry.memory_id)}
                                    data-testid="memory-entry-row"
                                >
                                    <TableCell className="font-medium text-foreground max-w-[400px]">
                                        <div className="truncate" title={entry.title}>{entry.title}</div>
                                        {entry.summary_preview && (
                                            <div className="text-[11px] text-muted-foreground truncate mt-0.5" title={entry.summary_preview}>
                                                {entry.summary_preview}
                                            </div>
                                        )}
                                    </TableCell>
                                    <TableCell>
                                        <OutcomeBadge outcome={entry.outcome} />
                                    </TableCell>
                                    <TableCell>
                                        <Link
                                            to={`/tasks/${encodeURIComponent(entry.task_id)}`}
                                            onClick={(e) => e.stopPropagation()}
                                            className="text-primary hover:underline inline-flex items-center gap-1"
                                        >
                                            <LinkIcon className="w-3 h-3" />
                                            <span className="truncate max-w-[160px]">{entry.task_id.slice(0, 8)}…</span>
                                        </Link>
                                    </TableCell>
                                    <TableCell className="text-muted-foreground">
                                        {new Date(entry.created_at).toLocaleString()}
                                    </TableCell>
                                    {showScore && (
                                        <TableCell className="text-right text-muted-foreground">
                                            {typeof entry.score === 'number' ? entry.score.toFixed(3) : '—'}
                                        </TableCell>
                                    )}
                                    <TableCell className="text-right">
                                        <button
                                            type="button"
                                            onClick={(e) => {
                                                e.stopPropagation();
                                                onDeleteClick(entry);
                                            }}
                                            className="text-destructive/80 hover:text-destructive transition-colors inline-flex items-center gap-1 text-xs uppercase tracking-widest"
                                            aria-label={`Delete ${entry.title}`}
                                            data-testid="memory-row-delete-button"
                                        >
                                            <Trash2 className="w-3 h-3" />
                                            Delete
                                        </button>
                                    </TableCell>
                                </TableRow>
                            );
                        })}
                </TableBody>
            </Table>
        </div>
    );
}

export function MemoryTab() {
    const { agentId, memoryId } = useParams<{ agentId: string; memoryId?: string }>();
    const navigate = useNavigate();
    const { data: agent } = useAgent(agentId ?? '');

    // Filters live in component state (no URL sync for v1 — the design doc
    // lists URL-synced filters as a future enhancement).
    const [outcome, setOutcome] = useState<MemoryOutcomeFilter>('all');
    const [fromDate, setFromDate] = useState('');
    const [toDate, setToDate] = useState('');
    const [searchInput, setSearchInput] = useState('');
    const [submittedQuery, setSubmittedQuery] = useState('');

    // ISO conversion for API. Empty string → undefined.
    const fromIso = fromDate ? new Date(fromDate + 'T00:00:00').toISOString() : undefined;
    const toIso = toDate ? new Date(toDate + 'T23:59:59').toISOString() : undefined;

    const inSearchMode = submittedQuery.trim().length > 0;

    const listQuery = useAgentMemoryList(
        agentId ?? '',
        {
            outcome: outcome === 'all' ? undefined : outcome,
            from: fromIso,
            to: toIso,
            limit: 50,
        },
        { enabled: !inSearchMode }
    );

    const searchQuery = useAgentMemorySearch(
        agentId ?? '',
        submittedQuery,
        {
            mode: 'hybrid',
            limit: SEARCH_LIMIT,
            outcome: outcome === 'all' ? undefined : outcome,
            from: fromIso,
            to: toIso,
        },
        { enabled: inSearchMode }
    );

    const deleteMutation = useDeleteAgentMemoryEntry(agentId ?? '');
    const [deleteTarget, setDeleteTarget] = useState<MemoryEntrySummary | null>(null);

    const items: MemoryEntrySummary[] = inSearchMode
        ? searchQuery.data?.results ?? []
        : listQuery.data?.items ?? [];
    const stats = listQuery.data?.agent_storage_stats;
    const isLoading = inSearchMode ? searchQuery.isLoading : listQuery.isLoading;
    const error = inSearchMode ? searchQuery.error : listQuery.error;
    const rankingUsed = searchQuery.data?.ranking_used;

    const memoryEnabled = agent?.agent_config?.memory?.enabled === true;
    const maxEntries = useMemo(() => {
        const configured = agent?.agent_config?.memory?.max_entries;
        if (typeof configured === 'number' && configured > 0) return configured;
        return DEFAULT_MAX_ENTRIES;
    }, [agent]);

    const filtersActive = outcome !== 'all' || !!fromDate || !!toDate || inSearchMode;
    const emptyState: 'no-entries' | 'no-results' = filtersActive ? 'no-results' : 'no-entries';

    function handleRowClick(mid: string) {
        navigate(`/agents/${encodeURIComponent(agentId ?? '')}/memory/${encodeURIComponent(mid)}`);
    }

    function handleDeleteConfirm() {
        if (!deleteTarget) return;
        deleteMutation.mutate(deleteTarget.memory_id, {
            onSuccess: () => {
                toast.success('Memory entry deleted');
                setDeleteTarget(null);
            },
            onError: (err: Error) => {
                toast.error('Failed to delete memory entry', {
                    description: err.message || 'Unknown error occurred.',
                });
                setDeleteTarget(null);
            },
        });
    }

    function handleSearchSubmit(e: React.FormEvent) {
        e.preventDefault();
        setSubmittedQuery(searchInput.trim());
    }

    function handleClearSearch() {
        setSearchInput('');
        setSubmittedQuery('');
    }

    function handleDeleteOld() {
        // v1 shortcut: narrow to oldest by clearing outcome and date range so
        // the list order (DESC created_at) still helps customers triage from
        // the end. A dedicated "sort ascending" option is deferred — see
        // plan A7 "80% banner → delete old entries" note.
        setOutcome('all');
        setFromDate('');
        setToDate('');
        setSearchInput('');
        setSubmittedQuery('');
        toast.info('Filters cleared. Scroll to the oldest entries to trim.');
    }

    if (!agentId) {
        return null;
    }

    // Nested-route detail view. Rendered in place of the list + filters.
    if (memoryId) {
        return (
            <MemoryEntryDetail
                agentId={agentId}
                memoryId={memoryId}
                onBack={() => navigate(`/agents/${encodeURIComponent(agentId)}/memory`)}
                onDeleted={() => navigate(`/agents/${encodeURIComponent(agentId)}/memory`)}
            />
        );
    }

    return (
        <div className="space-y-6 animate-in fade-in duration-500" data-testid="memory-tab-root">
            <div className="console-surface-strong rounded-[28px] p-6 md:p-7 flex items-center gap-3">
                <Brain className="w-5 h-5 text-primary drop-shadow-[0_0_12px_var(--color-primary)]" />
                <div>
                    <h3 className="text-lg font-display font-semibold tracking-tight">Memory</h3>
                    <p className="text-xs text-muted-foreground">
                        Browse, search, and delete the distilled memory entries this agent has accumulated.
                    </p>
                </div>
            </div>

            {!memoryEnabled && (
                <div
                    className="rounded-[18px] border border-amber-500/30 bg-amber-500/5 p-4 flex items-start gap-3"
                    data-testid="memory-disabled-notice"
                >
                    <Info className="w-4 h-4 text-amber-400 mt-0.5 shrink-0" />
                    <div className="text-sm">
                        <div className="font-semibold text-amber-300 mb-0.5">Memory is disabled for this agent.</div>
                        <p className="text-xs text-muted-foreground">
                            Existing entries are preserved and can still be browsed or deleted here. No new entries
                            will be written until memory is re-enabled in the agent&apos;s configuration.
                        </p>
                    </div>
                </div>
            )}

            <StorageStatsStrip stats={stats} maxEntries={maxEntries} onDeleteOld={handleDeleteOld} />

            <div className="console-surface rounded-[28px] p-4 md:p-5 space-y-4">
                <form
                    onSubmit={handleSearchSubmit}
                    className="flex flex-col md:flex-row md:items-end gap-3"
                >
                    <div className="flex-1 min-w-0">
                        <label className="text-xs uppercase tracking-widest text-muted-foreground mb-2 block">
                            Search
                        </label>
                        <div className="relative">
                            <SearchIcon className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
                            <Input
                                value={searchInput}
                                onChange={(e) => setSearchInput(e.target.value)}
                                placeholder="Search titles, summaries, observations…"
                                className="pl-9 rounded-xl border-white/10 bg-white/5 font-mono"
                                data-testid="memory-search-input"
                            />
                        </div>
                    </div>
                    <div>
                        <label className="text-xs uppercase tracking-widest text-muted-foreground mb-2 block">
                            Outcome
                        </label>
                        <select
                            value={outcome}
                            onChange={(e) => setOutcome(e.target.value as MemoryOutcomeFilter)}
                            className="flex h-10 w-40 rounded-xl border border-white/10 bg-white/5 px-3 py-1 text-sm font-mono appearance-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                            data-testid="memory-outcome-select"
                        >
                            <option value="all">All</option>
                            <option value="succeeded">Succeeded</option>
                            <option value="failed">Failed</option>
                        </select>
                    </div>
                    <div>
                        <label className="text-xs uppercase tracking-widest text-muted-foreground mb-2 block">
                            From
                        </label>
                        <Input
                            type="date"
                            value={fromDate}
                            onChange={(e) => setFromDate(e.target.value)}
                            className="w-44 rounded-xl border-white/10 bg-white/5 font-mono"
                            data-testid="memory-from-date"
                        />
                    </div>
                    <div>
                        <label className="text-xs uppercase tracking-widest text-muted-foreground mb-2 block">
                            To
                        </label>
                        <Input
                            type="date"
                            value={toDate}
                            onChange={(e) => setToDate(e.target.value)}
                            className="w-44 rounded-xl border-white/10 bg-white/5 font-mono"
                            data-testid="memory-to-date"
                        />
                    </div>
                    <div className="flex gap-2">
                        <Button
                            type="submit"
                            className="font-bold uppercase tracking-widest text-xs px-5"
                            data-testid="memory-search-submit"
                        >
                            Search
                        </Button>
                        {(inSearchMode || !!fromDate || !!toDate || outcome !== 'all') && (
                            <Button
                                type="button"
                                variant="ghost"
                                onClick={handleClearSearch}
                                className="uppercase tracking-widest text-xs"
                                data-testid="memory-clear-filters"
                            >
                                Clear
                            </Button>
                        )}
                    </div>
                </form>

                {inSearchMode && (
                    <div className="text-xs text-muted-foreground font-mono flex items-center gap-2" data-testid="memory-search-label">
                        <span>Top {SEARCH_LIMIT} matches</span>
                        {rankingUsed && (
                            <Badge
                                variant="outline"
                                className="border-white/10 text-[10px] px-2 py-0.5 uppercase tracking-widest font-mono"
                            >
                                ranking: {rankingUsed}
                            </Badge>
                        )}
                    </div>
                )}

                {error && (
                    <div className="text-xs text-destructive font-mono" data-testid="memory-load-error">
                        Failed to load memory entries: {(error as Error).message}
                    </div>
                )}
            </div>

            <EntriesTable
                items={items}
                showScore={inSearchMode}
                isLoading={isLoading}
                emptyState={emptyState}
                onRowClick={handleRowClick}
                onDeleteClick={(entry) => setDeleteTarget(entry)}
            />

            <DeleteEntryDialog
                open={!!deleteTarget}
                onOpenChange={(open) => { if (!open) setDeleteTarget(null); }}
                entryTitle={deleteTarget?.title ?? ''}
                isPending={deleteMutation.isPending}
                onConfirm={handleDeleteConfirm}
            />
        </div>
    );
}

// Re-export the template-model sentinel set so tests can assert against it
// without duplicating the constant.
export const _MEMORY_TAB_INTERNALS = {
    TEMPLATE_MODEL_IDS,
    DEFAULT_MAX_ENTRIES,
    PLATFORM_MAX_ENTRIES,
    WARNING_THRESHOLD,
};
