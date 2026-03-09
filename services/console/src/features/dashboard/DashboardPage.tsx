import { useHealth } from './useHealth';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Activity, Database, Server, Clock } from 'lucide-react';
import { cn } from '@/lib/utils';

export function DashboardPage() {
    const { data: health, isLoading, isError } = useHealth();

    const isUp = !isError && health?.status === 'UP';

    return (
        <div className="space-y-8 animate-in fade-in duration-500">
            <div>
                <h2 className="text-2xl font-display font-medium uppercase tracking-wider mb-2">System Overview</h2>
                <p className="text-muted-foreground">Real-time status of the Persistent Agent Runtime.</p>
            </div>

            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
                <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                        <CardTitle className="text-sm font-medium tracking-wide text-muted-foreground uppercase">Runtime Status</CardTitle>
                        <Activity className={cn("h-4 w-4", isUp ? "text-primary border-primary" : "text-destructive")} />
                    </CardHeader>
                    <CardContent>
                        {isLoading ? (
                            <div className="h-8 bg-muted/20 animate-pulse w-24"></div>
                        ) : (
                            <div className={cn("text-2xl font-bold uppercase tracking-widest", isUp ? "text-primary drop-shadow-[0_0_8px_var(--color-primary)]" : "text-destructive")}>
                                {isUp ? 'Online' : 'Offline'}
                            </div>
                        )}
                    </CardContent>
                </Card>

                <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                        <CardTitle className="text-sm font-medium tracking-wide text-muted-foreground uppercase">Database</CardTitle>
                        <Database className={cn("h-4 w-4", health?.database_connected ? "text-[#ccff00]" : "text-destructive")} />
                    </CardHeader>
                    <CardContent>
                        {isLoading ? (
                            <div className="h-8 bg-muted/20 animate-pulse w-24"></div>
                        ) : (
                            <div className={cn("text-2xl font-bold uppercase tracking-widest", health?.database_connected ? "text-[#ccff00]" : "text-destructive")}>
                                {health?.database_connected ? 'Connected' : 'Disconnected'}
                            </div>
                        )}
                    </CardContent>
                </Card>

                <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                        <CardTitle className="text-sm font-medium tracking-wide text-muted-foreground uppercase">Active Workers</CardTitle>
                        <Server className="h-4 w-4 text-muted-foreground" />
                    </CardHeader>
                    <CardContent>
                        {isLoading ? (
                            <div className="h-8 bg-muted/20 animate-pulse w-16"></div>
                        ) : (
                            <div className="text-2xl font-bold tracking-widest">{health?.active_workers ?? 0}</div>
                        )}
                        <p className="text-[10px] text-muted-foreground mt-1 uppercase tracking-wider">Nodes Connected</p>
                    </CardContent>
                </Card>

                <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
                    <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                        <CardTitle className="text-sm font-medium tracking-wide text-muted-foreground uppercase">Queued Tasks</CardTitle>
                        <Clock className="h-4 w-4 text-muted-foreground" />
                    </CardHeader>
                    <CardContent>
                        {isLoading ? (
                            <div className="h-8 bg-muted/20 animate-pulse w-16"></div>
                        ) : (
                            <div className="text-2xl font-bold tracking-widest">{health?.queued_tasks ?? 0}</div>
                        )}
                        <p className="text-[10px] text-muted-foreground mt-1 uppercase tracking-wider">Pending execution</p>
                    </CardContent>
                </Card>
            </div>
        </div>
    );
}
