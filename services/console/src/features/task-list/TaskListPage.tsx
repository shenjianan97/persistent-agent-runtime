import { useTaskList } from './useTaskList';
import { TaskStatusBadge } from '@/features/task-detail/TaskStatusBadge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Link } from 'react-router';
import { useState, useRef, useCallback } from 'react';
import { Input } from '@/components/ui/input';
import { List, Ghost } from 'lucide-react';
import { formatUsd } from '@/lib/utils';
import { TaskStatus } from '@/types';

const STATUS_OPTIONS: { value: string; label: string }[] = [
    { value: '', label: 'All' },
    { value: 'queued', label: 'Queued' },
    { value: 'running', label: 'Running' },
    { value: 'waiting_for_approval', label: 'Awaiting Approval' },
    { value: 'waiting_for_input', label: 'Awaiting Input' },
    { value: 'paused', label: 'Paused' },
    { value: 'completed', label: 'Completed' },
    { value: 'cancelled', label: 'Cancelled' },
    { value: 'dead_letter', label: 'Failed' },
];

export function TaskListPage() {
    const [status, setStatus] = useState('');
    const [agentId, setAgentId] = useState('');
    const [debouncedAgentId, setDebouncedAgentId] = useState('');
    const debounceTimer = useRef<ReturnType<typeof setTimeout>>(null);
    const { data, isLoading } = useTaskList(status || undefined, debouncedAgentId || undefined);

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
                    <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.24em] text-primary">Task Browser</div>
                    <h2 className="text-3xl font-display font-semibold tracking-tight mb-2 flex items-center gap-2">
                        <List className="w-6 h-6 text-primary drop-shadow-[0_0_12px_var(--color-primary)]" />
                        Tasks
                    </h2>
                    <p className="text-muted-foreground">
                        All tasks across the runtime.
                    </p>
                </div>

                <div className="flex gap-4">
                    <div>
                        <label className="text-xs uppercase tracking-widest text-muted-foreground mb-2 block">
                            Status
                        </label>
                        <select
                            className="flex h-10 w-40 rounded-xl border border-white/10 bg-white/5 px-3 py-1 text-sm font-mono backdrop-blur-xl appearance-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                            value={status}
                            onChange={(e) => setStatus(e.target.value)}
                        >
                            {STATUS_OPTIONS.map((opt) => (
                                <option key={opt.value} value={opt.value}>{opt.label}</option>
                            ))}
                        </select>
                    </div>
                    <div>
                        <label className="text-xs uppercase tracking-widest text-muted-foreground mb-2 block">
                            Agent ID
                        </label>
                        <Input
                            className="rounded-none border-border bg-black/50 font-mono text-sm h-9 w-48"
                            placeholder="e.g. console-test"
                            value={agentId}
                            onChange={handleSearchChange}
                        />
                    </div>
                </div>
            </div>

            <div className="console-surface rounded-[28px] overflow-hidden">
                <Table>
                    <TableHeader className="sticky top-0 bg-[#0f1727]/90 backdrop-blur-xl">
                        <TableRow className="border-white/8 hover:bg-transparent">
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Task ID</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Agent</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Status</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10 text-right">Checkpoints</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10 text-right">Cost</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10 text-right">Created</TableHead>
                        </TableRow>
                    </TableHeader>
                    <TableBody>
                        {isLoading && (
                            <TableRow>
                                <TableCell colSpan={6} className="h-24 text-center">
                                    <span className="uppercase tracking-widest text-xs font-bold text-muted-foreground animate-pulse">Loading tasks...</span>
                                </TableCell>
                            </TableRow>
                        )}

                        {!isLoading && (!data?.items || data.items.length === 0) && (
                            <TableRow>
                                <TableCell colSpan={6} className="h-48 text-center text-muted-foreground hover:bg-transparent">
                                    <div className="flex flex-col items-center justify-center gap-2">
                                        <Ghost className="w-8 h-8 opacity-20 mb-2" />
                                        <span className="uppercase tracking-widest text-xs">No tasks found</span>
                                    </div>
                                </TableCell>
                            </TableRow>
                        )}

                        {data?.items.map((task) => (
                            <TableRow key={task.task_id} className="border-border/40 font-mono text-xs hover:bg-white/5 transition-colors">
                                <TableCell className="font-medium text-foreground">
                                    <Link
                                        to={`/tasks/${task.task_id}`}
                                        className="hover:text-primary hover:underline underline-offset-4 decoration-primary/50 transition-colors"
                                    >
                                        {task.task_id.substring(0, 8)}...{task.task_id.substring(task.task_id.length - 4)}
                                    </Link>
                                </TableCell>
                                <TableCell>
                                    <Link
                                        to={`/agents/${encodeURIComponent(task.agent_id)}`}
                                        className="hover:text-primary hover:underline underline-offset-4 decoration-primary/50 transition-colors"
                                    >
                                        {task.agent_display_name && (
                                            <span className="block text-foreground">{task.agent_display_name}</span>
                                        )}
                                        <span className="block text-muted-foreground text-[10px]">{task.agent_id}</span>
                                    </Link>
                                </TableCell>
                                <TableCell>
                                    <TaskStatusBadge status={task.status as TaskStatus} className="text-[10px] px-2 py-0.5" />
                                </TableCell>
                                <TableCell className="text-right tabular-nums">{task.checkpoint_count}</TableCell>
                                <TableCell className="text-right tabular-nums text-success">${formatUsd(task.total_cost_microdollars)}</TableCell>
                                <TableCell className="text-right text-muted-foreground">
                                    {new Date(task.created_at).toLocaleDateString()}
                                    <br />
                                    <span className="text-[10px]">{new Date(task.created_at).toLocaleTimeString()}</span>
                                </TableCell>
                            </TableRow>
                        ))}
                    </TableBody>
                </Table>
            </div>
        </div>
    );
}
