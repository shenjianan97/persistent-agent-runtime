import { useDeadLetters, useRedriveTask } from './useDeadLetter';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Button } from '@/components/ui/button';
import { AlertCircle, RotateCcw, Ghost } from 'lucide-react';
import { toast } from 'sonner';
import { Link, useNavigate } from 'react-router';
import { useState, useRef, useCallback } from 'react';
import { Input } from '@/components/ui/input';

export function DeadLetterPage() {
    const navigate = useNavigate();
    const [agentId, setAgentId] = useState('');
    const [debouncedAgentId, setDebouncedAgentId] = useState('');
    const { data, isLoading } = useDeadLetters(debouncedAgentId || undefined);
    const redriveMutation = useRedriveTask();
    const debounceTimer = useRef<ReturnType<typeof setTimeout>>(null);

    const handleRedrive = (taskId: string) => {
        redriveMutation.mutate(taskId, {
            onSuccess: (response) => {
                toast.success(`Task redrive initiated`, { description: response.task_id });
                navigate(`/tasks/${response.task_id}`);
            },
            onError: (err: Error) => {
                toast.error('Redrive failed', { description: err.message });
            }
        });
    };

    const handleSearchChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
        const value = e.target.value;
        setAgentId(value);
        if (debounceTimer.current) clearTimeout(debounceTimer.current);
        debounceTimer.current = setTimeout(() => setDebouncedAgentId(value), 500);
    }, []);

    return (
        <div className="space-y-6 animate-in fade-in duration-500">
            <div className="console-surface-strong rounded-[28px] p-6 md:p-7 flex flex-col md:flex-row md:items-end justify-between gap-4">
                <div>
                    <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.24em] text-destructive">Task Recovery</div>
                    <h2 className="text-3xl font-display font-semibold tracking-tight mb-2 flex items-center gap-2 text-destructive">
                        <AlertCircle className="w-6 h-6 drop-shadow-[0_0_12px_var(--color-destructive)]" />
                        Failed
                    </h2>
                    <p className="text-muted-foreground">
                        Tasks that exhausted retries or ended in a terminal failure.
                    </p>
                </div>

                <div className="w-full md:w-64">
                    <label className="text-xs uppercase tracking-widest text-muted-foreground mb-2 block">
                        Filter by Agent ID
                    </label>
                    <Input
                        className="rounded-none border-border bg-black/50 font-mono text-sm h-9"
                        placeholder="e.g. e2e-test"
                        value={agentId}
                        onChange={handleSearchChange}
                    />
                </div>
            </div>

            <div className="console-surface rounded-[28px] overflow-hidden">
                <Table>
                    <TableHeader className="sticky top-0 bg-[#0f1727]/90 backdrop-blur-xl">
                        <TableRow className="border-white/8 hover:bg-transparent">
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Task ID</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Agent</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Error / Reason</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10 text-right">Retries</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10 text-right">Timestamp</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10 text-right">Action</TableHead>
                        </TableRow>
                    </TableHeader>
                    <TableBody>
                        {isLoading && (
                            <TableRow>
                                <TableCell colSpan={6} className="h-24 text-center">
                                    <span className="uppercase tracking-widest text-xs font-bold text-muted-foreground animate-pulse">Loading queue...</span>
                                </TableCell>
                            </TableRow>
                        )}

                        {!isLoading && (!data?.items || data.items.length === 0) && (
                            <TableRow>
                                <TableCell colSpan={6} className="h-48 text-center text-muted-foreground hover:bg-transparent">
                                    <div className="flex flex-col items-center justify-center gap-2">
                                        <Ghost className="w-8 h-8 opacity-20 mb-2" />
                                        <span className="uppercase tracking-widest text-xs">No failed tasks</span>
                                        <span className="text-[10px] opacity-50">All tasks executing nominally</span>
                                    </div>
                                </TableCell>
                            </TableRow>
                        )}

                        {data?.items.map((task) => (
                            <TableRow key={task.task_id} className="border-border/40 font-mono text-xs hover:bg-white/5 transition-colors group">
                                <TableCell className="font-medium text-foreground">
                                    <Link to={`/tasks/${task.task_id}`} className="hover:text-primary hover:underline underline-offset-4 decoration-primary/50 transition-colors">
                                        {task.task_id.split('-')[0]}...{task.task_id.split('-').pop()}
                                    </Link>
                                </TableCell>
                                <TableCell>
                                    <Link to={`/agents/${encodeURIComponent(task.agent_id)}`} className="hover:text-primary hover:underline underline-offset-4 decoration-primary/50 transition-colors">
                                        {task.agent_display_name && (
                                            <span className="block text-foreground">{task.agent_display_name}</span>
                                        )}
                                        <span className="block text-muted-foreground text-[10px]">{task.agent_id}</span>
                                    </Link>
                                </TableCell>
                                <TableCell className="max-w-[300px]">
                                    <div className="truncate font-bold text-destructive mb-1">{task.dead_letter_reason}</div>
                                    <div className="truncate text-[10px] text-muted-foreground">{task.last_error_message || 'No extended error detail provided.'}</div>
                                </TableCell>
                                <TableCell className="text-right">
                                    <div className="px-2 py-0.5 bg-white/5 border border-border/40 inline-block">
                                        {task.retry_count}
                                    </div>
                                </TableCell>
                                <TableCell className="text-right text-muted-foreground">
                                    {new Date(task.dead_lettered_at).toLocaleDateString()}
                                    <br />
                                    <span className="text-[10px]">{new Date(task.dead_lettered_at).toLocaleTimeString()}</span>
                                </TableCell>
                                <TableCell className="text-right">
                                    <Button
                                        size="sm"
                                        variant="outline"
                                        className="rounded-none border-primary/50 text-primary hover:bg-primary hover:text-black uppercase tracking-widest text-[10px] h-7 opacity-0 group-hover:opacity-100 transition-opacity focus:opacity-100"
                                        onClick={() => handleRedrive(task.task_id)}
                                        disabled={redriveMutation.isPending}
                                    >
                                        <RotateCcw className="w-3 h-3 mr-1" /> Redrive
                                    </Button>
                                </TableCell>
                            </TableRow>
                        ))}
                    </TableBody>
                </Table>
            </div>
        </div>
    );
}
