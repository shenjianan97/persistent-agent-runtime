import { BrainCircuit, Terminal } from 'lucide-react';

export function Header() {
    return (
        <div className="flex flex-col shrink-0 z-10 w-full">
            <header className="h-16 flex items-center justify-between px-6 border-b border-white/8 bg-[#09121d]/72 backdrop-blur-2xl">
                <div className="flex items-center gap-2">
                    <Terminal className="w-5 h-5 text-primary drop-shadow-[0_0_14px_var(--color-primary)]" />
                    <h1 className="font-semibold text-lg tracking-tight">PERSISTENT AGENT RUNTIME</h1>
                </div>

                <div className="flex items-center gap-2 text-sm">
                    <BrainCircuit className="w-4 h-4 text-muted-foreground" />
                    <span className="text-slate-400 uppercase tracking-[0.24em] text-[11px]">
                        Customer Execution Console
                    </span>
                </div>
            </header>
        </div>
    );
}
