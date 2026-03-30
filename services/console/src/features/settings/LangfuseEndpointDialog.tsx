import { useState, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useTestLangfuseEndpoint } from './useLangfuseEndpoints';
import type { LangfuseEndpoint, LangfuseEndpointRequest } from '@/types';
import { X, Wifi, Loader2 } from 'lucide-react';

interface LangfuseEndpointDialogProps {
    open: boolean;
    onClose: () => void;
    onSubmit: (request: LangfuseEndpointRequest) => void;
    isPending: boolean;
    endpoint?: LangfuseEndpoint | null;
}

export function LangfuseEndpointDialog({ open, onClose, onSubmit, isPending, endpoint }: LangfuseEndpointDialogProps) {
    const [name, setName] = useState('');
    const [host, setHost] = useState('');
    const [publicKey, setPublicKey] = useState('');
    const [secretKey, setSecretKey] = useState('');
    const [testResult, setTestResult] = useState<{ reachable: boolean; message: string } | null>(null);

    const testMutation = useTestLangfuseEndpoint();

    const isEditMode = !!endpoint;

    useEffect(() => {
        if (open) {
            setName(endpoint?.name ?? '');
            setHost(endpoint?.host ?? '');
            setPublicKey('');
            setSecretKey('');
            setTestResult(null);
        }
    }, [open, endpoint]);

    if (!open) return null;

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        onSubmit({ name, host, public_key: publicKey, secret_key: secretKey });
    };

    const handleTest = () => {
        if (!endpoint) return;
        setTestResult(null);
        testMutation.mutate(endpoint.endpoint_id, {
            onSuccess: (result) => setTestResult(result),
            onError: (err: Error) => setTestResult({ reachable: false, message: err.message }),
        });
    };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
            <div className="fixed inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
            <div className="relative z-50 w-full max-w-lg mx-4 border border-white/10 bg-[#0c1422] shadow-2xl rounded-2xl">
                <div className="flex items-center justify-between px-6 py-4 border-b border-white/8">
                    <h3 className="text-sm font-display uppercase tracking-widest text-primary">
                        {isEditMode ? 'Edit Endpoint' : 'Add Endpoint'}
                    </h3>
                    <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
                        <X className="w-4 h-4" />
                    </button>
                </div>

                <form onSubmit={handleSubmit} className="p-6 space-y-5">
                    <div className="space-y-2">
                        <Label className="uppercase tracking-widest text-muted-foreground text-xs">Name</Label>
                        <Input
                            className="rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1"
                            placeholder="e.g., Production Langfuse"
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            required
                        />
                    </div>

                    <div className="space-y-2">
                        <Label className="uppercase tracking-widest text-muted-foreground text-xs">Host URL</Label>
                        <Input
                            className="rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1"
                            placeholder="e.g., https://langfuse.example.com"
                            value={host}
                            onChange={(e) => setHost(e.target.value)}
                            required
                        />
                    </div>

                    <div className="space-y-2">
                        <Label className="uppercase tracking-widest text-muted-foreground text-xs">Public Key</Label>
                        <Input
                            type="password"
                            className="rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1"
                            placeholder="pk-lf-..."
                            value={publicKey}
                            onChange={(e) => setPublicKey(e.target.value)}
                            required={!isEditMode}
                        />
                    </div>

                    <div className="space-y-2">
                        <Label className="uppercase tracking-widest text-muted-foreground text-xs">Secret Key</Label>
                        <Input
                            type="password"
                            className="rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1"
                            placeholder="sk-lf-..."
                            value={secretKey}
                            onChange={(e) => setSecretKey(e.target.value)}
                            required={!isEditMode}
                        />
                    </div>

                    {isEditMode && (
                        <div className="space-y-2">
                            <Button
                                type="button"
                                variant="outline"
                                className="uppercase tracking-[0.18em] font-bold text-xs"
                                onClick={handleTest}
                                disabled={testMutation.isPending}
                            >
                                {testMutation.isPending ? (
                                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                                ) : (
                                    <Wifi className="w-4 h-4 mr-2" />
                                )}
                                Test Connection
                            </Button>
                            {testResult && (
                                <div className={`text-xs uppercase tracking-widest px-3 py-2 border ${testResult.reachable ? 'border-success/40 text-success bg-success/10' : 'border-destructive/40 text-destructive bg-destructive/10'}`}>
                                    {testResult.message}
                                </div>
                            )}
                        </div>
                    )}

                    <div className="flex justify-end gap-3 pt-2">
                        <Button
                            type="button"
                            variant="outline"
                            className="uppercase tracking-[0.18em] font-bold text-xs"
                            onClick={onClose}
                        >
                            Cancel
                        </Button>
                        <Button
                            type="submit"
                            disabled={isPending}
                            className="rounded-none font-bold uppercase tracking-widest px-6 hover:saturate-150 transition-all border border-primary text-black"
                        >
                            {isPending ? 'Saving...' : isEditMode ? 'Update' : 'Create'}
                        </Button>
                    </div>
                </form>
            </div>
        </div>
    );
}
