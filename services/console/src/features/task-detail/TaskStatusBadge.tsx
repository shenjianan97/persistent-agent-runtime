import { Badge } from '@/components/ui/badge';
import { TaskStatus } from '@/types';
import { cn } from '@/lib/utils';
import { Loader2 } from 'lucide-react';

export function TaskStatusBadge({ status, className }: { status: TaskStatus, className?: string }) {
    const label = status === 'dead_letter' ? 'failed' : status.replace('_', ' ');

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
