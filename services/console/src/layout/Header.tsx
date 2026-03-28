import { BrainCircuit, Terminal } from 'lucide-react';

export function Header() {
    return (
        <div className="flex flex-col shrink-0 z-10 w-full">
            <header className="h-16 flex items-center justify-between px-6 border-b border-border/40 bg-background/95 backdrop-blur">
                <div className="flex items-center gap-2">
                    <Terminal className="w-5 h-5 text-primary" />
                    <h1 className="font-display font-semibold text-lg tracking-tight">PERSISTENT AGENT RUNTIME</h1>
                </div>

                <div className="flex items-center gap-2 text-sm">
                    <BrainCircuit className="w-4 h-4 text-muted-foreground" />
                    <span className="text-muted-foreground uppercase tracking-widest text-xs">
                        Customer Execution Console
                    </span>
                </div>
            </header>
        </div>
    );
}
