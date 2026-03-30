import { useState } from 'react';
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
import { Plus, Pencil, Trash2, Wifi, Loader2 } from 'lucide-react';

export function LangfuseEndpointList() {
    const { data: endpoints = [], isLoading } = useLangfuseEndpoints();
    const createMutation = useCreateLangfuseEndpoint();
    const updateMutation = useUpdateLangfuseEndpoint();
    const deleteMutation = useDeleteLangfuseEndpoint();
    const testMutation = useTestLangfuseEndpoint();

    const [dialogOpen, setDialogOpen] = useState(false);
    const [editingEndpoint, setEditingEndpoint] = useState<LangfuseEndpoint | null>(null);
    const [testingId, setTestingId] = useState<string | null>(null);

    const handleCreate = () => {
        setEditingEndpoint(null);
        setDialogOpen(true);
    };

    const handleEdit = (endpoint: LangfuseEndpoint) => {
        setEditingEndpoint(endpoint);
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
        setTestingId(endpoint.endpoint_id);
        testMutation.mutate(endpoint.endpoint_id, {
            onSuccess: (result) => {
                if (result.reachable) {
                    toast.success(result.message);
                } else {
                    toast.error(result.message);
                }
                setTestingId(null);
            },
            onError: (err: Error) => {
                toast.error(err.message || 'Connection test failed');
                setTestingId(null);
            },
        });
    };

    const handleDialogSubmit = (request: LangfuseEndpointRequest) => {
        if (editingEndpoint) {
            updateMutation.mutate(
                { endpointId: editingEndpoint.endpoint_id, request },
                {
                    onSuccess: () => {
                        toast.success(`Endpoint "${request.name}" updated`);
                        setDialogOpen(false);
                    },
                    onError: (err: Error) => toast.error(err.message || 'Failed to update endpoint'),
                },
            );
        } else {
            createMutation.mutate(request, {
                onSuccess: () => {
                    toast.success(`Endpoint "${request.name}" created`);
                    setDialogOpen(false);
                },
                onError: (err: Error) => toast.error(err.message || 'Failed to create endpoint'),
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
                                                className="h-8 w-8 p-0"
                                                onClick={() => handleTest(endpoint)}
                                                disabled={testingId === endpoint.endpoint_id}
                                                title="Test Connection"
                                            >
                                                {testingId === endpoint.endpoint_id ? (
                                                    <Loader2 className="w-4 h-4 animate-spin" />
                                                ) : (
                                                    <Wifi className="w-4 h-4" />
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
                endpoint={editingEndpoint}
            />
        </div>
    );
}
