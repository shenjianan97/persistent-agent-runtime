import { useParams } from 'react-router';
import { useTaskStatus, useCancelTask } from './useTaskStatus';
import { useCheckpoints } from './useCheckpoints';
import { useRedriveTask } from '@/features/dead-letter/useDeadLetter';
import { TaskStatusBadge } from './TaskStatusBadge';
import { CheckpointTimeline } from './CheckpointTimeline';
import { CostSummary } from './CostSummary';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { AlertCircle, Terminal, Ban, RotateCcw } from 'lucide-react';
import { toast } from 'sonner';
import { CheckpointResponse } from '@/types';

export function TaskDetailPage() {
    const { taskId } = useParams<{ taskId: string }>();
    const { data: task, isLoading, isError } = useTaskStatus(taskId!);
    const { data: checkpointsData } = useCheckpoints(taskId!, task?.status);

    const cancelMutation = useCancelTask();
    const redriveMutation = useRedriveTask();

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
    const isDeadLetter = task.status === 'dead_letter';

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
            onSuccess: () => toast.success("Task redriven successfully"),
            onError: (err: Error) => toast.error(err.message || "Failed to redrive task"),
        });
    };

    return (
        <div className="space-y-6 animate-in fade-in duration-500 pb-20">
            {/* Header Panel */}
            <div className="flex flex-col md:flex-row md:items-start justify-between gap-4 border border-border/40 bg-black/40 backdrop-blur p-6">
                <div className="space-y-2">
                    <div className="flex items-center gap-3">
                        <h2 className="text-xl font-display font-medium uppercase tracking-wider">
                            {task.task_id}
                        </h2>
                        <TaskStatusBadge status={task.status} />
                    </div>
                    <div className="flex gap-4 text-xs font-mono text-muted-foreground uppercase tracking-widest">
                        <span>Agent: <span className="text-foreground">{task.agent_id}</span></span>
                        <span>Retries: <span className="text-foreground">{task.retry_count}</span></span>
                        <span>Created: {new Date(task.created_at).toLocaleString()}</span>
                    </div>
                </div>

                <div className="flex gap-3">
                    {isRunning && (
                        <Button
                            variant="outline"
                            className="rounded-none border-border hover:bg-destructive hover:text-white uppercase tracking-widest font-bold text-xs h-9"
                            onClick={handleCancel}
                            disabled={cancelMutation.isPending}
                        >
                            <Ban className="w-4 h-4 mr-2" /> Cancel
                        </Button>
                    )}
                    {isDeadLetter && (
                        <Button
                            variant="outline"
                            className="rounded-none border-primary text-primary hover:bg-primary hover:text-black uppercase tracking-widest font-bold text-xs h-9"
                            onClick={handleRedrive}
                            disabled={redriveMutation.isPending}
                        >
                            <RotateCcw className="w-4 h-4 mr-2" /> Redrive Task
                        </Button>
                    )}
                </div>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                {/* Left Column: Cost Summary and I/O */}
                <div className="lg:col-span-2 space-y-6">
                    <CostSummary
                        checkpoints={checkpoints}
                        totalCostMicrodollars={task.total_cost_microdollars}
                    />

                    {isDeadLetter && (
                        <div className="border border-destructive/50 bg-destructive/10 p-6 space-y-3 relative overflow-hidden">
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

                    <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none h-[400px] flex flex-col">
                        <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2 border-b border-border/40 shrink-0">
                            <CardTitle className="text-sm font-display uppercase tracking-widest flex items-center gap-2 text-muted-foreground">
                                <Terminal className="w-4 h-4" /> Input Payload
                            </CardTitle>
                        </CardHeader>
                        <CardContent className="pt-4 flex-1 h-0 overflow-auto">
                            <pre className="text-xs font-mono text-muted-foreground whitespace-pre-wrap">
                                {formatJson(task.input)}
                            </pre>
                        </CardContent>
                    </Card>

                    {task.status === 'completed' && !!task.output && (
                        <Card className="rounded-none border-success/40 bg-black/40 backdrop-blur shadow-none h-[400px] flex flex-col">
                            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2 border-b border-success/40 shrink-0 bg-success/5">
                                <CardTitle className="text-sm font-display uppercase tracking-widest flex items-center gap-2 text-success">
                                    <Terminal className="w-4 h-4" /> Execution Result
                                </CardTitle>
                            </CardHeader>
                            <CardContent className="pt-4 flex-1 h-0 overflow-auto">
                                <pre className="text-xs font-mono text-success whitespace-pre-wrap">
                                    {formatJson(task.output)}
                                </pre>
                            </CardContent>
                        </Card>
                    )}
                </div>

                {/* Right Column: Timeline */}
                <div className="lg:col-span-1">
                    <CheckpointTimeline checkpoints={checkpoints} isRunning={isRunning} />
                </div>
            </div>
        </div>
    );
}
