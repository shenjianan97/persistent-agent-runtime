import { useState } from 'react';
import { Link } from 'react-router';
import { toast } from 'sonner';
import { ArrowLeft, Trash2, Link as LinkIcon, Paperclip, Info } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useAgentMemoryDetail, useDeleteAgentMemoryEntry } from './hooks';
import { DeleteEntryDialog } from './DeleteEntryDialog';

interface MemoryEntryDetailProps {
    agentId: string;
    memoryId: string;
    /** Navigates the parent back to the list view. */
    onBack: () => void;
    /** Called after a successful delete; parent closes the detail view. */
    onDeleted: () => void;
}

const TEMPLATE_MODEL_IDS = new Set(['template:fallback', 'template:dead_letter']);

/**
 * Full memory entry detail view.
 *
 * Renders every field specified by the design doc's "Memory entry detail view":
 * title, outcome badge, created/updated timestamps, summary, observations,
 * tags, linked task (deep-link), summarizer model id, Delete, and an
 * "Attach to new task" shortcut that deep-links into the Submit page (Task 10
 * picks up the `?attachMemoryId` param).
 */
export function MemoryEntryDetail({
    agentId,
    memoryId,
    onBack,
    onDeleted,
}: MemoryEntryDetailProps) {
    const { data: entry, isLoading, error } = useAgentMemoryDetail(agentId, memoryId);
    const deleteMutation = useDeleteAgentMemoryEntry(agentId);
    const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);

    function handleDelete() {
        deleteMutation.mutate(memoryId, {
            onSuccess: () => {
                toast.success('Memory entry deleted');
                setDeleteDialogOpen(false);
                onDeleted();
            },
            onError: (err: Error) => {
                toast.error('Failed to delete memory entry', {
                    description: err.message || 'Unknown error occurred.',
                });
                setDeleteDialogOpen(false);
            },
        });
    }

    if (isLoading) {
        return (
            <div className="console-surface rounded-[28px] p-6">
                <span className="uppercase tracking-widest text-xs font-bold text-muted-foreground animate-pulse">
                    Loading memory entry...
                </span>
            </div>
        );
    }

    if (error || !entry) {
        return (
            <div className="console-surface rounded-[28px] p-6 space-y-3">
                <button
                    onClick={onBack}
                    className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 uppercase tracking-widest transition-colors"
                >
                    <ArrowLeft className="w-3 h-3" />
                    Back to memory list
                </button>
                <h3 className="text-lg font-display font-semibold text-destructive">Memory entry not found</h3>
                <p className="text-xs text-muted-foreground font-mono">{memoryId}</p>
            </div>
        );
    }

    const isTemplateSummary = !!entry.summarizer_model_id && TEMPLATE_MODEL_IDS.has(entry.summarizer_model_id);
    const outcomeClass = entry.outcome === 'succeeded'
        ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30'
        : 'bg-red-500/20 text-red-400 border-red-500/30';

    return (
        <div className="space-y-6 animate-in fade-in duration-300">
            <div className="console-surface rounded-[28px] p-6 space-y-4">
                <button
                    onClick={onBack}
                    className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 uppercase tracking-widest transition-colors"
                >
                    <ArrowLeft className="w-3 h-3" />
                    Back to memory list
                </button>

                <div className="flex flex-col md:flex-row md:items-start md:justify-between gap-4">
                    <div className="min-w-0 flex-1">
                        <h3 className="text-2xl font-display font-semibold tracking-tight mb-2">
                            {entry.title}
                        </h3>
                        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground font-mono">
                            <Badge
                                variant="outline"
                                className={`${outcomeClass} text-[10px] px-2 py-0.5 uppercase tracking-widest`}
                                data-testid="memory-detail-outcome-badge"
                            >
                                {entry.outcome}
                            </Badge>
                            <span>v{entry.version}</span>
                            <span>·</span>
                            <span>Created {new Date(entry.created_at).toLocaleString()}</span>
                            <span>·</span>
                            <span>Updated {new Date(entry.updated_at).toLocaleString()}</span>
                        </div>
                    </div>

                    <div className="flex flex-wrap gap-2 shrink-0">
                        <Link
                            to={`/tasks/new?agent_id=${encodeURIComponent(agentId)}&attachMemoryId=${encodeURIComponent(entry.memory_id)}`}
                        >
                            <Button
                                type="button"
                                variant="outline"
                                className="font-bold uppercase tracking-widest text-xs px-4 border-primary/50 text-primary hover:bg-primary hover:text-black transition-all"
                            >
                                <Paperclip className="w-3 h-3 mr-2" />
                                Attach to new task
                            </Button>
                        </Link>
                        <Button
                            type="button"
                            onClick={() => setDeleteDialogOpen(true)}
                            variant="outline"
                            className="font-bold uppercase tracking-widest text-xs px-4 border-destructive/50 text-destructive hover:bg-destructive hover:text-destructive-foreground transition-all"
                            data-testid="memory-detail-delete-button"
                        >
                            <Trash2 className="w-3 h-3 mr-2" />
                            Delete
                        </Button>
                    </div>
                </div>
            </div>

            <Card className="console-surface border-white/10">
                <CardHeader className="border-b border-white/8 pb-4">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-primary flex items-center gap-2">
                        Summary
                        {isTemplateSummary && (
                            <span
                                title="Generated from a template because the summarizer was unavailable."
                                className="text-amber-400 inline-flex items-center gap-1 text-[10px] normal-case tracking-normal font-mono"
                            >
                                <Info className="w-3 h-3" />
                                template fallback
                            </span>
                        )}
                    </CardTitle>
                </CardHeader>
                <CardContent className="pt-6">
                    <p className="text-sm text-foreground/90 whitespace-pre-wrap leading-relaxed">
                        {entry.summary || <span className="text-muted-foreground italic">No summary</span>}
                    </p>
                </CardContent>
            </Card>

            <Card className="console-surface border-white/10">
                <CardHeader className="border-b border-white/8 pb-4">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">
                        Observations
                    </CardTitle>
                </CardHeader>
                <CardContent className="pt-6">
                    {entry.observations.length === 0 ? (
                        <p className="text-xs text-muted-foreground italic">
                            The agent did not record any observations during this task.
                        </p>
                    ) : (
                        <ol className="space-y-2 list-decimal list-inside">
                            {entry.observations.map((obs: string, idx: number) => (
                                <li key={idx} className="text-sm text-foreground/90 font-mono whitespace-pre-wrap">
                                    {obs}
                                </li>
                            ))}
                        </ol>
                    )}
                </CardContent>
            </Card>

            <Card className="console-surface border-white/10">
                <CardHeader className="border-b border-white/8 pb-4">
                    <CardTitle className="text-sm font-display uppercase tracking-widest">Provenance</CardTitle>
                </CardHeader>
                <CardContent className="pt-6 grid grid-cols-1 md:grid-cols-2 gap-5 text-sm">
                    <div>
                        <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">
                            Task
                        </span>
                        <Link
                            to={`/tasks/${encodeURIComponent(entry.task_id)}`}
                            className="font-mono text-primary hover:underline inline-flex items-center gap-1"
                        >
                            <LinkIcon className="w-3 h-3" />
                            {entry.task_id}
                        </Link>
                    </div>
                    <div>
                        <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">
                            Summarizer Model
                        </span>
                        <span className="font-mono text-foreground/90">
                            {entry.summarizer_model_id || <span className="text-muted-foreground">(none)</span>}
                        </span>
                    </div>
                    {entry.tags.length > 0 && (
                        <div className="md:col-span-2">
                            <span className="text-muted-foreground block mb-2 uppercase tracking-widest text-[10px]">
                                Tags
                            </span>
                            <div className="flex flex-wrap gap-2">
                                {entry.tags.map((tag: string) => (
                                    <Badge
                                        key={tag}
                                        variant="outline"
                                        className="border-primary/30 text-primary text-[10px] px-2 py-0.5 font-mono"
                                    >
                                        {tag}
                                    </Badge>
                                ))}
                            </div>
                        </div>
                    )}
                </CardContent>
            </Card>

            <DeleteEntryDialog
                open={deleteDialogOpen}
                onOpenChange={setDeleteDialogOpen}
                entryTitle={entry.title}
                isPending={deleteMutation.isPending}
                onConfirm={handleDelete}
            />
        </div>
    );
}
