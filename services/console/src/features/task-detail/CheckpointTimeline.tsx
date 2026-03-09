import { CheckpointResponse } from '@/types';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Zap, MoveRight } from 'lucide-react';
import { useEffect, useRef } from 'react';
import { formatUsd } from '@/lib/utils';

interface CheckpointTimelineProps {
    checkpoints: CheckpointResponse[];
    isRunning: boolean;
}

export function CheckpointTimeline({ checkpoints, isRunning }: CheckpointTimelineProps) {
    const scrollRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
            // Scroll to bottom manually on container
            const viewport = scrollRef.current.querySelector('[data-radix-scroll-area-viewport]');
            if (viewport) {
                viewport.scrollTop = viewport.scrollHeight;
            }
        }
    }, [checkpoints.length]);

    return (
        <div className="border border-border/40 bg-black/40 backdrop-blur flex flex-col h-[500px]">
            <div className="p-4 border-b border-border/40 bg-black/60 shrink-0">
                <h3 className="font-display text-sm uppercase tracking-widest text-primary flex items-center gap-2">
                    <Zap className="w-4 h-4" /> Execution Timeline
                </h3>
            </div>

            <ScrollArea className="flex-1" ref={scrollRef}>
                <div className="p-6">
                    {checkpoints.length === 0 ? (
                        <div className="h-full flex items-center justify-center pt-20">
                            <span className="text-muted-foreground text-sm tracking-widest uppercase animate-pulse">
                                Waiting for checkpoints...
                            </span>
                        </div>
                    ) : (
                        <div className="relative border-l border-border/40 ml-4 space-y-8 pl-8 pb-8">
                            {checkpoints.map((cp, idx) => {
                                const prevWorker = idx > 0 ? checkpoints[idx - 1].worker_id : null;
                                const isHandoff = prevWorker && prevWorker !== cp.worker_id;

                                return (
                                    <div key={cp.checkpoint_id} className="relative animate-in slide-in-from-left-4 fade-in duration-300">
                                        <div className="absolute -left-[45px] top-1 h-6 w-6 rounded-full border-2 border-background bg-primary shadow-[0_0_8px_var(--color-primary)] ring-2 ring-primary/20 flex items-center justify-center">
                                            <div className="w-2 h-2 rounded-full bg-black" />
                                        </div>

                                        <div className="space-y-3">
                                            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                                                <div className="flex items-center gap-3">
                                                    <span className="text-sm font-bold text-primary tracking-wider uppercase">Step {cp.step_number}</span>
                                                    <span className="text-sm font-medium border border-border/40 bg-white/5 px-2 py-0.5 text-foreground">{cp.node_name}</span>
                                                </div>
                                                <span className="text-xs text-muted-foreground tabular-nums">
                                                    {new Date(cp.created_at).toLocaleTimeString()}
                                                </span>
                                            </div>

                                            {isHandoff && (
                                                <div className="bg-warning/10 border border-warning/20 p-2 text-xs text-warning flex items-center gap-2">
                                                    <MoveRight className="w-3 h-3 shrink-0" />
                                                    <span className="font-bold tracking-widest uppercase shrink-0">Handoff:</span>
                                                    <span className="opacity-80 truncate" title={`${prevWorker} → ${cp.worker_id}`}>
                                                        {prevWorker.split('-')[0]} → {cp.worker_id.split('-')[0]}
                                                    </span>
                                                </div>
                                            )}

                                            <div className="grid grid-cols-2 gap-4 text-xs font-mono bg-black/50 p-3 border border-border/20">
                                                <div>
                                                    <span className="text-muted-foreground block mb-1">Worker ID</span>
                                                    <span className="truncate block opacity-80" title={cp.worker_id}>{cp.worker_id.split('-')[0]}...</span>
                                                </div>
                                                <div>
                                                    <span className="text-muted-foreground block mb-1">Cost Delta</span>
                                                    <span className="text-success">+${formatUsd(cp.cost_microdollars)}</span>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                );
                            })}

                            {isRunning && (
                                <div className="relative pt-4">
                                    <div className="absolute -left-[41px] top-6 h-4 w-4 rounded-full border border-primary bg-primary/20 animate-ping" />
                                    <div className="absolute -left-[39px] top-[26px] h-3 w-3 rounded-full bg-primary" />
                                    <div className="pl-2">
                                        <span className="text-xs tracking-widest font-bold uppercase text-primary animate-pulse">Running Compute...</span>
                                    </div>
                                </div>
                            )}
                        </div>
                    )}
                </div>
            </ScrollArea>
        </div>
    );
}
