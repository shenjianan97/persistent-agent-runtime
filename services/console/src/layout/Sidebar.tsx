import { NavLink } from 'react-router';
import { AlertTriangle, PlaySquare, LayoutDashboard } from 'lucide-react';
import { cn } from '@/lib/utils';

const NAV_ITEMS = [
    { path: '/', label: 'Overview', icon: LayoutDashboard },
    { path: '/tasks/new', label: 'Submit Task', icon: PlaySquare },
    { path: '/dead-letter', label: 'Dead Letters', icon: AlertTriangle },
];

export function Sidebar() {
    return (
        <aside className="w-64 flex flex-col border-r border-border/40 bg-background/95 backdrop-blur shrink-0 z-20">
            <div className="h-16 flex items-center px-6 border-b border-border/40">
                <div className="text-xs text-muted-foreground font-bold tracking-[0.2em]">CONSOLE_V1.0</div>
            </div>

            <nav className="flex-1 py-6 px-3 flex flex-col gap-1">
                {NAV_ITEMS.map((item) => {
                    const Icon = item.icon;
                    return (
                        <NavLink
                            key={item.path}
                            to={item.path}
                            className={({ isActive }) => cn(
                                "group flex items-center gap-3 px-3 py-2 text-sm font-medium border border-transparent transition-all",
                                isActive
                                    ? "bg-primary/10 text-primary border-primary/20 shadow-[inset_2px_0_0_0_var(--color-primary)]"
                                    : "text-muted-foreground hover:bg-white/5 hover:text-foreground hover:border-border/40"
                            )}
                        >
                            <Icon className="w-4 h-4 opacity-70 group-hover:opacity-100" />
                            {item.label}
                        </NavLink>
                    );
                })}
            </nav>

            <div className="p-4 border-t border-border/40">
                <div className="text-[10px] uppercase text-muted-foreground tracking-widest bg-black/50 p-3 border border-border/40 flex flex-col gap-1 rounded-none">
                    <div><span className="text-primary opacity-50">SYS //</span> STABLE</div>
                    <div><span className="text-primary opacity-50">MEM //</span> OK</div>
                    <div><span className="text-primary opacity-50">LAT //</span> 24ms</div>
                </div>
            </div>
        </aside>
    );
}
