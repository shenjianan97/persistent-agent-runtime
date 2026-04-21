import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { useCreateAgent } from './useAgents';
import { useModels } from '@/features/submit/useModels';
import { groupModelsByProvider } from '@/lib/models';
import { toast } from 'sonner';
import { formatUsd } from '@/lib/utils';
import { useToolServers } from '../tool-servers/useToolServers';
import { ContextManagementSection } from './ContextManagementSection';
import type { ContextManagementConfig } from './ContextManagementSection';

import {
    Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/ui/dialog';
import {
    Form, FormControl, FormField, FormItem, FormLabel, FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';

const createAgentSchema = z.object({
    display_name: z.string().min(1, 'Agent name is required').max(200),
    system_prompt: z.string().min(1, 'System prompt is required').max(51200),
    provider: z.string().min(1, 'Provider is required'),
    model: z.string().min(1, 'Model is required'),
    temperature: z.number().min(0).max(2).default(0.7),
    tool_servers: z.array(z.string()).default([]),
    max_concurrent_tasks: z.number().int().min(1).default(5),
    budget_max_per_task: z.number().int().min(1).default(500000),
    budget_max_per_hour: z.number().int().min(1).default(5000000),
    sandbox_enabled: z.boolean().default(false),
    sandbox_template: z.string().default(''),
    sandbox_vcpu: z.number().int().min(1).max(8).default(2),
    sandbox_memory_mb: z.number().int().min(512).max(8192).default(2048),
    sandbox_timeout_seconds: z.number().int().min(60).max(86400).default(3600),
    memory_enabled: z.boolean().default(false),
    memory_summarizer_model: z.string().default(''),
    memory_max_entries: z
        .string()
        .default('')
        .refine((value) => {
            if (value.trim() === '') return true;
            const parsed = Number.parseInt(value, 10);
            return Number.isInteger(parsed) && parsed >= 100 && parsed <= 100_000;
        }, 'Max entries must be between 100 and 100,000'),
});

type CreateAgentFormValues = z.infer<typeof createAgentSchema>;

interface CreateAgentDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
}

function buildContextManagementPayload(
    config: ContextManagementConfig | undefined,
): ContextManagementConfig {
    const summarizer = config?.summarizer_model?.trim();
    const excludeTools = config?.exclude_tools ?? [];
    return {
        ...(summarizer ? { summarizer_model: summarizer } : {}),
        ...(excludeTools.length ? { exclude_tools: excludeTools } : {}),
        pre_tier3_memory_flush: !!config?.pre_tier3_memory_flush,
    };
}

export function CreateAgentDialog({ open, onOpenChange }: CreateAgentDialogProps) {
    const mutation = useCreateAgent();
    const { data: models = [], isLoading: isLoadingModels } = useModels();
    const modelGroups = groupModelsByProvider(models);
    const { data: toolServers = [] } = useToolServers('active');
    const [ctxMgmt, setCtxMgmt] = useState<ContextManagementConfig | undefined>(undefined);
    const ctxMgmtDirty = useRef(false);

    const form = useForm<CreateAgentFormValues>({
        resolver: zodResolver(createAgentSchema),
        defaultValues: {
            display_name: '',
            system_prompt: 'You are a helpful assistant. Provide concise and accurate answers.',
            provider: '',
            model: '',
            temperature: 0.7,
            tool_servers: [],
            max_concurrent_tasks: 5,
            budget_max_per_task: 500000,
            budget_max_per_hour: 5000000,
            sandbox_enabled: false,
            sandbox_template: '',
            sandbox_vcpu: 2,
            sandbox_memory_mb: 2048,
            sandbox_timeout_seconds: 3600,
            memory_enabled: false,
            memory_summarizer_model: '',
            memory_max_entries: '',
        },
    });

    const selectedToolServers = form.watch('tool_servers');
    const sandboxEnabled = form.watch('sandbox_enabled');
    const memoryEnabled = form.watch('memory_enabled');
    const selectedProvider = form.watch('provider');
    const selectedModel = form.watch('model');

    useEffect(() => {
        if (models.length === 0) return;
        if (selectedModel) return;
        const first = models[0];
        form.setValue('provider', first.provider);
        form.setValue('model', first.model_id);
    }, [models, selectedModel, form]);
    const providerFilteredModels = useMemo(
        () => models.filter((m) => m.provider === selectedProvider),
        [models, selectedProvider],
    );

    useEffect(() => {
        if (!ctxMgmt?.summarizer_model) return;
        const stillValid = providerFilteredModels.some((m) => m.model_id === ctxMgmt.summarizer_model);
        if (!stillValid) {
            ctxMgmtDirty.current = true;
            setCtxMgmt({ ...ctxMgmt, summarizer_model: undefined });
        }
    }, [providerFilteredModels, ctxMgmt]);

    const handleCtxMgmtChange = useCallback((next: ContextManagementConfig) => {
        ctxMgmtDirty.current = true;
        setCtxMgmt(next);
    }, []);

    function onSubmit(data: CreateAgentFormValues) {
        const sandboxConfig = data.sandbox_enabled
            ? {
                enabled: true,
                template: data.sandbox_template,
                vcpu: data.sandbox_vcpu,
                memory_mb: data.sandbox_memory_mb,
                timeout_seconds: data.sandbox_timeout_seconds,
            }
            : undefined;
        const parsedMaxEntries = data.memory_max_entries.trim() === ''
            ? undefined
            : Number.parseInt(data.memory_max_entries, 10);
        const summarizerModel = data.memory_summarizer_model.trim();
        const hasMemoryConfig = data.memory_enabled || !!summarizerModel || parsedMaxEntries !== undefined;
        const memoryConfig = hasMemoryConfig
            ? {
                enabled: data.memory_enabled,
                ...(summarizerModel ? { summarizer_model: summarizerModel } : {}),
                ...(parsedMaxEntries !== undefined ? { max_entries: parsedMaxEntries } : {}),
            }
            : undefined;
        const contextManagementPayload = ctxMgmtDirty.current
            ? buildContextManagementPayload(ctxMgmt)
            : undefined;

        mutation.mutate(
            {
                display_name: data.display_name,
                agent_config: {
                    system_prompt: data.system_prompt,
                    provider: data.provider,
                    model: data.model,
                    temperature: data.temperature,
                    tool_servers: data.tool_servers,
                    ...(sandboxConfig ? { sandbox: sandboxConfig } : {}),
                    ...(memoryConfig ? { memory: memoryConfig } : {}),
                    ...(contextManagementPayload ? { context_management: contextManagementPayload } : {}),
                },
                max_concurrent_tasks: data.max_concurrent_tasks,
                budget_max_per_task: data.budget_max_per_task,
                budget_max_per_hour: data.budget_max_per_hour,
            },
            {
                onSuccess: () => {
                    toast.success('Agent created', {
                        description: `Agent "${data.display_name}" is now active.`,
                    });
                    form.reset();
                    setCtxMgmt(undefined);
                    ctxMgmtDirty.current = false;
                    onOpenChange(false);
                },
                onError: (error: Error) => {
                    toast.error('Failed to create agent', {
                        description: error.message || 'Unknown error occurred.',
                    });
                },
            }
        );
    }

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="w-[calc(100vw-2rem)] max-h-[calc(100vh-2rem)] sm:max-w-[600px] sm:max-h-[calc(100vh-4rem)] overflow-hidden flex flex-col console-surface border-white/10 rounded-2xl p-0 gap-0">
                <DialogHeader className="px-6 pt-6 shrink-0">
                    <DialogTitle className="text-lg font-display uppercase tracking-widest text-primary">
                        Create Agent
                    </DialogTitle>
                    <DialogDescription className="sr-only">
                        Create a new agent and configure its model, tools, memory, and context management settings.
                    </DialogDescription>
                </DialogHeader>

                <Form {...form}>
                    <form onSubmit={form.handleSubmit(onSubmit)} className="min-h-0 flex flex-col flex-1">
                        <div className="flex-1 overflow-y-auto px-6 space-y-5 pb-6">
                        <FormField
                            control={form.control}
                            name="display_name"
                            render={({ field }) => (
                                <FormItem>
                                    <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Agent Name</FormLabel>
                                    <FormControl>
                                        <Input className="rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1" placeholder="e.g., Support Agent" {...field} />
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

                        <FormField
                            control={form.control}
                            name="system_prompt"
                            render={({ field }) => (
                                <FormItem>
                                    <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">System Prompt</FormLabel>
                                    <FormControl>
                                        <Textarea
                                            className="min-h-[80px] resize-y rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1"
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

                        <div className="space-y-3">
                            <div className="uppercase tracking-widest text-muted-foreground text-xs">Tool Servers</div>
                            {toolServers.length === 0 ? (
                                <p className="text-muted-foreground text-xs">
                                    No tool servers registered. Register one in Tool Servers to give this agent custom tools.
                                </p>
                            ) : (
                                <div className="space-y-2">
                                    {toolServers.map((server) => (
                                        <label
                                            key={server.server_id}
                                            className="flex items-center gap-3 p-2 rounded hover:bg-white/5 cursor-pointer"
                                        >
                                            <input
                                                type="checkbox"
                                                checked={selectedToolServers.includes(server.name)}
                                                onChange={(e) => {
                                                    const current = form.getValues('tool_servers');
                                                    if (e.target.checked) {
                                                        form.setValue('tool_servers', [...current, server.name], { shouldDirty: true, shouldValidate: true });
                                                    } else {
                                                        form.setValue('tool_servers', current.filter((n) => n !== server.name), { shouldDirty: true, shouldValidate: true });
                                                    }
                                                }}
                                                className="accent-primary"
                                            />
                                            <div className="flex items-center gap-2">
                                                <span className="text-sm font-medium">{server.name}</span>
                                                <Badge
                                                    variant={server.status === 'active' ? 'default' : 'secondary'}
                                                    className={
                                                        server.status === 'active'
                                                            ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30 text-[10px] px-1.5 py-0'
                                                            : 'bg-zinc-500/20 text-zinc-400 border-zinc-500/30 text-[10px] px-1.5 py-0'
                                                    }
                                                >
                                                    {server.status}
                                                </Badge>
                                                <span className="text-muted-foreground text-xs">— {server.url}</span>
                                            </div>
                                        </label>
                                    ))}
                                </div>
                            )}
                        </div>

                        <div className="space-y-3">
                            <span className="text-xs font-medium uppercase tracking-widest text-muted-foreground">Sandbox (Code Execution)</span>
                            <div className="p-3 border border-border rounded-none bg-black/30 space-y-4">
                                <FormField
                                    control={form.control}
                                    name="sandbox_enabled"
                                    render={({ field }) => (
                                        <FormItem className="flex flex-row items-center gap-3">
                                            <FormControl>
                                                <Checkbox
                                                    className="rounded-none border-primary data-[state=checked]:bg-primary data-[state=checked]:text-black"
                                                    checked={field.value}
                                                    onCheckedChange={field.onChange}
                                                />
                                            </FormControl>
                                            <div>
                                                <FormLabel className="font-normal font-mono cursor-pointer text-sm">
                                                    Enable Sandbox
                                                </FormLabel>
                                                <p className="text-xs text-muted-foreground mt-0.5">
                                                    Provision an E2B sandbox for code execution. Required for file input.
                                                </p>
                                            </div>
                                        </FormItem>
                                    )}
                                />

                                {sandboxEnabled && (
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2">
                                        <FormField
                                            control={form.control}
                                            name="sandbox_template"
                                            render={({ field }) => (
                                                <FormItem className="md:col-span-2">
                                                    <FormLabel className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">Template</FormLabel>
                                                    <FormControl>
                                                        <Input
                                                            className="rounded-none border-border bg-black/50 w-full"
                                                            placeholder="e.g., python-3.11"
                                                            {...field}
                                                        />
                                                    </FormControl>
                                                    <FormMessage className="text-destructive font-bold text-xs" />
                                                </FormItem>
                                            )}
                                        />
                                        <FormField
                                            control={form.control}
                                            name="sandbox_vcpu"
                                            render={({ field }) => (
                                                <FormItem>
                                                    <FormLabel className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">vCPU (1-8)</FormLabel>
                                                    <FormControl>
                                                        <Input
                                                            type="number"
                                                            min="1"
                                                            max="8"
                                                            step="1"
                                                            className="rounded-none border-border bg-black/50 w-full"
                                                            {...field}
                                                            onChange={(e) => field.onChange(parseInt(e.target.value, 10) || 1)}
                                                        />
                                                    </FormControl>
                                                    <FormMessage className="text-destructive font-bold text-xs" />
                                                </FormItem>
                                            )}
                                        />
                                        <FormField
                                            control={form.control}
                                            name="sandbox_memory_mb"
                                            render={({ field }) => (
                                                <FormItem>
                                                    <FormLabel className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">Memory MB (512-8192)</FormLabel>
                                                    <FormControl>
                                                        <Input
                                                            type="number"
                                                            min="512"
                                                            max="8192"
                                                            step="256"
                                                            className="rounded-none border-border bg-black/50 w-full"
                                                            {...field}
                                                            onChange={(e) => field.onChange(parseInt(e.target.value, 10) || 512)}
                                                        />
                                                    </FormControl>
                                                    <FormMessage className="text-destructive font-bold text-xs" />
                                                </FormItem>
                                            )}
                                        />
                                        <FormField
                                            control={form.control}
                                            name="sandbox_timeout_seconds"
                                            render={({ field }) => (
                                                <FormItem className="md:col-span-2">
                                                    <FormLabel className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">Sandbox Lifetime (seconds)</FormLabel>
                                                    <FormControl>
                                                        <Input
                                                            type="number"
                                                            min="60"
                                                            max="86400"
                                                            step="60"
                                                            className="rounded-none border-border bg-black/50 w-full"
                                                            {...field}
                                                            onChange={(e) => field.onChange(parseInt(e.target.value, 10) || 60)}
                                                        />
                                                    </FormControl>
                                                    <p className="text-xs text-muted-foreground mt-1">
                                                        The sandbox stays alive while the task is running. This timeout only applies after
                                                        a crash — if no one redrives within this window, the sandbox and its files are lost.
                                                        Default: 1 hour.
                                                    </p>
                                                    <FormMessage className="text-destructive font-bold text-xs" />
                                                </FormItem>
                                            )}
                                        />
                                    </div>
                                )}
                            </div>
                        </div>

                        <div className="space-y-3">
                            <span className="text-xs font-medium uppercase tracking-widest text-muted-foreground">Memory</span>
                            <div className="p-3 border border-border rounded-none bg-black/30 space-y-4">
                                <FormField
                                    control={form.control}
                                    name="memory_enabled"
                                    render={({ field }) => (
                                        <FormItem className="flex flex-row items-center gap-3">
                                            <FormControl>
                                                <Checkbox
                                                    className="rounded-none border-primary data-[state=checked]:bg-primary data-[state=checked]:text-black"
                                                    checked={field.value}
                                                    onCheckedChange={field.onChange}
                                                />
                                            </FormControl>
                                            <div>
                                                <FormLabel className="font-normal font-mono cursor-pointer text-sm">
                                                    Enable Memory
                                                </FormLabel>
                                                <p className="text-xs text-muted-foreground mt-0.5">
                                                    Persist cross-task memory entries for this agent and unlock memory tools in the Console.
                                                </p>
                                            </div>
                                        </FormItem>
                                    )}
                                />

                                {memoryEnabled && (
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2">
                                        <FormField
                                            control={form.control}
                                            name="memory_summarizer_model"
                                            render={({ field }) => (
                                                <FormItem>
                                                    <FormLabel className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">Summarizer Model</FormLabel>
                                                    <FormControl>
                                                        <Input
                                                            aria-label="Summarizer Model"
                                                            className="rounded-none border-border bg-black/50 w-full"
                                                            placeholder="e.g., claude-3-5-haiku-latest"
                                                            {...field}
                                                        />
                                                    </FormControl>
                                                    <p className="text-xs text-muted-foreground mt-1">
                                                        Leave blank to use the runtime-configured platform default summarizer. If the platform does not override it, the worker falls back to
                                                        {' '}
                                                        <code>claude-haiku-4-5</code>
                                                        .
                                                    </p>
                                                    <FormMessage className="text-destructive font-bold text-xs" />
                                                </FormItem>
                                            )}
                                        />
                                        <FormField
                                            control={form.control}
                                            name="memory_max_entries"
                                            render={({ field }) => (
                                                <FormItem>
                                                    <FormLabel className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">Max Entries</FormLabel>
                                                    <FormControl>
                                                        <Input
                                                            type="number"
                                                            min="100"
                                                            max="100000"
                                                            step="100"
                                                            aria-label="Max Entries"
                                                            className="rounded-none border-border bg-black/50 w-full"
                                                            placeholder="10000"
                                                            {...field}
                                                        />
                                                    </FormControl>
                                                    <p className="text-xs text-muted-foreground mt-1">
                                                        Optional retention cap. When omitted, the platform default of
                                                        {' '}
                                                        <code>10,000</code>
                                                        {' '}
                                                        entries is used.
                                                    </p>
                                                    <FormMessage className="text-destructive font-bold text-xs" />
                                                </FormItem>
                                            )}
                                        />
                                    </div>
                                )}
                            </div>
                        </div>

                        <ContextManagementSection
                            value={ctxMgmt}
                            memoryEnabled={memoryEnabled}
                            availableSummarizerModels={providerFilteredModels}
                            onChange={handleCtxMgmtChange}
                        />

                        <div className="space-y-3">
                            <span className="text-xs font-medium uppercase tracking-widest text-muted-foreground">Scheduling & Budget</span>
                            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                                <FormField
                                    control={form.control}
                                    name="max_concurrent_tasks"
                                    render={({ field }) => (
                                        <FormItem>
                                            <FormLabel className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">Max Concurrent Tasks</FormLabel>
                                            <FormControl>
                                                <Input
                                                    type="number"
                                                    min="1"
                                                    step="1"
                                                    className="rounded-none border-border bg-black/50 w-full"
                                                    {...field}
                                                    onChange={(e) => field.onChange(parseInt(e.target.value, 10) || 1)}
                                                />
                                            </FormControl>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
                                />
                                <FormField
                                    control={form.control}
                                    name="budget_max_per_task"
                                    render={({ field }) => (
                                        <FormItem>
                                            <FormLabel className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">Budget/Task</FormLabel>
                                            <FormControl>
                                                <Input
                                                    type="number"
                                                    min="1"
                                                    step="1"
                                                    className="rounded-none border-border bg-black/50 w-full"
                                                    {...field}
                                                    onChange={(e) => field.onChange(parseInt(e.target.value, 10) || 1)}
                                                />
                                            </FormControl>
                                            <p className="text-xs text-muted-foreground mt-1">${formatUsd(field.value)}</p>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
                                />
                                <FormField
                                    control={form.control}
                                    name="budget_max_per_hour"
                                    render={({ field }) => (
                                        <FormItem>
                                            <FormLabel className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">Budget/Hour</FormLabel>
                                            <FormControl>
                                                <Input
                                                    type="number"
                                                    min="1"
                                                    step="1"
                                                    className="rounded-none border-border bg-black/50 w-full"
                                                    {...field}
                                                    onChange={(e) => field.onChange(parseInt(e.target.value, 10) || 1)}
                                                />
                                            </FormControl>
                                            <p className="text-xs text-muted-foreground mt-1">${formatUsd(field.value)}</p>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
                                />
                            </div>
                        </div>

                        </div>

                        <div className="shrink-0 px-6 py-4 border-t border-white/5 bg-background mt-auto">
                            <DialogFooter>
                                <Button
                                    type="button"
                                    variant="ghost"
                                    onClick={() => onOpenChange(false)}
                                    className="uppercase tracking-widest text-xs"
                                >
                                    Cancel
                                </Button>
                                <Button
                                    type="submit"
                                    disabled={mutation.isPending}
                                    className="font-bold uppercase tracking-widest px-6 hover:saturate-150 transition-all"
                                >
                                    {mutation.isPending ? 'Creating...' : 'Create'}
                                </Button>
                            </DialogFooter>
                        </div>
                    </form>
                </Form>
            </DialogContent>
        </Dialog>
    );
}
