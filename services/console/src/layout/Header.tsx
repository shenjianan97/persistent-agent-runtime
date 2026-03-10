import { useHealth } from '@/features/dashboard/useHealth';
import { Activity, Database, Server, Terminal, AlertCircle } from 'lucide-react';
import { cn } from '@/lib/utils';

export function Header() {
    const { data: health, isError, isLoading } = useHealth();

    const isHealthy = !isLoading && !isError && health?.status === 'healthy' && health?.database === 'connected';
    const systemLabel = isLoading ? 'SYNCING' : isHealthy ? 'ONLINE' : 'OFFLINE';

    return (
        <div className="flex flex-col shrink-0 z-10 w-full">
            {/* API Unreachable Banner */}
            {isError && (
                <div className="bg-destructive text-destructive-foreground px-4 py-2 text-xs font-bold flex items-center justify-center gap-2 uppercase tracking-widest border-b border-[rgba(0,0,0,0.2)]">
                    <AlertCircle className="w-4 h-4" />
                    API unavailable — is the API service running on port 8080?
                </div>
            )}

            <header className="h-16 flex items-center justify-between px-6 border-b border-border/40 bg-background/95 backdrop-blur">
                <div className="flex items-center gap-2">
                    <Terminal className="w-5 h-5 text-primary" />
                    <h1 className="font-display font-semibold text-lg tracking-tight">PERSISTENT AGENT RUNTIME</h1>
                </div>

                <div className="flex items-center gap-6 text-sm">
                    <div className="flex items-center gap-2">
                        <Database className="w-4 h-4 text-muted-foreground" />
                        <span className="text-muted-foreground mr-1">DB</span>
                        <div
                            className={cn(
                                "w-2 h-2 rounded-full",
                                isLoading
                                    ? "bg-muted-foreground/50"
                                    : isHealthy
                                        ? "bg-success shadow-[0_0_8px_var(--color-success)]"
                                        : "bg-destructive shadow-[0_0_8px_var(--color-destructive)]"
                            )}
                        />
                    </div>

                    <div className="flex items-center gap-2">
                        <Server className="w-4 h-4 text-muted-foreground" />
                        <span className="text-muted-foreground mr-1">WORKERS</span>
                        <div
                            className={cn(
                                "font-bold",
                                isLoading
                                    ? "text-muted-foreground"
                                    : (!health || health.active_workers === 0)
                                        ? "text-destructive"
                                        : ""
                            )}
                        >
                            {isLoading ? '...' : (health?.active_workers ?? '-')}
                        </div>
                    </div>

                    <div className="flex items-center gap-2 border-l border-border/40 pl-6">
                        <Activity className="w-4 h-4 text-muted-foreground" />
                        <span className="text-muted-foreground">SYSTEM</span>
                        <div
                            className={cn(
                                "px-2 py-0.5 text-xs font-bold tracking-wider border",
                                isLoading
                                    ? "bg-muted/10 text-muted-foreground border-border/40"
                                    : isHealthy
                                        ? "bg-success/10 text-success border-success/20"
                                        : "bg-destructive/10 text-destructive border-destructive/20"
                            )}
                        >
                            {systemLabel}
                        </div>
                    </div>
                </div>
            </header>
        </div>
    );
}
