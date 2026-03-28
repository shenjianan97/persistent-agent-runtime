import { NavLink } from 'react-router';
import { AlertTriangle, PlaySquare, LayoutDashboard, List } from 'lucide-react';
import { cn } from '@/lib/utils';

const NAV_ITEMS = [
    { path: '/', label: 'Home', icon: LayoutDashboard },
    { path: '/tasks', label: 'Tasks', icon: List, end: true },
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
                            end={'end' in item && item.end}
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
        </aside>
    );
}
