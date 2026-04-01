import { useParams, useNavigate } from 'react-router';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { useAgent, useUpdateAgent } from './useAgents';
import { useModels } from '@/features/submit/useModels';
import { ALLOWED_TOOLS } from '@/features/submit/schema';
import { groupModelsByProvider } from '@/lib/models';
import { toast } from 'sonner';
import { useEffect } from 'react';

import {
    Form, FormControl, FormField, FormItem, FormLabel, FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Bot, PlaySquare } from 'lucide-react';

const agentDetailSchema = z.object({
    display_name: z.string().min(1, 'Agent name is required').max(200),
    system_prompt: z.string().min(1, 'System prompt is required').max(51200),
    provider: z.string().min(1, 'Provider is required'),
    model: z.string().min(1, 'Model is required'),
    temperature: z.number().min(0).max(2).default(0.7),
    allowed_tools: z.array(z.string()).default([]),
    status: z.enum(['active', 'disabled']),
});

type AgentDetailFormValues = z.infer<typeof agentDetailSchema>;

export function AgentDetailPage() {
    const { agentId } = useParams<{ agentId: string }>();
    const navigate = useNavigate();
    const { data: agent, isLoading, error } = useAgent(agentId!);
    const mutation = useUpdateAgent();
    const { data: models = [], isLoading: isLoadingModels } = useModels();
    const modelGroups = groupModelsByProvider(models);

    const form = useForm<AgentDetailFormValues>({
        resolver: zodResolver(agentDetailSchema),
        defaultValues: {
            display_name: '',
            system_prompt: '',
            provider: '',
            model: '',
            temperature: 0.7,
            allowed_tools: [],
            status: 'active',
        },
    });

    useEffect(() => {
        if (agent) {
            form.reset({
                display_name: agent.display_name,
                system_prompt: agent.agent_config.system_prompt,
                provider: agent.agent_config.provider,
                model: agent.agent_config.model,
                temperature: agent.agent_config.temperature,
                allowed_tools: agent.agent_config.allowed_tools ?? [],
                status: agent.status,
            });
        }
    }, [agent, form]);

    function onSubmit(data: AgentDetailFormValues) {
        if (!agentId) return;
        mutation.mutate(
            {
                agentId,
                request: {
                    display_name: data.display_name,
                    agent_config: {
                        system_prompt: data.system_prompt,
                        provider: data.provider,
                        model: data.model,
                        temperature: data.temperature,
                        allowed_tools: data.allowed_tools,
                    },
                    status: data.status,
                },
            },
            {
                onSuccess: () => {
                    toast.success('Agent updated', {
                        description: 'Configuration saved successfully.',
                    });
                },
                onError: (error: Error) => {
                    toast.error('Failed to update agent', {
                        description: error.message || 'Unknown error occurred.',
                    });
                },
            }
        );
    }

    if (isLoading) {
        return (
            <div className="max-w-4xl mx-auto animate-in fade-in duration-500">
                <div className="console-surface-strong rounded-[28px] p-6 md:p-7">
                    <span className="uppercase tracking-widest text-xs font-bold text-muted-foreground animate-pulse">Loading agent...</span>
                </div>
            </div>
        );
    }

    if (error || !agent) {
        return (
            <div className="max-w-4xl mx-auto animate-in fade-in duration-500">
                <div className="console-surface-strong rounded-[28px] p-6 md:p-7">
                    <h2 className="text-xl font-display font-semibold text-destructive mb-2">Agent Not Found</h2>
                    <p className="text-muted-foreground text-sm">
                        The agent <code className="font-mono text-foreground">{agentId}</code> could not be found.
                    </p>
                </div>
            </div>
        );
    }

    const isDisabled = form.watch('status') === 'disabled';

    return (
        <div className="max-w-4xl mx-auto animate-in fade-in duration-500">
            <div className="console-surface-strong rounded-[28px] p-6 md:p-7 mb-8 flex flex-col md:flex-row md:items-end justify-between gap-4">
                <div>
                    <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.24em] text-primary">Agent Detail</div>
                    <h2 className="text-3xl font-display font-semibold tracking-tight mb-1 flex items-center gap-2">
                        <Bot className="w-6 h-6 text-primary drop-shadow-[0_0_12px_var(--color-primary)]" />
                        {agent.display_name}
                    </h2>
                    <p className="text-muted-foreground font-mono text-sm">{agent.agent_id}</p>
                    <div className="mt-2">
                        <Badge
                            variant={agent.status === 'active' ? 'default' : 'secondary'}
                            className={
                                agent.status === 'active'
                                    ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30 text-[10px] px-2 py-0.5'
                                    : 'bg-zinc-500/20 text-zinc-400 border-zinc-500/30 text-[10px] px-2 py-0.5'
                            }
                        >
                            {agent.status}
                        </Badge>
                    </div>
                </div>
                <Button
                    onClick={() => navigate(`/tasks/new?agent_id=${encodeURIComponent(agent.agent_id)}`)}
                    disabled={agent.status === 'disabled'}
                    className="font-bold uppercase tracking-widest px-6 hover:saturate-150 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
                >
                    <PlaySquare className="w-4 h-4 mr-2" />
                    Submit Task
                </Button>
            </div>

            <Form {...form}>
                <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-6">
                    <Card className="console-surface border-white/10">
                        <CardHeader className="border-b border-white/8 pb-4">
                            <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Configuration</CardTitle>
                        </CardHeader>
                        <CardContent className="pt-6 space-y-6">
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                <FormField
                                    control={form.control}
                                    name="display_name"
                                    render={({ field }) => (
                                        <FormItem>
                                            <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Agent Name</FormLabel>
                                            <FormControl>
                                                <Input className="rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1" {...field} />
                                            </FormControl>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
                                />
                                <FormField
                                    control={form.control}
                                    name="model"
                                    render={({ field }) => {
                                        const currentValue = form.getValues('provider') && field.value
                                            ? `${form.getValues('provider')}|${field.value}`
                                            : '';
                                        return (
                                            <FormItem>
                                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Model</FormLabel>
                                                <FormControl>
                                                    <select
                                                        className="flex h-10 w-full border border-border bg-black/50 px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary focus-visible:ring-offset-0 disabled:cursor-not-allowed disabled:opacity-50 rounded-none appearance-none"
                                                        value={currentValue}
                                                        onChange={(e) => {
                                                            const val = e.target.value;
                                                            if (val) {
                                                                const [provider, modelId] = val.split('|');
                                                                form.setValue('provider', provider);
                                                                field.onChange(modelId);
                                                            } else {
                                                                form.setValue('provider', '');
                                                                field.onChange('');
                                                            }
                                                        }}
                                                    >
                                                        <option value="" disabled>{isLoadingModels ? 'Loading models...' : 'Select model'}</option>
                                                        {modelGroups.map((group) => (
                                                            <optgroup key={group.provider} label={group.label}>
                                                                {group.models.map((m) => (
                                                                    <option key={`${m.provider}|${m.model_id}`} value={`${m.provider}|${m.model_id}`}>
                                                                        {m.display_name}
                                                                    </option>
                                                                ))}
                                                            </optgroup>
                                                        ))}
                                                    </select>
                                                </FormControl>
                                                <FormMessage className="text-destructive font-bold text-xs" />
                                            </FormItem>
                                        );
                                    }}
                                />
                            </div>

                            <FormField
                                control={form.control}
                                name="system_prompt"
                                render={({ field }) => (
                                    <FormItem>
                                        <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">System Prompt</FormLabel>
                                        <FormControl>
                                            <Textarea
                                                className="min-h-[100px] resize-y rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1"
                                                {...field}
                                            />
                                        </FormControl>
                                        <FormMessage className="text-destructive font-bold text-xs" />
                                    </FormItem>
                                )}
                            />

                            <FormField
                                control={form.control}
                                name="temperature"
                                render={({ field }) => (
                                    <FormItem>
                                        <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Temperature</FormLabel>
                                        <FormControl>
                                            <Input
                                                type="number"
                                                step="0.1"
                                                min="0"
                                                max="2"
                                                className="rounded-none border-border bg-black/50 w-32"
                                                {...field}
                                                onChange={(e) => field.onChange(parseFloat(e.target.value))}
                                            />
                                        </FormControl>
                                        <FormMessage className="text-destructive font-bold text-xs" />
                                    </FormItem>
                                )}
                            />

                            <FormField
                                control={form.control}
                                name="allowed_tools"
                                render={() => (
                                    <FormItem>
                                        <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Allowed Tools</FormLabel>
                                        <div className="flex flex-wrap gap-4 mt-2">
                                            {ALLOWED_TOOLS.map((item) => (
                                                <FormField
                                                    key={item.id}
                                                    control={form.control}
                                                    name="allowed_tools"
                                                    render={({ field }) => (
                                                        <FormItem className="flex flex-row items-start space-x-3 space-y-0">
                                                            <FormControl>
                                                                <Checkbox
                                                                    className="rounded-none border-primary data-[state=checked]:bg-primary data-[state=checked]:text-black"
                                                                    checked={field.value?.includes(item.id)}
                                                                    onCheckedChange={(checked) =>
                                                                        checked
                                                                            ? field.onChange([...(field.value || []), item.id])
                                                                            : field.onChange(field.value?.filter((v) => v !== item.id))
                                                                    }
                                                                />
                                                            </FormControl>
                                                            <FormLabel className="font-normal font-mono cursor-pointer text-sm">
                                                                {item.label}
                                                            </FormLabel>
                                                        </FormItem>
                                                    )}
                                                />
                                            ))}
                                        </div>
                                        <FormMessage className="text-destructive font-bold text-xs" />
                                    </FormItem>
                                )}
                            />
                        </CardContent>
                    </Card>

                    <Card className="console-surface border-white/10">
                        <CardHeader className="border-b border-white/8 pb-4">
                            <CardTitle className="text-sm font-display uppercase tracking-widest">Lifecycle</CardTitle>
                        </CardHeader>
                        <CardContent className="pt-6">
                            <FormField
                                control={form.control}
                                name="status"
                                render={({ field }) => (
                                    <FormItem>
                                        <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Status</FormLabel>
                                        <FormControl>
                                            <select
                                                className="flex h-10 w-48 border border-border bg-black/50 px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary focus-visible:ring-offset-0 rounded-none appearance-none"
                                                value={field.value}
                                                onChange={field.onChange}
                                            >
                                                <option value="active">Active</option>
                                                <option value="disabled">Disabled</option>
                                            </select>
                                        </FormControl>
                                        {isDisabled && (
                                            <p className="text-xs text-amber-400 mt-1">Disabled agents cannot be used for new task submissions.</p>
                                        )}
                                        <FormMessage className="text-destructive font-bold text-xs" />
                                    </FormItem>
                                )}
                            />
                        </CardContent>
                    </Card>

                    <div className="flex justify-end pt-4 pb-12">
                        <Button
                            type="submit"
                            disabled={mutation.isPending}
                            className="font-bold uppercase tracking-widest px-8 hover:saturate-150 transition-all"
                        >
                            {mutation.isPending ? 'Saving...' : 'Save Changes'}
                        </Button>
                    </div>
                </form>
            </Form>
        </div>
    );
}
