import { CheckpointResponse } from '@/types';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { DollarSign, Cpu } from 'lucide-react';

interface CostSummaryProps {
    checkpoints: CheckpointResponse[];
    totalCostMicrodollars: number;
}

export function CostSummary({ checkpoints, totalCostMicrodollars }: CostSummaryProps) {
    const formattedCost = (totalCostMicrodollars / 1_000_000).toFixed(4);

    const chartData = checkpoints.map(cp => ({
        name: cp.node_name,
        step: cp.step_number,
        cost: cp.cost_microdollars / 1_000_000,
    }));

    return (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-[#ccff00]">Total Cost</CardTitle>
                    <DollarSign className="h-4 w-4 text-[#ccff00]" />
                </CardHeader>
                <CardContent>
                    <div className="text-3xl font-display font-medium text-[#ccff00] drop-shadow-[0_0_8px_rgba(204,255,0,0.4)]">
                        ${formattedCost}
                    </div>
                    <p className="text-xs text-muted-foreground mt-1 tracking-wider uppercase">USD</p>
                </CardContent>
            </Card>

            <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Checkpoints</CardTitle>
                    <Cpu className="h-4 w-4 text-primary" />
                </CardHeader>
                <CardContent>
                    <div className="text-3xl font-display font-medium text-primary drop-shadow-[0_0_8px_rgba(0,240,255,0.4)]">
                        {checkpoints.length}
                    </div>
                    <p className="text-xs text-muted-foreground mt-1 tracking-wider uppercase">Saved states</p>
                </CardContent>
            </Card>

            <Card className="md:col-span-2 rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
                <CardHeader className="pb-2">
                    <CardTitle className="text-sm font-display uppercase tracking-widest text-muted-foreground">Cost Per Step</CardTitle>
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
                                        tickFormatter={(val) => `Step ${val}`}
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
                                                        <p className="text-muted-foreground">Step: {payload[0].payload.step}</p>
                                                        <p className="text-[#ccff00]">Cost: ${(payload[0].value as number).toFixed(4)}</p>
                                                    </div>
                                                );
                                            }
                                            return null;
                                        }}
                                    />
                                    <Bar dataKey="cost" fill="#00F0FF" radius={[0, 0, 0, 0]} />
                                </BarChart>
                            </ResponsiveContainer>
                        ) : (
                            <div className="w-full h-full flex items-center justify-center text-muted-foreground border border-dashed border-border/40">
                                <span className="uppercase tracking-widest text-xs">Awaiting Execution Data...</span>
                            </div>
                        )}
                    </div>
                </CardContent>
            </Card>
        </div>
    );
}
