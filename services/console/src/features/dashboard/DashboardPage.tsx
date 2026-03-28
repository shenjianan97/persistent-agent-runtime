import { Link } from 'react-router';
import { AlertCircle, ArrowRight, Clock3, ListChecks, PlaySquare, ReceiptText, Zap } from 'lucide-react';

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
        <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-sm font-medium tracking-wide text-muted-foreground uppercase">{title}</CardTitle>
                <Icon className="h-4 w-4 text-primary" />
            </CardHeader>
            <CardContent>
                <div className="text-2xl font-bold uppercase tracking-widest text-foreground">{value}</div>
                <p className="mt-1 text-[10px] uppercase tracking-wider text-muted-foreground">{subtitle}</p>
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
                <h3 className="text-sm font-display uppercase tracking-widest text-foreground">{title}</h3>
                <p className="mt-1 text-xs text-muted-foreground">{description}</p>
            </div>
            {actionLabel && actionTo && (
                <Link
                    to={actionTo}
                    className="inline-flex items-center gap-2 text-xs uppercase tracking-widest text-primary hover:text-primary/80"
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

    if (isError) {
        return (
            <div className="border border-destructive/50 bg-destructive/10 p-6 text-sm uppercase tracking-widest text-destructive">
                Unable to load the home dashboard.
            </div>
        );
    }

    return (
        <div className="space-y-8 animate-in fade-in duration-500">
            <div className="flex flex-col gap-4 border border-border/40 bg-black/40 p-6 backdrop-blur md:flex-row md:items-end md:justify-between">
                <div className="space-y-2">
                    <h2 className="text-2xl font-display font-medium uppercase tracking-wider">Home</h2>
                    <p className="max-w-2xl text-muted-foreground">
                        Track what needs attention, what is currently running, and what completed most recently.
                    </p>
                </div>
                <div className="flex flex-wrap gap-3">
                    <Button
                        asChild
                        className="rounded-none uppercase tracking-widest text-xs font-bold"
                    >
                        <Link to="/tasks/new">
                            <PlaySquare className="mr-2 h-4 w-4" />
                            Submit Task
                        </Link>
                    </Button>
                    <Button
                        asChild
                        variant="outline"
                        className="rounded-none border-border uppercase tracking-widest text-xs font-bold"
                    >
                        <Link to="/tasks">
                            View All Tasks
                        </Link>
                    </Button>
                </div>
            </div>

            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                <SummaryCard
                    title="In Progress"
                    value={String(summary.inProgressCount)}
                    subtitle="Queued or running right now"
                    icon={Clock3}
                />
                <SummaryCard
                    title="Dead Letters"
                    value={String(summary.deadLetterCount)}
                    subtitle="Runs needing review or redrive"
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

            <div className="grid gap-6 xl:grid-cols-[1.1fr_1fr]">
                <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
                    <CardHeader className="border-b border-border/40">
                        <SectionHeader
                            title="Needs Attention"
                            description="Recent dead-letter runs that may need inspection or redrive."
                            actionLabel="Open Dead Letters"
                            actionTo="/dead-letter"
                        />
                    </CardHeader>
                    <CardContent className="pt-4">
                        {isLoading ? (
                            <div className="border border-dashed border-border/40 p-4 text-xs uppercase tracking-widest text-muted-foreground">
                                Loading recent issues...
                            </div>
                        ) : deadLetters.length === 0 ? (
                            <div className="border border-dashed border-border/40 p-5 text-sm text-muted-foreground">
                                No runs need attention right now.
                            </div>
                        ) : (
                            <div className="space-y-3">
                                {deadLetters.map((task) => {
                                    const timestamp = formatDateTime(task.dead_lettered_at);
                                    return (
                                        <div
                                            key={task.task_id}
                                            className="border border-destructive/30 bg-destructive/5 p-4"
                                        >
                                            <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                                                <div className="space-y-2">
                                                    <div className="flex flex-wrap items-center gap-2">
                                                        <Link
                                                            to={`/tasks/${task.task_id}`}
                                                            className="text-sm font-semibold uppercase tracking-widest text-foreground hover:text-primary"
                                                        >
                                                            {formatTaskId(task.task_id)}
                                                        </Link>
                                                        <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
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

                <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
                    <CardHeader className="border-b border-border/40">
                        <SectionHeader
                            title="In Progress"
                            description="Runs that are currently queued or executing."
                            actionLabel="Open Tasks"
                            actionTo="/tasks"
                        />
                    </CardHeader>
                    <CardContent className="pt-4">
                        {isLoading ? (
                            <div className="border border-dashed border-border/40 p-4 text-xs uppercase tracking-widest text-muted-foreground">
                                Loading active runs...
                            </div>
                        ) : inProgress.length === 0 ? (
                            <div className="border border-dashed border-border/40 p-5 text-sm text-muted-foreground">
                                No tasks are currently running.
                            </div>
                        ) : (
                            <div className="space-y-3">
                                {inProgress.map((task) => {
                                    const timestamp = formatDateTime(task.updated_at || task.created_at);
                                    return (
                                        <Link
                                            key={task.task_id}
                                            to={`/tasks/${task.task_id}`}
                                            className="block border border-border/40 bg-black/30 p-4 transition-colors hover:border-primary/40 hover:bg-white/5"
                                        >
                                            <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                                                <div className="space-y-2">
                                                    <div className="flex flex-wrap items-center gap-2">
                                                        <span className="text-sm font-semibold uppercase tracking-widest text-foreground">
                                                            {formatTaskId(task.task_id)}
                                                        </span>
                                                        <TaskStatusBadge status={task.status} className="text-[10px] px-2 py-0.5" />
                                                    </div>
                                                    <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
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

            <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
                <CardHeader className="border-b border-border/40">
                    <SectionHeader
                        title="Recent Runs"
                        description="The latest completed runs from your current task history slice."
                        actionLabel="View all tasks"
                        actionTo="/tasks"
                    />
                </CardHeader>
                <CardContent className="pt-4">
                    {isLoading ? (
                        <div className="border border-dashed border-border/40 p-4 text-xs uppercase tracking-widest text-muted-foreground">
                            Loading recent runs...
                        </div>
                    ) : recentRuns.length === 0 ? (
                        <div className="border border-dashed border-border/40 p-5 text-sm text-muted-foreground">
                            Submit your first task to start building execution history.
                        </div>
                    ) : (
                        <div className="space-y-3">
                            {recentRuns.map((task) => {
                                const timestamp = formatDateTime(task.created_at);
                                return (
                                    <Link
                                        key={task.task_id}
                                        to={`/tasks/${task.task_id}`}
                                        className="grid gap-3 border border-border/40 bg-black/30 p-4 transition-colors hover:border-primary/40 hover:bg-white/5 md:grid-cols-[1.4fr_1fr_auto_auto]"
                                    >
                                        <div className="space-y-2">
                                            <div className="flex flex-wrap items-center gap-2">
                                                <span className="text-sm font-semibold uppercase tracking-widest text-foreground">
                                                    {formatTaskId(task.task_id)}
                                                </span>
                                                <TaskStatusBadge status={task.status} className="text-[10px] px-2 py-0.5" />
                                            </div>
                                            <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                                                Agent {task.agent_id}
                                            </div>
                                        </div>
                                        <div className="text-xs text-muted-foreground">
                                            <div>{task.checkpoint_count} checkpoints</div>
                                        </div>
                                        <div className="text-xs font-medium text-success">
                                            ${formatUsd(task.total_cost_microdollars)}
                                        </div>
                                        <div className="text-right text-[10px] uppercase tracking-widest text-muted-foreground">
                                            <div>{timestamp.date}</div>
                                            <div>{timestamp.time}</div>
                                        </div>
                                    </Link>
                                );
                            })}
                        </div>
                    )}
                </CardContent>
            </Card>
        </div>
    );
}
