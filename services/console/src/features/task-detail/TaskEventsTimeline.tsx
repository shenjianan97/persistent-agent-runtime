import { TaskEventResponse, TaskEventType } from '@/types';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
    Play, UserCheck, RotateCcw, Clock, Skull, RefreshCw,
    CheckCircle2, Pause, PlayCircle, ShieldCheck, ShieldX,
    MessageSquare, MessageCircle, Ban, ArrowRight, Ghost,
} from 'lucide-react';

interface TaskEventsTimelineProps {
    events: TaskEventResponse[];
    isLoading: boolean;
}

interface EventStyle {
    label: string;
    colorClass: string;
    bgClass: string;
    borderClass: string;
    icon: typeof Play;
}

const EVENT_STYLES: Record<TaskEventType, EventStyle> = {
    task_submitted:                 { label: 'Task Submitted',          colorClass: 'text-blue-400',    bgClass: 'bg-blue-500',    borderClass: 'border-blue-500/30 bg-blue-500/5',    icon: Play },
    task_claimed:                   { label: 'Task Claimed',            colorClass: 'text-blue-400',    bgClass: 'bg-blue-500',    borderClass: 'border-blue-500/30 bg-blue-500/5',    icon: UserCheck },
    task_retry_scheduled:           { label: 'Retry Scheduled',         colorClass: 'text-amber-400',   bgClass: 'bg-amber-500',   borderClass: 'border-amber-500/30 bg-amber-500/5',  icon: RotateCcw },
    task_reclaimed_after_lease_expiry: { label: 'Reclaimed (Lease Expired)', colorClass: 'text-amber-400', bgClass: 'bg-amber-500', borderClass: 'border-amber-500/30 bg-amber-500/5', icon: Clock },
    task_dead_lettered:             { label: 'Dead Lettered',           colorClass: 'text-red-400',     bgClass: 'bg-red-500',     borderClass: 'border-red-500/30 bg-red-500/5',      icon: Skull },
    task_redriven:                  { label: 'Task Redriven',           colorClass: 'text-blue-400',    bgClass: 'bg-blue-500',    borderClass: 'border-blue-500/30 bg-blue-500/5',    icon: RefreshCw },
    task_completed:                 { label: 'Task Completed',          colorClass: 'text-green-400',   bgClass: 'bg-green-500',   borderClass: 'border-green-500/30 bg-green-500/5',  icon: CheckCircle2 },
    task_paused:                    { label: 'Task Paused',             colorClass: 'text-amber-400',   bgClass: 'bg-amber-500',   borderClass: 'border-amber-500/30 bg-amber-500/5',  icon: Pause },
    task_resumed:                   { label: 'Task Resumed',            colorClass: 'text-green-400',   bgClass: 'bg-green-500',   borderClass: 'border-green-500/30 bg-green-500/5',  icon: PlayCircle },
    task_approval_requested:        { label: 'Approval Requested',      colorClass: 'text-amber-400',   bgClass: 'bg-amber-500',   borderClass: 'border-amber-500/30 bg-amber-500/5',  icon: ShieldCheck },
    task_approved:                  { label: 'Approved',                colorClass: 'text-green-400',   bgClass: 'bg-green-500',   borderClass: 'border-green-500/30 bg-green-500/5',  icon: ShieldCheck },
    task_rejected:                  { label: 'Rejected',                colorClass: 'text-red-400',     bgClass: 'bg-red-500',     borderClass: 'border-red-500/30 bg-red-500/5',      icon: ShieldX },
    task_input_requested:           { label: 'Input Requested',         colorClass: 'text-amber-400',   bgClass: 'bg-amber-500',   borderClass: 'border-amber-500/30 bg-amber-500/5',  icon: MessageSquare },
    task_input_received:            { label: 'Input Received',          colorClass: 'text-green-400',   bgClass: 'bg-green-500',   borderClass: 'border-green-500/30 bg-green-500/5',  icon: MessageCircle },
    task_cancelled:                 { label: 'Task Cancelled',          colorClass: 'text-red-400',     bgClass: 'bg-red-500',     borderClass: 'border-red-500/30 bg-red-500/5',      icon: Ban },
};

function getEventStyle(eventType: TaskEventType): EventStyle {
    return EVENT_STYLES[eventType] ?? {
        label: eventType.replace(/^task_/, '').replace(/_/g, ' '),
        colorClass: 'text-muted-foreground',
        bgClass: 'bg-muted',
        borderClass: 'border-border/30 bg-white/5',
        icon: Play,
    };
}

function formatRelativeTime(dateStr: string): string {
    const diff = Date.now() - new Date(dateStr).getTime();
    const seconds = Math.floor(diff / 1000);
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
}

export function TaskEventsTimeline({ events, isLoading }: TaskEventsTimelineProps) {
    return (
        <Card className="console-surface border-white/10 flex flex-col h-[400px]">
            <CardHeader className="border-b border-white/8 shrink-0">
                <CardTitle className="text-sm font-display uppercase tracking-widest text-muted-foreground">
                    Lifecycle Events
                </CardTitle>
            </CardHeader>

            <ScrollArea className="flex-1">
                <div className="p-6">
                    {isLoading ? (
                        <div className="h-full flex items-center justify-center pt-16">
                            <span className="text-muted-foreground text-sm tracking-widest uppercase animate-pulse">
                                Loading events...
                            </span>
                        </div>
                    ) : events.length === 0 ? (
                        <div className="h-full flex flex-col items-center justify-center pt-16 gap-2">
                            <Ghost className="w-8 h-8 opacity-20" />
                            <span className="text-muted-foreground text-sm tracking-widest uppercase">
                                No lifecycle events recorded
                            </span>
                        </div>
                    ) : (
                        <div className="relative border-l border-border/40 ml-4 space-y-4 pl-6 pb-4">
                            {events.map((event) => {
                                const style = getEventStyle(event.event_type);
                                const EventIcon = style.icon;
                                return (
                                    <div key={event.event_id} className="relative animate-in slide-in-from-left-4 fade-in duration-300">
                                        <div className={`absolute -left-[33px] top-1 h-5 w-5 rounded-full border-2 border-background ${style.bgClass} flex items-center justify-center`}>
                                            <EventIcon className="w-2.5 h-2.5 text-black" />
                                        </div>

                                        <div className={`border ${style.borderClass} p-3 space-y-2`}>
                                            <div className="flex items-start justify-between gap-3">
                                                <div className="flex flex-wrap items-center gap-2">
                                                    <span className={`text-xs font-bold tracking-wider uppercase ${style.colorClass}`}>
                                                        {style.label}
                                                    </span>
                                                    {event.status_before && event.status_after && (
                                                        <span className="flex items-center gap-1 text-[10px] font-mono text-muted-foreground">
                                                            {event.status_before}
                                                            <ArrowRight className="w-2.5 h-2.5" />
                                                            {event.status_after}
                                                        </span>
                                                    )}
                                                </div>
                                                <span className="shrink-0 text-[10px] text-muted-foreground tabular-nums" title={new Date(event.created_at).toLocaleString()}>
                                                    {formatRelativeTime(event.created_at)}
                                                </span>
                                            </div>

                                            {event.worker_id && (
                                                <div className="text-[10px] font-mono text-muted-foreground truncate">
                                                    Worker: {event.worker_id}
                                                </div>
                                            )}

                                            {(event.error_code || event.error_message) && (
                                                <div className="text-xs font-mono text-red-400 bg-black/40 border border-red-500/20 p-2">
                                                    {event.error_code && (
                                                        <span className="uppercase tracking-wider font-bold">[{event.error_code}] </span>
                                                    )}
                                                    {event.error_message}
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    )}
                </div>
            </ScrollArea>
        </Card>
    );
}
