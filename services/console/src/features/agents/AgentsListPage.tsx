import { useState } from 'react';
import { Link } from 'react-router';
import { useAgents } from './useAgents';
import { CreateAgentDialog } from './CreateAgentDialog';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Bot, Ghost, Plus } from 'lucide-react';
import { formatUsd } from '@/lib/utils';

const STATUS_OPTIONS = [
    { value: '', label: 'All' },
    { value: 'active', label: 'Active' },
    { value: 'disabled', label: 'Disabled' },
];

export function AgentsListPage() {
    const [status, setStatus] = useState('');
    const [dialogOpen, setDialogOpen] = useState(false);
    const { data: agents = [], isLoading } = useAgents(status || undefined);

    return (
        <div className="space-y-6 animate-in fade-in duration-500">
            <div className="console-surface-strong rounded-[28px] p-6 md:p-7 flex flex-col md:flex-row md:items-end justify-between gap-4">
                <div>
                    <h2 className="text-3xl font-display font-semibold tracking-tight mb-2 flex items-center gap-2">
                        <Bot className="w-6 h-6 text-primary drop-shadow-[0_0_12px_var(--color-primary)]" />
                        Agents
                    </h2>
                    <p className="text-muted-foreground">
                        Manage reusable agent configurations.
                    </p>
                </div>

                <div className="flex gap-4 items-end">
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
                    <Button
                        onClick={() => setDialogOpen(true)}
                        className="font-bold uppercase tracking-widest px-6 hover:saturate-150 transition-all"
                    >
                        <Plus className="w-4 h-4 mr-2" />
                        Create Agent
                    </Button>
                </div>
            </div>

            <div className="console-surface rounded-[28px] overflow-hidden">
                <Table>
                    <TableHeader className="sticky top-0 bg-[#0f1727]/90 backdrop-blur-xl">
                        <TableRow className="border-white/8 hover:bg-transparent">
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Name</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Agent ID</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Provider</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Model</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Status</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10 text-right">Max Tasks</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10 text-right">Budget/Task</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10 text-right">Budget/Hour</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10 text-right">Created</TableHead>
                        </TableRow>
                    </TableHeader>
                    <TableBody>
                        {isLoading && (
                            <TableRow>
                                <TableCell colSpan={9} className="h-24 text-center">
                                    <span className="uppercase tracking-widest text-xs font-bold text-muted-foreground animate-pulse">Loading agents...</span>
                                </TableCell>
                            </TableRow>
                        )}

                        {!isLoading && agents.length === 0 && (
                            <TableRow>
                                <TableCell colSpan={9} className="h-48 text-center text-muted-foreground hover:bg-transparent">
                                    <div className="flex flex-col items-center justify-center gap-2">
                                        <Ghost className="w-8 h-8 opacity-20 mb-2" />
                                        <span className="uppercase tracking-widest text-xs">No agents found</span>
                                        <span className="text-xs">Create an agent to get started.</span>
                                    </div>
                                </TableCell>
                            </TableRow>
                        )}

                        {agents.map((agent) => (
                            <TableRow key={agent.agent_id} className="border-border/40 font-mono text-xs hover:bg-white/5 transition-colors">
                                <TableCell className="font-medium text-foreground">
                                    <Link
                                        to={`/agents/${encodeURIComponent(agent.agent_id)}`}
                                        className="hover:text-primary hover:underline underline-offset-4 decoration-primary/50 transition-colors"
                                    >
                                        {agent.display_name}
                                    </Link>
                                </TableCell>
                                <TableCell className="text-muted-foreground">{agent.agent_id}</TableCell>
                                <TableCell className="capitalize">{agent.provider}</TableCell>
                                <TableCell>{agent.model}</TableCell>
                                <TableCell>
                                    <Badge
                                        variant={agent.status === 'active' ? 'default' : 'secondary'}
                                        className={
                                            agent.status === 'active'
                                                ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30 text-[10px] px-2 py-0.5'
                                                : 'bg-zinc-500/20 text-zinc-400 border-zinc-500/30 text-[10px] px-2 py-0.5'
                                        }
                                    >
                                        {agent.status}
                                    </Badge>
                                </TableCell>
                                <TableCell className="text-right tabular-nums">{agent.max_concurrent_tasks}</TableCell>
                                <TableCell className="text-right tabular-nums text-success">${formatUsd(agent.budget_max_per_task)}</TableCell>
                                <TableCell className="text-right tabular-nums text-success">${formatUsd(agent.budget_max_per_hour)}</TableCell>
                                <TableCell className="text-right text-muted-foreground">
                                    {new Date(agent.created_at).toLocaleDateString()}
                                    <br />
                                    <span className="text-[10px]">{new Date(agent.created_at).toLocaleTimeString()}</span>
                                </TableCell>
                            </TableRow>
                        ))}
                    </TableBody>
                </Table>
            </div>

            <CreateAgentDialog open={dialogOpen} onOpenChange={setDialogOpen} />
        </div>
    );
}
