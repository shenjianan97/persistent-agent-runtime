import { Link } from 'react-router';
import { AlertCircle, ArrowRight, Clock3, ListChecks, PlaySquare, ReceiptText, UserCheck, Zap } from 'lucide-react';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { TaskStatusBadge } from '@/features/task-detail/TaskStatusBadge';
import { formatUsd } from '@/lib/utils';
import { useDashboardOverview } from './useDashboardOverview';

function formatTaskId(taskId: string) {
    return `${taskId.slice(0, 8)}...${taskId.slice(-4)}`;
}

function formatDateTime(value: string) {
    const date = new Date(value);
    return {
        date: date.toLocaleDateString(),
        time: date.toLocaleTimeString(),
    };
}

function SummaryCard({
    title,
    value,
    subtitle,
    icon: Icon,
}: {
    title: string;
    value: string;
    subtitle: string;
    icon: typeof Zap;
}) {
    return (
        <Card className="console-surface overflow-hidden border-white/10">
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-[10px] font-medium tracking-[0.24em] text-muted-foreground uppercase">{title}</CardTitle>
                <Icon className="h-4 w-4 text-primary/80 drop-shadow-[0_0_10px_var(--color-primary)]" />
            </CardHeader>
            <CardContent className="space-y-3">
                <div className="text-3xl font-semibold tracking-[-0.02em] tabular-nums text-foreground">{value}</div>
                <div className="border-t border-white/8 pt-3">
                    <p className="text-sm leading-6 text-muted-foreground">{subtitle}</p>
                </div>
            </CardContent>
        </Card>
    );
}

function SectionHeader({
    title,
    description,
    actionLabel,
    actionTo,
}: {
    title: string;
    description: string;
    actionLabel?: string;
    actionTo?: string;
}) {
    return (
        <div className="flex items-center justify-between gap-4">
            <div>
                <h3 className="text-sm font-semibold uppercase tracking-[0.2em] text-foreground">{title}</h3>
                <p className="mt-2 max-w-xl text-sm leading-6 text-muted-foreground">{description}</p>
            </div>
            {actionLabel && actionTo && (
                <Link
                    to={actionTo}
                    className="inline-flex items-center gap-2 rounded-full border border-primary/15 bg-primary/8 px-3 py-2 text-xs uppercase tracking-[0.2em] text-primary hover:text-primary/80"
                >
                    {actionLabel}
                    <ArrowRight className="h-3 w-3" />
                </Link>
            )}
        </div>
    );
}

export function DashboardPage() {
    const { isLoading, isError, deadLetters, inProgress, recentRuns, summary } = useDashboardOverview();
    const activityItems = [...inProgress, ...recentRuns]
        .sort((left, right) => {
            const leftTime = new Date(left.updated_at || left.created_at).getTime();
            const rightTime = new Date(right.updated_at || right.created_at).getTime();
            return rightTime - leftTime;
        })
        .slice(0, 6);

    if (isError) {
        return (
            <div className="console-danger-surface rounded-3xl p-6 text-sm uppercase tracking-[0.2em] text-destructive">
                Unable to load the home dashboard.
            </div>
        );
    }

    return (
        <div className="space-y-8 animate-in fade-in duration-500">
            <div className="console-surface-strong flex flex-col gap-4 rounded-[28px] p-6 md:flex-row md:items-end md:justify-between md:p-8">
                <div className="space-y-2">
                    <div className="text-[11px] font-semibold uppercase tracking-[0.26em] text-primary">Customer Home</div>
                    <h2 className="text-3xl font-semibold tracking-tight text-foreground">Home</h2>
                    <p className="max-w-2xl text-sm text-muted-foreground md:text-base">
                        Track what needs attention first, then review recent agent activity and completed work.
                    </p>
                </div>
                <div className="flex flex-wrap gap-3">
                    <Button
                        asChild
                        className="uppercase tracking-[0.18em] text-xs font-bold"
                    >
                        <Link to="/tasks/new">
                            <PlaySquare className="mr-2 h-4 w-4" />
                            Submit Task
                        </Link>
                    </Button>
                    <Button
                        asChild
                        variant="outline"
                        className="uppercase tracking-[0.18em] text-xs font-bold"
                    >
                        <Link to="/tasks">
                            View All Tasks
                        </Link>
                    </Button>
                </div>
            </div>

            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
                <SummaryCard
                    title="Queued + Running"
                    value={String(summary.inProgressCount)}
                    subtitle="Active tasks are shown as a quick signal, not as a separate empty panel."
                    icon={Clock3}
                />
                <SummaryCard
                    title="Pending Actions"
                    value={String(summary.pendingActionCount)}
                    subtitle="Tasks awaiting human approval or input"
                    icon={UserCheck}
                />
                <SummaryCard
                    title="Failed"
                    value={String(summary.deadLetterCount)}
                    subtitle="Tasks needing review or redrive"
                    icon={AlertCircle}
                />
                <SummaryCard
                    title="Completed Recently"
                    value={String(summary.completedCount)}
                    subtitle="Finished runs in the recent activity slice"
                    icon={ListChecks}
                />
                <SummaryCard
                    title="Recent Cost"
                    value={`$${formatUsd(summary.recentCostMicrodollars)}`}
                    subtitle="Observed cost across recent completed runs"
                    icon={ReceiptText}
                />
            </div>

            <div className="grid gap-6 xl:grid-cols-[1.15fr_1fr]">
                <Card className="console-surface border-white/10">
                    <CardHeader className="border-b border-white/8">
                        <SectionHeader
                            title="Needs Attention"
                            description="Recent failed tasks that may need inspection or redrive."
                            actionLabel="Open Failed"
                            actionTo="/dead-letter"
                        />
                    </CardHeader>
                    <CardContent className="pt-5 max-h-[420px] overflow-auto">
                        {isLoading ? (
                            <div className="border-t border-white/8 pt-4 text-xs uppercase tracking-[0.2em] text-muted-foreground">
                                Loading recent issues...
                            </div>
                        ) : deadLetters.length === 0 ? (
                            <div className="border-t border-white/8 pt-4 text-sm text-muted-foreground">
                                No runs need attention right now.
                            </div>
                        ) : (
                            <div className="space-y-3">
                                {deadLetters.map((task) => {
                                    const timestamp = formatDateTime(task.dead_lettered_at);
                                    return (
                                        <div
                                            key={task.task_id}
                                            className="console-danger-surface rounded-2xl p-4"
                                        >
                                            <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                                                <div className="space-y-2">
                                                    <div className="flex flex-wrap items-center gap-2">
                                                        <Link
                                                            to={`/tasks/${task.task_id}`}
                                                            className="text-sm font-semibold uppercase tracking-[0.18em] text-foreground hover:text-primary"
                                                        >
                                                            {formatTaskId(task.task_id)}
                                                        </Link>
                                                        <span className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                                                            Agent {task.agent_id}
                                                        </span>
                                                    </div>
                                                    <div className="text-sm font-medium text-destructive">
                                                        {task.dead_letter_reason}
                                                    </div>
                                                    <div className="text-xs text-muted-foreground">
                                                        {task.last_error_message || 'No extended error detail provided.'}
                                                    </div>
                                                </div>
                                                <div className="text-right text-[10px] uppercase tracking-widest text-muted-foreground">
                                                    <div>{timestamp.date}</div>
                                                    <div>{timestamp.time}</div>
                                                </div>
                                            </div>
                                        </div>
                                    );
                                })}
                            </div>
                        )}
                    </CardContent>
                </Card>

                <Card className="console-surface border-white/10">
                    <CardHeader className="border-b border-white/8">
                        <SectionHeader
                            title="Recent Runs"
                            description="Recent history plus any currently active tasks, without a separate empty running panel."
                            actionLabel="View All Tasks"
                            actionTo="/tasks"
                        />
                    </CardHeader>
                    <CardContent className="pt-5 max-h-[420px] overflow-auto">
                        {isLoading ? (
                            <div className="border-t border-white/8 pt-4 text-xs uppercase tracking-[0.2em] text-muted-foreground">
                                Loading activity...
                            </div>
                        ) : activityItems.length === 0 ? (
                            <div className="border-t border-white/8 pt-4 text-sm text-muted-foreground">
                                Submit your first task to start building execution history.
                            </div>
                        ) : (
                            <div className="space-y-3">
                                {activityItems.map((task) => {
                                    const timestamp = formatDateTime(task.updated_at || task.created_at);
                                    return (
                                        <Link
                                            key={task.task_id}
                                            to={`/tasks/${task.task_id}`}
                                            className="block rounded-2xl border border-white/8 bg-white/[0.03] p-4 transition-colors hover:border-primary/30 hover:bg-white/[0.06]"
                                        >
                                            <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                                                <div className="space-y-2">
                                                    <div className="flex flex-wrap items-center gap-2">
                                                        <span className="text-sm font-semibold uppercase tracking-[0.18em] text-foreground">
                                                            {formatTaskId(task.task_id)}
                                                        </span>
                                                        <TaskStatusBadge status={task.status} className="text-[10px] px-2 py-0.5" />
                                                    </div>
                                                    <div className="text-[10px] uppercase tracking-[0.18em] text-muted-foreground">
                                                        Agent {task.agent_id}
                                                    </div>
                                                    <div className="flex flex-wrap gap-4 text-xs text-muted-foreground">
                                                        <span>{task.checkpoint_count} checkpoints</span>
                                                        <span>${formatUsd(task.total_cost_microdollars)}</span>
                                                    </div>
                                                </div>
                                                <div className="text-right text-[10px] uppercase tracking-widest text-muted-foreground">
                                                    <div>{timestamp.date}</div>
                                                    <div>{timestamp.time}</div>
                                                </div>
                                            </div>
                                        </Link>
                                    );
                                })}
                            </div>
                        )}
                    </CardContent>
                </Card>
            </div>
        </div>
    );
}
