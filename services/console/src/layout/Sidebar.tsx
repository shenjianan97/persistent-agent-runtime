import { NavLink } from 'react-router';
import { AlertTriangle, PlaySquare, LayoutDashboard, List, Settings, Bot, Server } from 'lucide-react';
import { cn } from '@/lib/utils';

const NAV_ITEMS = [
    { path: '/', label: 'Home', icon: LayoutDashboard },
    { path: '/agents', label: 'Agents', icon: Bot, end: true },
    { path: '/tool-servers', label: 'Tool Servers', icon: Server, end: true },
    { path: '/tasks', label: 'Tasks', icon: List, end: true },
    { path: '/tasks/new', label: 'Submit Task', icon: PlaySquare },
    { path: '/dead-letter', label: 'Failed', icon: AlertTriangle },
    { path: '/settings', label: 'Settings', icon: Settings },
];

export function Sidebar() {
    return (
        <aside className="w-64 flex flex-col border-r border-white/8 bg-[#09121d]/80 backdrop-blur-2xl shrink-0 z-20">
            <div className="h-16 flex items-center px-6 border-b border-white/8">
                <div className="text-xs text-slate-400 font-bold tracking-[0.2em]">CONSOLE_V1.0</div>
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
                                "group flex items-center gap-3 px-3 py-2.5 text-sm font-medium border border-transparent rounded-xl transition-all duration-200",
                                isActive
                                    ? "bg-linear-to-r from-primary to-sky-400 text-primary-foreground shadow-[0_0_28px_rgba(99,215,255,0.22)]"
                                    : "text-muted-foreground hover:bg-white/6 hover:text-foreground hover:border-white/8"
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
