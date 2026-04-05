import { useNavigate, useParams, Link } from 'react-router';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTaskStatus, useCancelTask } from './useTaskStatus';
import { useTaskObservability } from './useTaskObservability';
import { useCheckpoints } from './useCheckpoints';
import { useRedriveTask } from '@/features/dead-letter/useDeadLetter';
import { useLangfuseEndpoints } from '@/features/settings/useLangfuseEndpoints';
import { TaskStatusBadge } from './TaskStatusBadge';
import { CostSummary } from './CostSummary';
import { CheckpointTimeline } from './CheckpointTimeline';
import { ApprovalPanel } from './ApprovalPanel';
import { InputResponsePanel } from './InputResponsePanel';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { AlertCircle, Terminal, Ban, RotateCcw, PlayCircle } from 'lucide-react';
import { toast } from 'sonner';
import { api, ApiError } from '@/api/client';
import { CheckpointResponse } from '@/types';
import { formatUsd } from '@/lib/utils';

export function TaskDetailPage() {
    const { taskId } = useParams<{ taskId: string }>();
    const navigate = useNavigate();
    const { data: task, isLoading, isError } = useTaskStatus(taskId!);
    const { data: observability } = useTaskObservability(taskId!, task?.status);
    const { data: checkpointsData } = useCheckpoints(taskId!, task?.status, task?.checkpoint_count);

    const { data: langfuseEndpoints = [] } = useLangfuseEndpoints();
    const langfuseEndpoint = task?.langfuse_endpoint_id
        ? langfuseEndpoints.find(ep => ep.endpoint_id === task.langfuse_endpoint_id)
        : null;

    const queryClient = useQueryClient();
    const cancelMutation = useCancelTask();
    const redriveMutation = useRedriveTask();

    const resumeMutation = useMutation({
        mutationFn: (id: string) => api.resumeTask(id),
        onSuccess: (_, id) => {
            queryClient.invalidateQueries({ queryKey: ['task', id] });
            queryClient.invalidateQueries({ queryKey: ['task-events', id] });
            toast.success('Task resumed');
        },
        onError: (error: ApiError) => {
            toast.error(error.message || 'Resume failed');
        },
    });

    const isNonTerminal = task?.status === 'queued' || task?.status === 'running' ||
        task?.status === 'waiting_for_approval' || task?.status === 'waiting_for_input' || task?.status === 'paused';

    const { data: eventsData } = useQuery({
        queryKey: ['task-events', taskId],
        queryFn: () => api.getTaskEvents(taskId!),
        refetchInterval: isNonTerminal ? 5000 : false,
        enabled: !!taskId,
    });

    const handleActionComplete = () => {
        queryClient.invalidateQueries({ queryKey: ['task', taskId] });
        queryClient.invalidateQueries({ queryKey: ['task-events', taskId] });
    };

    if (isLoading) {
        return (
            <div className="flex items-center justify-center p-20 animate-pulse text-muted-foreground uppercase tracking-widest font-bold">
                Loading task data...
            </div>
        );
    }

    if (isError || !task) {
        return (
            <div className="p-8 border border-destructive/50 bg-destructive/10 text-destructive text-sm uppercase tracking-widest">
                Error loading task ID: {taskId}
            </div>
        );
    }

    const checkpoints: CheckpointResponse[] = checkpointsData?.checkpoints || [];
    const isRunning = task.status === 'running' || task.status === 'queued';
    const isWaitingForApproval = task.status === 'waiting_for_approval';
    const isWaitingForInput = task.status === 'waiting_for_input';
    const isCancellable = isRunning || isWaitingForApproval || isWaitingForInput || task.status === 'paused';
    const isDeadLetter = task.status === 'dead_letter';

    const parsedOutput = (() => {
        if (!task.output) return null;
        try {
            const obj = typeof task.output === 'string' ? JSON.parse(task.output) : task.output;
            return typeof obj === 'object' ? obj : null;
        } catch { return null; }
    })();
    const langfuseStatus = parsedOutput?.langfuse_status as string | undefined;

    // Filter internal metadata from the output for display
    const displayOutput = (() => {
        if (!parsedOutput) return task.output;
        const { langfuse_status: _, ...rest } = parsedOutput as Record<string, unknown>;
        return Object.keys(rest).length > 0 ? rest : task.output;
    })();

    const formatJson = (value?: unknown) => {
        if (!value) return '';
        if (typeof value === 'string') {
            try {
                return JSON.stringify(JSON.parse(value), null, 2);
            } catch {
                return value;
            }
        }
        return JSON.stringify(value, null, 2);
    };

    const handleCancel = () => {
        cancelMutation.mutate(task.task_id, {
            onSuccess: () => toast.success("Task cancellation requested"),
            onError: (err: Error) => toast.error(err.message || "Failed to cancel task"),
        });
    };

    const handleRedrive = () => {
        redriveMutation.mutate(task.task_id, {
            onSuccess: (response) => {
                toast.success("Task redriven successfully");
                if (response.task_id !== task.task_id) {
                    navigate(`/tasks/${response.task_id}`);
                }
            },
            onError: (err: Error) => toast.error(err.message || "Failed to redrive task"),
        });
    };

    return (
        <div className="space-y-6 animate-in fade-in duration-500 pb-20">
            {/* Header Panel */}
            <div className="console-surface-strong rounded-[28px] p-6 md:p-7 flex flex-col md:flex-row md:items-start justify-between gap-4">
                <div className="space-y-2">
                    <div className="flex items-center gap-3">
                        <h2 className="text-2xl font-display font-semibold tracking-tight">
                            {task.task_id}
                        </h2>
                        <TaskStatusBadge status={task.status} pauseReason={task.pause_reason} />
                    </div>
                    <div className="flex flex-wrap gap-4 text-xs font-mono text-muted-foreground uppercase tracking-[0.18em]">
                        <span>Agent: <Link to={`/agents/${encodeURIComponent(task.agent_id)}`} className="text-foreground hover:text-primary hover:underline underline-offset-4 decoration-primary/50 transition-colors">{task.agent_display_name ? `${task.agent_display_name} (${task.agent_id})` : task.agent_id}</Link></span>
                        <span>Retries: <span className="text-foreground">{task.retry_count}</span></span>
                        <span>Created: {new Date(task.created_at).toLocaleString()}</span>
                    </div>
                </div>

                <div className="flex gap-3">
                    {isCancellable && (
                        <Button
                            variant="outline"
                            className="uppercase tracking-[0.18em] font-bold text-xs h-9"
                            onClick={handleCancel}
                            disabled={cancelMutation.isPending}
                        >
                            <Ban className="w-4 h-4 mr-2" /> Cancel
                        </Button>
                    )}
                    {isDeadLetter && (
                        <Button
                            variant="outline"
                            className="uppercase tracking-[0.18em] font-bold text-xs h-9"
                            onClick={handleRedrive}
                            disabled={redriveMutation.isPending}
                        >
                            <RotateCcw className="w-4 h-4 mr-2" /> Redrive Task
                        </Button>
                    )}
                </div>
            </div>

            <div className="space-y-6">
                    <CostSummary
                        observability={observability}
                        checkpointCount={task.checkpoint_count}
                        totalCostMicrodollars={task.total_cost_microdollars}
                        task={task}
                    />

                    {(langfuseStatus || task.langfuse_endpoint_id) && (
                        <div className={`flex items-center justify-between gap-4 px-4 py-3 rounded-lg text-sm font-mono ${
                            langfuseStatus === 'sent' ? 'bg-success/10 text-success border border-success/20' :
                            langfuseStatus === 'failed' ? 'bg-destructive/10 text-destructive border border-destructive/20' :
                            'bg-muted/10 text-muted-foreground border border-border/20'
                        }`}>
                            <span className="uppercase tracking-widest">
                                {langfuseStatus === 'sent' ? 'Traces sent to Langfuse' :
                                 langfuseStatus === 'failed' ? 'Langfuse trace push failed' :
                                 'Langfuse endpoint configured'}
                            </span>
                            {langfuseEndpoint && (
                                <span className="text-xs opacity-70">
                                    {langfuseEndpoint.name} — {langfuseEndpoint.host}
                                </span>
                            )}
                        </div>
                    )}

                    {isWaitingForApproval && (
                        <ApprovalPanel task={task} onActionComplete={handleActionComplete} />
                    )}

                    {isWaitingForInput && (
                        <InputResponsePanel task={task} onActionComplete={handleActionComplete} />
                    )}

                    {task.status === 'paused' && task.pause_reason && (
                        <div className="console-surface rounded-[24px] p-6 space-y-4 relative overflow-hidden border border-amber-500/30 bg-amber-500/5">
                            <div className="absolute top-0 left-0 w-1 h-full bg-amber-500" />
                            <h3 className="text-amber-400 font-bold uppercase tracking-widest flex items-center gap-2 text-sm">
                                <AlertCircle className="w-4 h-4" /> Budget Pause
                            </h3>
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm font-mono">
                                <div>
                                    <span className="text-muted-foreground block text-xs uppercase mb-1">Pause Reason</span>
                                    <span className="text-amber-400 font-bold">
                                        {task.pause_reason === 'budget_per_task' ? 'Per-Task Budget Exceeded' : 'Hourly Budget Exceeded'}
                                    </span>
                                </div>
                                {task.pause_details && (
                                    <>
                                        <div>
                                            <span className="text-muted-foreground block text-xs uppercase mb-1">Budget Limit</span>
                                            <span className="text-foreground">
                                                ${formatUsd(task.pause_details.budget_max_per_task ?? task.pause_details.budget_max_per_hour ?? 0)}
                                            </span>
                                        </div>
                                        <div>
                                            <span className="text-muted-foreground block text-xs uppercase mb-1">Observed Cost</span>
                                            <span className="text-foreground">
                                                ${formatUsd(task.pause_details.observed_task_cost_microdollars ?? task.pause_details.observed_hour_cost_microdollars ?? 0)}
                                            </span>
                                        </div>
                                        <div>
                                            <span className="text-muted-foreground block text-xs uppercase mb-1">Recovery</span>
                                            <span className="text-foreground">
                                                {task.pause_details.recovery_mode === 'automatic_after_window_clears'
                                                    ? `Auto-recovers${task.resume_eligible_at ? ` at ${new Date(task.resume_eligible_at).toLocaleString()}` : ''}`
                                                    : 'Requires budget increase + manual resume'}
                                            </span>
                                        </div>
                                    </>
                                )}
                            </div>
                            {task.pause_reason === 'budget_per_task' && (
                                <div className="pt-2">
                                    <Button
                                        onClick={() => resumeMutation.mutate(task.task_id)}
                                        disabled={resumeMutation.isPending}
                                        className="font-bold uppercase tracking-widest px-6 hover:saturate-150 transition-all"
                                    >
                                        <PlayCircle className="w-4 h-4 mr-2" />
                                        {resumeMutation.isPending ? 'Resuming...' : 'Resume Task'}
                                    </Button>
                                </div>
                            )}
                        </div>
                    )}

                    <CheckpointTimeline
                        checkpoints={checkpoints}
                        hitlEvents={eventsData?.events ?? []}
                        isRunning={isRunning}
                        retryHistory={task.retry_history}
                        status={task.status}
                        deadLetterReason={task.dead_letter_reason}
                        lastErrorCode={task.last_error_code}
                        lastErrorMessage={task.last_error_message}
                        deadLetteredAt={task.dead_lettered_at}
                    />

                    {isDeadLetter && (
                        <div className="console-danger-surface rounded-[24px] p-6 space-y-3 relative overflow-hidden">
                            <div className="absolute top-0 left-0 w-1 h-full bg-destructive" />
                            <h3 className="text-destructive font-bold uppercase tracking-widest flex items-center gap-2">
                                <AlertCircle className="w-4 h-4" /> Execution Failure
                            </h3>
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm font-mono mt-4">
                                <div>
                                    <span className="text-muted-foreground block text-xs uppercase mb-1">Reason</span>
                                    <span className="text-destructive font-bold">{task.dead_letter_reason}</span>
                                </div>
                                <div>
                                    <span className="text-muted-foreground block text-xs uppercase mb-1">Failed At</span>
                                    <span>{task.dead_lettered_at ? new Date(task.dead_lettered_at).toLocaleString() : 'Unknown'}</span>
                                </div>
                            </div>
                            <div className="bg-black/50 border border-destructive/30 p-4 mt-4 text-xs font-mono overflow-auto max-h-[150px]">
                                <span className="text-muted-foreground block mb-2 uppercase border-b border-border/40 pb-2">Error Message [{task.last_error_code || 'UNKNOWN'}]</span>
                                <span className="text-red-400 break-all">{task.last_error_message}</span>
                            </div>
                        </div>
                    )}

                    <div className={`grid gap-6 ${task.status === 'completed' && task.output ? 'grid-cols-1 md:grid-cols-2' : 'grid-cols-1'}`}>
                        <Card className="console-surface border-white/10 flex flex-col max-h-[300px]">
                            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2 border-b border-white/8 shrink-0">
                                <CardTitle className="text-sm font-display uppercase tracking-widest flex items-center gap-2 text-muted-foreground">
                                    <Terminal className="w-4 h-4" /> Input
                                </CardTitle>
                            </CardHeader>
                            <CardContent className="pt-4 flex-1 overflow-auto">
                                <pre className="text-xs font-mono text-muted-foreground whitespace-pre-wrap">
                                    {formatJson(task.input)}
                                </pre>
                            </CardContent>
                        </Card>

                        {task.status === 'completed' && !!task.output && (
                            <Card className="console-surface border-success/30 flex flex-col max-h-[300px]">
                                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2 border-b border-success/30 shrink-0 bg-success/5">
                                    <CardTitle className="text-sm font-display uppercase tracking-widest flex items-center gap-2 text-success">
                                        <Terminal className="w-4 h-4" /> Output
                                    </CardTitle>
                                </CardHeader>
                                <CardContent className="pt-4 flex-1 overflow-auto">
                                    <pre className="text-xs font-mono text-success whitespace-pre-wrap">
                                        {formatJson(displayOutput)}
                                    </pre>
                                </CardContent>
                            </Card>
                        )}
                    </div>

            </div>
        </div>
    );
}
