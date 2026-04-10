import { useState } from 'react';
import { useNavigate } from 'react-router';
import { Server, Plus, Ghost } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { useToolServers } from './useToolServers';
import { RegisterToolServerDialog } from './RegisterToolServerDialog';

const STATUS_OPTIONS = [
    { value: '', label: 'All' },
    { value: 'active', label: 'Active' },
    { value: 'disabled', label: 'Disabled' },
];

export function ToolServersListPage() {
    const [status, setStatus] = useState('');
    const [dialogOpen, setDialogOpen] = useState(false);
    const navigate = useNavigate();
    const { data: servers = [], isLoading } = useToolServers(status || undefined);

    return (
        <div className="space-y-6 animate-in fade-in duration-500">
            <div className="console-surface-strong rounded-[28px] p-6 md:p-7 flex flex-col md:flex-row md:items-end justify-between gap-4">
                <div>
                    <h2 className="text-3xl font-display font-semibold tracking-tight mb-2 flex items-center gap-2">
                        <Server className="w-6 h-6 text-primary drop-shadow-[0_0_12px_var(--color-primary)]" />
                        Tool Servers
                    </h2>
                    <p className="text-muted-foreground">
                        External MCP tool servers that agents can use for custom tool capabilities.
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
                        Register Tool Server
                    </Button>
                </div>
            </div>

            <div className="console-surface rounded-[28px] overflow-hidden">
                <Table>
                    <TableHeader className="sticky top-0 bg-[#0f1727]/90 backdrop-blur-xl">
                        <TableRow className="border-white/8 hover:bg-transparent">
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Name</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">URL</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Auth</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10">Status</TableHead>
                            <TableHead className="font-display uppercase tracking-widest text-xs h-10 text-right">Created</TableHead>
                        </TableRow>
                    </TableHeader>
                    <TableBody>
                        {isLoading && (
                            <TableRow>
                                <TableCell colSpan={5} className="h-24 text-center">
                                    <span className="uppercase tracking-widest text-xs font-bold text-muted-foreground animate-pulse">Loading tool servers...</span>
                                </TableCell>
                            </TableRow>
                        )}

                        {!isLoading && servers.length === 0 && (
                            <TableRow>
                                <TableCell colSpan={5} className="h-48 text-center text-muted-foreground hover:bg-transparent">
                                    <div className="flex flex-col items-center justify-center gap-2">
                                        <Ghost className="w-8 h-8 opacity-20 mb-2" />
                                        <span className="uppercase tracking-widest text-xs">No tool servers registered</span>
                                        <span className="text-xs">Register one to give your agents custom tools.</span>
                                    </div>
                                </TableCell>
                            </TableRow>
                        )}

                        {servers.map((server) => (
                            <TableRow
                                key={server.server_id}
                                className="border-border/40 font-mono text-xs hover:bg-white/5 transition-colors cursor-pointer"
                                onClick={() => navigate(`/tool-servers/${encodeURIComponent(server.server_id)}`)}
                            >
                                <TableCell className="font-medium text-foreground">{server.name}</TableCell>
                                <TableCell className="text-muted-foreground truncate max-w-[300px]">{server.url}</TableCell>
                                <TableCell className="text-muted-foreground">
                                    {server.auth_type === 'bearer_token' ? 'Bearer Token' : 'None'}
                                </TableCell>
                                <TableCell>
                                    <Badge
                                        variant={server.status === 'active' ? 'default' : 'secondary'}
                                        className={
                                            server.status === 'active'
                                                ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30 text-[10px] px-2 py-0.5'
                                                : 'bg-zinc-500/20 text-zinc-400 border-zinc-500/30 text-[10px] px-2 py-0.5'
                                        }
                                    >
                                        {server.status}
                                    </Badge>
                                </TableCell>
                                <TableCell className="text-right text-muted-foreground">
                                    {new Date(server.created_at).toLocaleDateString()}
                                    <br />
                                    <span className="text-[10px]">{new Date(server.created_at).toLocaleTimeString()}</span>
                                </TableCell>
                            </TableRow>
                        ))}
                    </TableBody>
                </Table>
            </div>

            <RegisterToolServerDialog open={dialogOpen} onOpenChange={setDialogOpen} />
        </div>
    );
}
