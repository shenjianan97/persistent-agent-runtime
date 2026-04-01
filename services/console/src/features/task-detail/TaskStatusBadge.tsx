import { Badge } from '@/components/ui/badge';
import { TaskStatus } from '@/types';
import { cn } from '@/lib/utils';
import { Loader2 } from 'lucide-react';

const STATUS_LABELS: Record<string, string> = {
    dead_letter: 'failed',
    waiting_for_approval: 'Awaiting Approval',
    waiting_for_input: 'Awaiting Input',
    paused: 'Paused',
};

export function TaskStatusBadge({ status, className }: { status: TaskStatus, className?: string }) {
    const label = STATUS_LABELS[status] ?? status.replace('_', ' ');

    const getBadgeStyle = (status: TaskStatus) => {
        switch (status) {
            case 'queued':
                return 'bg-warning/10 text-warning border-warning/40 shadow-[0_0_12px_var(--color-warning)]';
            case 'running':
                return 'bg-primary/10 text-primary border-primary/40 shadow-[0_0_12px_var(--color-primary)]';
            case 'completed':
                return 'bg-success/10 text-success border-success/40 shadow-[0_0_12px_var(--color-success)]';
            case 'cancelled':
                return 'bg-muted text-muted-foreground border-border';
            case 'dead_letter':
                return 'bg-destructive/10 text-destructive border-destructive/40 shadow-[0_0_12px_var(--color-destructive)]';
            case 'waiting_for_approval':
                return 'bg-amber-500/10 text-amber-400 border-amber-500/40 shadow-[0_0_12px_rgba(245,158,11,0.3)]';
            case 'waiting_for_input':
                return 'bg-blue-500/10 text-blue-400 border-blue-500/40 shadow-[0_0_12px_rgba(59,130,246,0.3)]';
            case 'paused':
                return 'bg-gray-500/10 text-gray-400 border-gray-500/40';
            default:
                return 'bg-muted text-foreground';
        }
    };

    return (
        <Badge variant="outline" className={cn("rounded-full font-bold uppercase tracking-[0.18em] px-3 py-1 flex items-center gap-2", getBadgeStyle(status), className)}>
            {status === 'running' && <Loader2 className="w-3 h-3 animate-spin" />}
            {label}
        </Badge>
    );
}
