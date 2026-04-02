import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';
import { TaskStatusResponse } from '@/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { ShieldCheck, X, Clock } from 'lucide-react';
import { toast } from 'sonner';
import { useTimeoutCountdown } from './useTimeoutCountdown';

interface ApprovalPanelProps {
    task: TaskStatusResponse;
    onActionComplete?: () => void;
}

export function ApprovalPanel({ task, onActionComplete }: ApprovalPanelProps) {
    const queryClient = useQueryClient();
    const [showRejectForm, setShowRejectForm] = useState(false);
    const [rejectReason, setRejectReason] = useState('');
    const remaining = useTimeoutCountdown(task.human_input_timeout_at);

    const approveMutation = useMutation({
        mutationFn: () => api.approveTask(task.task_id),
        onSuccess: () => {
            toast.success('Task approved');
            queryClient.invalidateQueries({ queryKey: ['task', task.task_id] });
            onActionComplete?.();
        },
        onError: (err: Error) => toast.error(err.message || 'Failed to approve task'),
    });

    const rejectMutation = useMutation({
        mutationFn: () => api.rejectTask(task.task_id, rejectReason),
        onSuccess: () => {
            toast.success('Task rejected');
            queryClient.invalidateQueries({ queryKey: ['task', task.task_id] });
            onActionComplete?.();
        },
        onError: (err: Error) => toast.error(err.message || 'Failed to reject task'),
    });

    const isPending = approveMutation.isPending || rejectMutation.isPending;
    const action = task.pending_approval_action;

    return (
        <Card className="border-amber-500/30 bg-amber-500/5 overflow-hidden relative">
            <div className="absolute top-0 left-0 w-1 h-full bg-amber-500" />
            <CardHeader className="border-b border-amber-500/20">
                <CardTitle className="text-sm font-display uppercase tracking-widest flex items-center gap-2 text-amber-400">
                    <ShieldCheck className="w-4 h-4" /> Approval Required
                </CardTitle>
            </CardHeader>
            <CardContent className="pt-4 space-y-4">
                {action && (
                    <div className="space-y-2">
                        {'tool_name' in action && action.tool_name != null && (
                            <div className="text-xs font-mono uppercase tracking-wider text-muted-foreground">
                                Tool: <span className="text-amber-400 font-semibold">{String(action.tool_name)}</span>
                            </div>
                        )}
                        {'tool_args' in action && action.tool_args != null && (
                            <pre className="text-xs font-mono text-amber-300/80 bg-black/40 border border-amber-500/20 p-3 overflow-auto max-h-[200px] whitespace-pre-wrap break-all">
                                {JSON.stringify(action.tool_args, null, 2)}
                            </pre>
                        )}
                    </div>
                )}

                {remaining !== null && (
                    <div className="flex items-center gap-2 text-xs font-mono text-muted-foreground">
                        <Clock className="w-3 h-3" />
                        <span>Timeout in <span className="text-amber-400 font-bold">{remaining}</span></span>
                    </div>
                )}

                {showRejectForm ? (
                    <div className="space-y-3">
                        <Textarea
                            placeholder="Reason for rejection..."
                            value={rejectReason}
                            onChange={(e) => setRejectReason(e.target.value)}
                            className="border-amber-500/20 bg-black/40 text-sm min-h-[60px]"
                        />
                        <div className="flex gap-3">
                            <Button
                                variant="outline"
                                size="sm"
                                className="uppercase tracking-[0.18em] font-bold text-xs border-destructive/50 text-destructive hover:bg-destructive hover:text-white"
                                onClick={() => rejectMutation.mutate()}
                                disabled={isPending}
                            >
                                Confirm Reject
                            </Button>
                            <Button
                                variant="ghost"
                                size="sm"
                                className="uppercase tracking-[0.18em] font-bold text-xs"
                                onClick={() => { setShowRejectForm(false); setRejectReason(''); }}
                                disabled={isPending}
                            >
                                Cancel
                            </Button>
                        </div>
                    </div>
                ) : (
                    <div className="flex gap-3">
                        <Button
                            size="sm"
                            className="uppercase tracking-[0.18em] font-bold text-xs bg-amber-500 text-black hover:bg-amber-400"
                            onClick={() => approveMutation.mutate()}
                            disabled={isPending}
                        >
                            <ShieldCheck className="w-4 h-4 mr-2" /> Approve
                        </Button>
                        <Button
                            variant="outline"
                            size="sm"
                            className="uppercase tracking-[0.18em] font-bold text-xs border-destructive/50 text-destructive hover:bg-destructive hover:text-white"
                            onClick={() => setShowRejectForm(true)}
                            disabled={isPending}
                        >
                            <X className="w-4 h-4 mr-2" /> Reject
                        </Button>
                    </div>
                )}
            </CardContent>
        </Card>
    );
}
