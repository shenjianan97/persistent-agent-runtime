import { Badge } from '@/components/ui/badge';
import { TaskStatus } from '@/types';
import { cn } from '@/lib/utils';
import { Loader2 } from 'lucide-react';

export function TaskStatusBadge({ status, className }: { status: TaskStatus, className?: string }) {
    const getBadgeStyle = (status: TaskStatus) => {
        switch (status) {
            case 'queued':
                return 'bg-[#FFB000]/10 text-[#FFB000] border-[#FFB000]/50 shadow-[0_0_8px_rgba(255,176,0,0.4)]';
            case 'running':
                return 'bg-[#00F0FF]/10 text-[#00F0FF] border-[#00F0FF]/50 shadow-[0_0_8px_rgba(0,240,255,0.4)]';
            case 'completed':
                return 'bg-[#ccff00]/10 text-[#ccff00] border-[#ccff00]/50 shadow-[0_0_8px_rgba(204,255,0,0.4)]';
            case 'cancelled':
                return 'bg-muted text-muted-foreground border-border';
            case 'dead_letter':
                return 'bg-[#FF3366]/10 text-[#FF3366] border-[#FF3366]/50 shadow-[0_0_8px_rgba(255,51,102,0.4)]';
            default:
                return 'bg-muted text-foreground';
        }
    };

    return (
        <Badge variant="outline" className={cn("rounded-none font-bold uppercase tracking-widest px-3 py-1 flex items-center gap-2", getBadgeStyle(status), className)}>
            {status === 'running' && <Loader2 className="w-3 h-3 animate-spin" />}
            {status.replace('_', ' ')}
        </Badge>
    );
}
