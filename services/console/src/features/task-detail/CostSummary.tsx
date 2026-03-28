import { TaskObservabilityResponse } from '@/types';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { DollarSign, Cpu, Timer, Coins } from 'lucide-react';
import { formatUsd } from '@/lib/utils';

interface CostSummaryProps {
    observability?: TaskObservabilityResponse;
    checkpointCount: number;
    totalCostMicrodollars: number;
}

export function CostSummary({ observability, checkpointCount, totalCostMicrodollars }: CostSummaryProps) {
    const effectiveCost = observability?.total_cost_microdollars ?? totalCostMicrodollars;
    const formattedCost = formatUsd(effectiveCost);
    const isTerminal = observability?.status === 'completed' || observability?.status === 'dead_letter' || observability?.status === 'cancelled';
    const hasTrace = !!observability?.trace_id;

    const chartData = (observability?.spans ?? [])
        .filter((span) => span.type !== 'system')
        .map((span, index) => ({
        name: span.tool_name || span.model_name || span.node_name || `span-${index + 1}`,
        step: index + 1,
        cost: span.cost_microdollars / 1_000_000,
    }));

    return (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-6">
            <Card className="console-surface border-white/10">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-success">Total Cost</CardTitle>
                    <DollarSign className="h-4 w-4 text-success" />
                </CardHeader>
                <CardContent>
                    <div className="text-3xl font-display font-medium text-success drop-shadow-[0_0_8px_var(--color-success)]">
                        ${formattedCost}
                    </div>
                    <p className="text-xs text-muted-foreground mt-1 tracking-wider uppercase">USD</p>
                </CardContent>
            </Card>

            <Card className="console-surface border-white/10">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Checkpoints</CardTitle>
                    <Cpu className="h-4 w-4 text-primary" />
                </CardHeader>
                <CardContent>
                    <div className="text-3xl font-display font-medium text-primary drop-shadow-[0_0_8px_var(--color-primary)]">
                        {checkpointCount}
                    </div>
                    <p className="text-xs text-muted-foreground mt-1 tracking-wider uppercase">Saved states</p>
                </CardContent>
            </Card>

            <Card className="console-surface border-white/10">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Tokens</CardTitle>
                    <Coins className="h-4 w-4 text-primary" />
                </CardHeader>
                <CardContent>
                    <div className="text-3xl font-display font-medium text-primary drop-shadow-[0_0_8px_var(--color-primary)]">
                        {observability?.total_tokens ?? 0}
                    </div>
                    <p className="text-xs text-muted-foreground mt-1 tracking-wider uppercase">Prompt + completion</p>
                </CardContent>
            </Card>

            <Card className="console-surface border-white/10">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Duration</CardTitle>
                    <Timer className="h-4 w-4 text-primary" />
                </CardHeader>
                <CardContent>
                    <div className="text-3xl font-display font-medium text-primary drop-shadow-[0_0_8px_var(--color-primary)]">
                        {hasTrace && observability?.duration_ms != null ? `${observability.duration_ms}ms` : 'N/A'}
                    </div>
                    <p className="text-xs text-muted-foreground mt-1 tracking-wider uppercase">
                        {hasTrace ? 'Trace runtime' : 'Trace unavailable'}
                    </p>
                </CardContent>
            </Card>

            <Card className="md:col-span-2 xl:col-span-4 console-surface border-white/10">
                <CardHeader className="pb-2">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-muted-foreground">Cost Per Model/Tool Call</CardTitle>
                </CardHeader>
                <CardContent>
                    <div className="h-[200px] w-full mt-4">
                        {chartData.length > 0 ? (
                            <ResponsiveContainer width="100%" height="100%">
                                <BarChart data={chartData} margin={{ top: 0, right: 0, left: -20, bottom: 0 }}>
                                    <XAxis
                                        dataKey="step"
                                        stroke="#52525b"
                                        fontSize={12}
                                        tickLine={false}
                                        axisLine={false}
                                        tickFormatter={(val) => `Call ${val}`}
                                    />
                                    <YAxis
                                        stroke="#52525b"
                                        fontSize={12}
                                        tickLine={false}
                                        axisLine={false}
                                        tickFormatter={(value) => `$${value.toFixed(4)}`}
                                    />
                                    <Tooltip
                                        cursor={{ fill: 'rgba(255,255,255,0.05)' }}
                                        content={({ active, payload }) => {
                                            if (active && payload && payload.length) {
                                                return (
                                                    <div className="bg-black border border-primary p-2 text-xs font-mono shadow-[0_0_8px_rgba(0,240,255,0.2)]">
                                                        <p className="text-primary font-bold">{payload[0].payload.name}</p>
                                                        <p className="text-muted-foreground">Call: {payload[0].payload.step}</p>
                                                        <p className="text-success">Cost: ${(payload[0].value as number).toFixed(4)}</p>
                                                    </div>
                                                );
                                            }
                                            return null;
                                        }}
                                    />
                                    <Bar dataKey="cost" fill="#00F0FF" radius={[0, 0, 0, 0]} />
                                </BarChart>
                            </ResponsiveContainer>
                        ) : isTerminal ? (
                            <div className="w-full h-full flex items-center justify-center text-muted-foreground border border-dashed border-border/40">
                                <span className="uppercase tracking-widest text-xs">No model or tool call cost was recorded for this task.</span>
                            </div>
                        ) : (
                            <div className="w-full h-full flex items-center justify-center text-muted-foreground border border-dashed border-border/40">
                                <span className="uppercase tracking-widest text-xs">Awaiting model and tool call data...</span>
                            </div>
                        )}
                    </div>
                </CardContent>
            </Card>
        </div>
    );
}
