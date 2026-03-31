import { useState, useEffect } from 'react';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Button } from '@/components/ui/button';
import { LangfuseEndpointDialog } from './LangfuseEndpointDialog';
import {
    useLangfuseEndpoints,
    useCreateLangfuseEndpoint,
    useUpdateLangfuseEndpoint,
    useDeleteLangfuseEndpoint,
    useTestLangfuseEndpoint,
} from './useLangfuseEndpoints';
import type { LangfuseEndpoint, LangfuseEndpointRequest } from '@/types';
import { toast } from 'sonner';
import { Plus, Pencil, Trash2, Loader2 } from 'lucide-react';

export function LangfuseEndpointList() {
    const { data: endpoints = [], isLoading } = useLangfuseEndpoints();
    const createMutation = useCreateLangfuseEndpoint();
    const updateMutation = useUpdateLangfuseEndpoint();
    const deleteMutation = useDeleteLangfuseEndpoint();
    const testMutation = useTestLangfuseEndpoint();

    const [dialogOpen, setDialogOpen] = useState(false);
    const [editingEndpoint, setEditingEndpoint] = useState<LangfuseEndpoint | null>(null);
    const [testingIds, setTestingIds] = useState<Set<string>>(new Set());
    const [testResults, setTestResults] = useState<Record<string, boolean>>({});
    const [dialogError, setDialogError] = useState<string | null>(null);

    // Auto-test all endpoints on load
    useEffect(() => {
        if (endpoints.length === 0) return;
        endpoints.forEach((ep) => {
            if (testResults[ep.endpoint_id] !== undefined) return;
            setTestingIds((prev) => new Set(prev).add(ep.endpoint_id));
            testMutation.mutate(ep.endpoint_id, {
                onSuccess: (result) => {
                    setTestResults((prev) => ({ ...prev, [ep.endpoint_id]: result.reachable }));
                    setTestingIds((prev) => { const next = new Set(prev); next.delete(ep.endpoint_id); return next; });
                },
                onError: () => {
                    setTestResults((prev) => ({ ...prev, [ep.endpoint_id]: false }));
                    setTestingIds((prev) => { const next = new Set(prev); next.delete(ep.endpoint_id); return next; });
                },
            });
        });
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [endpoints.length]);

    const handleCreate = () => {
        setEditingEndpoint(null);
        setDialogError(null);
        setDialogOpen(true);
    };

    const handleEdit = (endpoint: LangfuseEndpoint) => {
        setEditingEndpoint(endpoint);
        setDialogError(null);
        setDialogOpen(true);
    };

    const handleDelete = (endpoint: LangfuseEndpoint) => {
        if (!confirm(`Delete endpoint "${endpoint.name}"? Tasks using this endpoint will no longer send traces.`)) return;
        deleteMutation.mutate(endpoint.endpoint_id, {
            onSuccess: () => toast.success(`Endpoint "${endpoint.name}" deleted`),
            onError: (err: Error) => toast.error(err.message || 'Failed to delete endpoint'),
        });
    };

    const handleTest = (endpoint: LangfuseEndpoint) => {
        setTestingIds((prev) => new Set(prev).add(endpoint.endpoint_id));
        setTestResults((prev) => { const next = { ...prev }; delete next[endpoint.endpoint_id]; return next; });
        testMutation.mutate(endpoint.endpoint_id, {
            onSuccess: (result) => {
                setTestResults((prev) => ({ ...prev, [endpoint.endpoint_id]: result.reachable }));
                if (result.reachable) {
                    toast.success(result.message);
                } else {
                    toast.error(result.message);
                }
                setTestingIds((prev) => { const next = new Set(prev); next.delete(endpoint.endpoint_id); return next; });
            },
            onError: (err: Error) => {
                setTestResults((prev) => ({ ...prev, [endpoint.endpoint_id]: false }));
                toast.error(err.message || 'Connection test failed');
                setTestingIds((prev) => { const next = new Set(prev); next.delete(endpoint.endpoint_id); return next; });
            },
        });
    };

    const handleDialogSubmit = (request: LangfuseEndpointRequest) => {
        setDialogError(null);
        if (editingEndpoint) {
            updateMutation.mutate(
                { endpointId: editingEndpoint.endpoint_id, request },
                {
                    onSuccess: () => {
                        toast.success(`Endpoint "${request.name}" updated`);
                        setDialogOpen(false);
                    },
                    onError: (err: Error) => {
                        const msg = err.message || 'Failed to update endpoint';
                        setDialogError(msg);
                        toast.error(msg);
                    },
                },
            );
        } else {
            createMutation.mutate(request, {
                onSuccess: () => {
                    toast.success(`Endpoint "${request.name}" created`);
                    setDialogOpen(false);
                },
                onError: (err: Error) => {
                    const msg = err.message || 'Failed to create endpoint';
                    setDialogError(msg);
                    toast.error(msg);
                },
            });
        }
    };

    return (
        <div className="space-y-4">
            <div className="flex items-center justify-between">
                <h3 className="text-sm font-display uppercase tracking-widest text-muted-foreground">
                    Langfuse Endpoints
                </h3>
                <Button
                    variant="outline"
                    className="uppercase tracking-[0.18em] font-bold text-xs"
                    onClick={handleCreate}
                >
                    <Plus className="w-4 h-4 mr-2" /> Add Endpoint
                </Button>
            </div>

            {isLoading ? (
                <div className="border border-dashed border-border/40 p-4 text-xs uppercase tracking-widest text-muted-foreground">
                    Loading endpoints...
                </div>
            ) : endpoints.length === 0 ? (
                <div className="border border-dashed border-border/40 p-6 text-xs uppercase tracking-widest text-muted-foreground text-center">
                    No Langfuse endpoints configured
                </div>
            ) : (
                <div className="border border-white/10 rounded-lg overflow-hidden">
                    <Table>
                        <TableHeader>
                            <TableRow className="border-white/8">
                                <TableHead className="text-xs uppercase tracking-widest">Name</TableHead>
                                <TableHead className="text-xs uppercase tracking-widest">Host</TableHead>
                                <TableHead className="text-xs uppercase tracking-widest">Created</TableHead>
                                <TableHead className="text-xs uppercase tracking-widest text-right">Actions</TableHead>
                            </TableRow>
                        </TableHeader>
                        <TableBody>
                            {endpoints.map((endpoint) => (
                                <TableRow key={endpoint.endpoint_id}>
                                    <TableCell className="font-medium text-sm">{endpoint.name}</TableCell>
                                    <TableCell className="text-xs font-mono text-muted-foreground">{endpoint.host}</TableCell>
                                    <TableCell className="text-xs text-muted-foreground">
                                        {new Date(endpoint.created_at).toLocaleDateString()}
                                    </TableCell>
                                    <TableCell className="text-right">
                                        <div className="flex items-center justify-end gap-2">
                                            <Button
                                                variant="ghost"
                                                size="sm"
                                                className={`h-auto px-2 py-1 text-xs font-mono ${testResults[endpoint.endpoint_id] === true ? 'text-green-500' : testResults[endpoint.endpoint_id] === false ? 'text-destructive' : ''}`}
                                                onClick={() => handleTest(endpoint)}
                                                disabled={testingIds.has(endpoint.endpoint_id)}
                                            >
                                                {testingIds.has(endpoint.endpoint_id) ? (
                                                    <Loader2 className="w-3 h-3 animate-spin" />
                                                ) : testResults[endpoint.endpoint_id] === true ? (
                                                    'Connected'
                                                ) : testResults[endpoint.endpoint_id] === false ? (
                                                    'Failed'
                                                ) : (
                                                    'Test'
                                                )}
                                            </Button>
                                            <Button
                                                variant="ghost"
                                                size="sm"
                                                className="h-8 w-8 p-0"
                                                onClick={() => handleEdit(endpoint)}
                                                title="Edit"
                                            >
                                                <Pencil className="w-4 h-4" />
                                            </Button>
                                            <Button
                                                variant="ghost"
                                                size="sm"
                                                className="h-8 w-8 p-0 text-destructive hover:text-destructive"
                                                onClick={() => handleDelete(endpoint)}
                                                disabled={deleteMutation.isPending}
                                                title="Delete"
                                            >
                                                <Trash2 className="w-4 h-4" />
                                            </Button>
                                        </div>
                                    </TableCell>
                                </TableRow>
                            ))}
                        </TableBody>
                    </Table>
                </div>
            )}

            <LangfuseEndpointDialog
                open={dialogOpen}
                onClose={() => setDialogOpen(false)}
                onSubmit={handleDialogSubmit}
                isPending={createMutation.isPending || updateMutation.isPending}
                submitError={dialogError}
                endpoint={editingEndpoint}
            />
        </div>
    );
}
