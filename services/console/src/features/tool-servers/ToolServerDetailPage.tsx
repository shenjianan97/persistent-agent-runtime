import { useParams, useNavigate } from 'react-router';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { toast } from 'sonner';
import { useEffect, useState } from 'react';
import {
    Form, FormControl, FormField, FormItem, FormLabel, FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import {
    Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/ui/dialog';
import { Server, Pencil, X, Trash2, Search, ArrowLeft } from 'lucide-react';
import { useToolServer, useUpdateToolServer, useDeleteToolServer, useDiscoverToolServer } from './useToolServers';
import type { DiscoveredToolInfo } from '@/types';

const editSchema = z.object({
    name: z.string()
        .min(1, 'Name is required')
        .max(100)
        .regex(/^[a-z0-9][a-z0-9-]*$/, 'Must start with lowercase alphanumeric and contain only lowercase letters, numbers, and hyphens'),
    url: z.string()
        .min(1, 'URL is required')
        .max(2048)
        .url('Must be a valid URL'),
    auth_type: z.enum(['none', 'bearer_token']),
    auth_token: z.string().optional(),
    status: z.enum(['active', 'disabled']),
});

type EditFormValues = z.infer<typeof editSchema>;

export function ToolServerDetailPage() {
    const { serverId } = useParams<{ serverId: string }>();
    const navigate = useNavigate();
    const { data: server, isLoading, error } = useToolServer(serverId!);
    const updateMutation = useUpdateToolServer();
    const deleteMutation = useDeleteToolServer();
    const discoverMutation = useDiscoverToolServer();
    const [isEditing, setIsEditing] = useState(false);
    const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
    const [discoveredTools, setDiscoveredTools] = useState<DiscoveredToolInfo[] | null>(null);
    const [discoverStatus, setDiscoverStatus] = useState<'reachable' | 'unreachable' | null>(null);
    const [discoverError, setDiscoverError] = useState<string | null>(null);

    const form = useForm<EditFormValues>({
        resolver: zodResolver(editSchema),
        defaultValues: {
            name: '',
            url: '',
            auth_type: 'none',
            auth_token: '',
            status: 'active',
        },
    });

    useEffect(() => {
        if (server) {
            form.reset({
                name: server.name,
                url: server.url,
                auth_type: server.auth_type,
                auth_token: '',
                status: server.status,
            });
        }
    }, [server, form]);

    function onSubmit(data: EditFormValues) {
        if (!serverId) return;
        updateMutation.mutate(
            {
                serverId,
                request: {
                    name: data.name,
                    url: data.url,
                    auth_type: data.auth_type,
                    auth_token: data.auth_type === 'bearer_token' && data.auth_token ? data.auth_token : undefined,
                    status: data.status,
                },
            },
            {
                onSuccess: () => {
                    toast.success('Tool server updated', {
                        description: 'Configuration saved successfully.',
                    });
                    setIsEditing(false);
                },
                onError: (error: Error) => {
                    toast.error('Failed to update tool server', {
                        description: error.message || 'Unknown error occurred.',
                    });
                },
            }
        );
    }

    function handleDelete() {
        if (!serverId) return;
        deleteMutation.mutate(serverId, {
            onSuccess: () => {
                toast.success('Tool server deleted');
                navigate('/tool-servers');
            },
            onError: (error: Error) => {
                toast.error('Failed to delete tool server', {
                    description: error.message || 'Unknown error occurred.',
                });
                setDeleteDialogOpen(false);
            },
        });
    }

    function handleDiscover() {
        if (!serverId) return;
        setDiscoveredTools(null);
        setDiscoverStatus(null);
        setDiscoverError(null);
        discoverMutation.mutate(serverId, {
            onSuccess: (result) => {
                setDiscoveredTools(result.tools);
                setDiscoverStatus(result.status);
                setDiscoverError(result.error);
                if (result.status === 'reachable') {
                    toast.success('Discovery complete', {
                        description: `Found ${result.tools.length} tool(s).`,
                    });
                } else {
                    toast.error('Server unreachable', {
                        description: result.error ?? 'Could not connect to tool server.',
                    });
                }
            },
            onError: (error: Error) => {
                toast.error('Discovery failed', {
                    description: error.message || 'Unknown error occurred.',
                });
            },
        });
    }

    function handleCancel() {
        form.reset();
        setIsEditing(false);
    }

    if (isLoading) {
        return (
            <div className="space-y-6 animate-in fade-in duration-500">
                <div className="console-surface-strong rounded-[28px] p-6 md:p-7">
                    <span className="uppercase tracking-widest text-xs font-bold text-muted-foreground animate-pulse">Loading tool server...</span>
                </div>
            </div>
        );
    }

    if (error || !server) {
        return (
            <div className="space-y-6 animate-in fade-in duration-500">
                <div className="console-surface-strong rounded-[28px] p-6 md:p-7">
                    <h2 className="text-xl font-display font-semibold text-destructive mb-2">Tool Server Not Found</h2>
                    <p className="text-muted-foreground text-sm">
                        The tool server <code className="font-mono text-foreground">{serverId}</code> could not be found.
                    </p>
                </div>
            </div>
        );
    }

    const authType = form.watch('auth_type');

    const readOnlyField = (label: string, value: React.ReactNode) => (
        <div>
            <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">{label}</span>
            <span className="text-foreground text-sm font-mono">{value}</span>
        </div>
    );

    return (
        <div className="space-y-6 animate-in fade-in duration-500">
            {/* Header */}
            <div className="console-surface-strong rounded-[28px] p-6 md:p-7 mb-8 flex flex-col md:flex-row md:items-end justify-between gap-4">
                <div>
                    <button
                        onClick={() => navigate('/tool-servers')}
                        className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 mb-3 uppercase tracking-widest transition-colors"
                    >
                        <ArrowLeft className="w-3 h-3" />
                        Tool Servers
                    </button>
                    <h2 className="text-3xl font-display font-semibold tracking-tight mb-1 flex items-center gap-2">
                        <Server className="w-6 h-6 text-primary drop-shadow-[0_0_12px_var(--color-primary)]" />
                        {server.name}
                    </h2>
                    <p className="text-muted-foreground font-mono text-sm">{server.server_id}</p>
                    <div className="mt-2">
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
                    </div>
                </div>
                {!isEditing && (
                    <div className="flex gap-3">
                        <Button
                            onClick={handleDiscover}
                            disabled={discoverMutation.isPending}
                            variant="outline"
                            className="font-bold uppercase tracking-widest px-6 border-primary/50 text-primary hover:bg-primary hover:text-black transition-all"
                        >
                            <Search className="w-4 h-4 mr-2" />
                            {discoverMutation.isPending ? 'Discovering...' : 'Discover Tools'}
                        </Button>
                        <Button
                            onClick={() => setIsEditing(true)}
                            variant="outline"
                            className="font-bold uppercase tracking-widest px-6 border-primary text-primary hover:bg-primary hover:text-black transition-all"
                        >
                            <Pencil className="w-4 h-4 mr-2" />
                            Edit
                        </Button>
                        <Button
                            onClick={() => setDeleteDialogOpen(true)}
                            variant="outline"
                            className="font-bold uppercase tracking-widest px-6 border-destructive/50 text-destructive hover:bg-destructive hover:text-destructive-foreground transition-all"
                        >
                            <Trash2 className="w-4 h-4 mr-2" />
                            Delete
                        </Button>
                    </div>
                )}
            </div>

            {!isEditing ? (
                <div className="space-y-6">
                    {/* Server details */}
                    <Card className="console-surface border-white/10">
                        <CardHeader className="border-b border-white/8 pb-4">
                            <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Configuration</CardTitle>
                        </CardHeader>
                        <CardContent className="pt-6 space-y-5">
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                {readOnlyField('Name', server.name)}
                                {readOnlyField('URL', server.url)}
                            </div>
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                {readOnlyField('Auth Type', server.auth_type === 'bearer_token' ? 'Bearer Token' : 'None')}
                                {server.auth_type === 'bearer_token' && readOnlyField('Auth Token', '••••••••')}
                            </div>
                        </CardContent>
                    </Card>

                    <Card className="console-surface border-white/10">
                        <CardHeader className="border-b border-white/8 pb-4">
                            <CardTitle className="text-sm font-display uppercase tracking-widest">Lifecycle</CardTitle>
                        </CardHeader>
                        <CardContent className="pt-6 space-y-5">
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                {readOnlyField('Status', server.status)}
                                {readOnlyField('Created', new Date(server.created_at).toLocaleString())}
                            </div>
                            {readOnlyField('Last Updated', new Date(server.updated_at).toLocaleString())}
                        </CardContent>
                    </Card>

                    {/* Discovered Tools section */}
                    {discoveredTools !== null && (
                        <Card className="console-surface border-white/10">
                            <CardHeader className="border-b border-white/8 pb-4">
                                <CardTitle className="text-sm font-display uppercase tracking-widest text-primary flex items-center gap-2">
                                    Discovered Tools
                                    {discoverStatus && (
                                        <Badge
                                            variant={discoverStatus === 'reachable' ? 'default' : 'secondary'}
                                            className={
                                                discoverStatus === 'reachable'
                                                    ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30 text-[10px] px-2 py-0.5'
                                                    : 'bg-red-500/20 text-red-400 border-red-500/30 text-[10px] px-2 py-0.5'
                                            }
                                        >
                                            {discoverStatus}
                                        </Badge>
                                    )}
                                </CardTitle>
                            </CardHeader>
                            <CardContent className="pt-0">
                                {discoverError && (
                                    <p className="text-xs text-destructive py-4">{discoverError}</p>
                                )}
                                {discoveredTools.length === 0 && !discoverError && (
                                    <p className="text-xs text-muted-foreground py-4">No tools discovered from this server.</p>
                                )}
                                {discoveredTools.length > 0 && (
                                    <Table>
                                        <TableHeader>
                                            <TableRow className="border-white/8 hover:bg-transparent">
                                                <TableHead className="font-display uppercase tracking-widest text-xs h-10">Tool Name</TableHead>
                                                <TableHead className="font-display uppercase tracking-widest text-xs h-10">Description</TableHead>
                                            </TableRow>
                                        </TableHeader>
                                        <TableBody>
                                            {discoveredTools.map((tool) => (
                                                <TableRow key={tool.name} className="border-border/40 font-mono text-xs hover:bg-white/5 transition-colors">
                                                    <TableCell className="font-medium text-foreground">{tool.name}</TableCell>
                                                    <TableCell className="text-muted-foreground">{tool.description}</TableCell>
                                                </TableRow>
                                            ))}
                                        </TableBody>
                                    </Table>
                                )}
                            </CardContent>
                        </Card>
                    )}
                </div>
            ) : (
                <Form {...form}>
                    <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-6">
                        <Card className="console-surface border-white/10">
                            <CardHeader className="border-b border-white/8 pb-4">
                                <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Configuration</CardTitle>
                            </CardHeader>
                            <CardContent className="pt-6 space-y-6">
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                    <FormField
                                        control={form.control}
                                        name="name"
                                        render={({ field }) => (
                                            <FormItem>
                                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Name</FormLabel>
                                                <FormControl>
                                                    <Input className="rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1" {...field} />
                                                </FormControl>
                                                <FormMessage className="text-destructive font-bold text-xs" />
                                            </FormItem>
                                        )}
                                    />
                                    <FormField
                                        control={form.control}
                                        name="url"
                                        render={({ field }) => (
                                            <FormItem>
                                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">URL</FormLabel>
                                                <FormControl>
                                                    <Input className="rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1" {...field} />
                                                </FormControl>
                                                <FormMessage className="text-destructive font-bold text-xs" />
                                            </FormItem>
                                        )}
                                    />
                                </div>

                                <FormField
                                    control={form.control}
                                    name="auth_type"
                                    render={({ field }) => (
                                        <FormItem>
                                            <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Authentication</FormLabel>
                                            <FormControl>
                                                <select
                                                    className="flex h-10 w-full border border-border bg-black/50 px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary focus-visible:ring-offset-0 rounded-none appearance-none"
                                                    value={field.value}
                                                    onChange={field.onChange}
                                                >
                                                    <option value="none">None</option>
                                                    <option value="bearer_token">Bearer Token</option>
                                                </select>
                                            </FormControl>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
                                />

                                {authType === 'bearer_token' && (
                                    <FormField
                                        control={form.control}
                                        name="auth_token"
                                        render={({ field }) => (
                                            <FormItem>
                                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">
                                                    Auth Token <span className="text-muted-foreground/60">(leave blank to keep existing)</span>
                                                </FormLabel>
                                                <FormControl>
                                                    <Input
                                                        type="password"
                                                        className="rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1"
                                                        placeholder="New bearer token"
                                                        {...field}
                                                    />
                                                </FormControl>
                                                <FormMessage className="text-destructive font-bold text-xs" />
                                            </FormItem>
                                        )}
                                    />
                                )}
                            </CardContent>
                        </Card>

                        <Card className="console-surface border-white/10">
                            <CardHeader className="border-b border-white/8 pb-4">
                                <CardTitle className="text-sm font-display uppercase tracking-widest">Lifecycle</CardTitle>
                            </CardHeader>
                            <CardContent className="pt-6">
                                <FormField
                                    control={form.control}
                                    name="status"
                                    render={({ field }) => (
                                        <FormItem>
                                            <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Status</FormLabel>
                                            <FormControl>
                                                <select
                                                    className="flex h-10 w-48 border border-border bg-black/50 px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary focus-visible:ring-offset-0 rounded-none appearance-none"
                                                    value={field.value}
                                                    onChange={field.onChange}
                                                >
                                                    <option value="active">Active</option>
                                                    <option value="disabled">Disabled</option>
                                                </select>
                                            </FormControl>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
                                />
                            </CardContent>
                        </Card>

                        <div className="flex justify-end gap-3 pt-4 pb-12">
                            <Button
                                type="button"
                                variant="ghost"
                                onClick={handleCancel}
                                className="uppercase tracking-widest text-xs"
                            >
                                <X className="w-4 h-4 mr-2" />
                                Cancel
                            </Button>
                            <Button
                                type="submit"
                                disabled={updateMutation.isPending}
                                className="font-bold uppercase tracking-widest px-8 hover:saturate-150 transition-all"
                            >
                                {updateMutation.isPending ? 'Saving...' : 'Save Changes'}
                            </Button>
                        </div>
                    </form>
                </Form>
            )}

            {/* Delete confirmation dialog */}
            <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
                <DialogContent className="sm:max-w-[440px] console-surface border-white/10 rounded-2xl">
                    <DialogHeader>
                        <DialogTitle className="text-lg font-display uppercase tracking-widest text-destructive">
                            Delete Tool Server
                        </DialogTitle>
                    </DialogHeader>
                    <p className="text-sm text-muted-foreground">
                        Are you sure you want to delete <span className="font-mono text-foreground">{server.name}</span>?
                        This action cannot be undone.
                    </p>
                    <DialogFooter>
                        <Button
                            type="button"
                            variant="ghost"
                            onClick={() => setDeleteDialogOpen(false)}
                            className="uppercase tracking-widest text-xs"
                        >
                            Cancel
                        </Button>
                        <Button
                            type="button"
                            onClick={handleDelete}
                            disabled={deleteMutation.isPending}
                            className="font-bold uppercase tracking-widest px-6 bg-destructive hover:bg-destructive/90 text-destructive-foreground"
                        >
                            {deleteMutation.isPending ? 'Deleting...' : 'Delete'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    );
}
