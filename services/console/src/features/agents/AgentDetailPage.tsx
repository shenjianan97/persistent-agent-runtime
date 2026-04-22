import { useParams, useLocation, Link } from 'react-router';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { useAgent, useUpdateAgent } from './useAgents';
import { useModels } from '@/features/submit/useModels';
import { ALL_TOOL_LABELS, HUMAN_INPUT_TOOL_ID } from '@/features/submit/schema';
import { formatProviderLabel, groupModelsByProvider } from '@/lib/models';
import { toast } from 'sonner';
import { useCallback, useEffect, useRef, useState } from 'react';
import { formatUsd } from '@/lib/utils';
import { useToolServers } from '../tool-servers/useToolServers';
import { MemoryTab } from './memory/MemoryTab';
import { ContextManagementSection } from './ContextManagementSection';
import type { ContextManagementConfig } from './ContextManagementSection';

import {
    Form, FormControl, FormField, FormItem, FormLabel, FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Bot, Pencil, X } from 'lucide-react';

const MEMORY_MAX_ENTRIES_PLATFORM_DEFAULT = 10_000;
const MEMORY_SUMMARIZER_PLATFORM_DEFAULT = 'Platform default (runtime-configured; fallback: claude-haiku-4-5)';

const agentDetailSchema = z.object({
    display_name: z.string().min(1, 'Agent name is required').max(200),
    system_prompt: z.string().min(1, 'System prompt is required').max(51200),
    provider: z.string().min(1, 'Provider is required'),
    model: z.string().min(1, 'Model is required'),
    temperature: z.number().min(0).max(2).default(0.7),
    tool_servers: z.array(z.string()).default([]),
    status: z.enum(['active', 'disabled']),
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

type AgentDetailFormValues = z.infer<typeof agentDetailSchema>;

export function AgentDetailPage() {
    const { agentId } = useParams<{ agentId: string }>();
    const location = useLocation();
    const { data: agent, isLoading, error } = useAgent(agentId!);
    const mutation = useUpdateAgent();
    const { data: models = [], isLoading: isLoadingModels } = useModels();
    const modelGroups = groupModelsByProvider(models);
    const [isEditing, setIsEditing] = useState(false);
    const { data: toolServers = [] } = useToolServers('active');

    // context_management is managed outside react-hook-form to preserve
    // don't-send-defaults semantics: only include in PUT when the user
    // has explicitly changed a field (or an existing value was loaded).
    const [ctxMgmt, setCtxMgmt] = useState<ContextManagementConfig | undefined>(undefined);
    // Track the value that was loaded from the server so we can detect changes.
    const loadedCtxMgmt = useRef<ContextManagementConfig | undefined>(undefined);
    const ctxMgmtDirty = useRef(false);

    // The memory tab mounts when the current route is `/agents/:id/memory[/:memoryId]`.
    const basePath = agentId ? `/agents/${encodeURIComponent(agentId)}` : '';
    const onMemoryRoute = !!agentId && location.pathname.startsWith(`${basePath}/memory`);
    const memoryEnabled = agent?.agent_config?.memory?.enabled === true;
    // The tab strip is visible whenever memory is enabled OR the user is
    // already on the memory route (historical entries when memory is disabled).
    const showMemoryTab = memoryEnabled || onMemoryRoute;

    const form = useForm<AgentDetailFormValues>({
        resolver: zodResolver(agentDetailSchema),
        defaultValues: {
            display_name: '',
            system_prompt: '',
            provider: '',
            model: '',
            temperature: 0.7,
            tool_servers: [],
            status: 'active',
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

    useEffect(() => {
        if (agent) {
            form.reset({
                display_name: agent.display_name,
                system_prompt: agent.agent_config.system_prompt,
                provider: agent.agent_config.provider,
                model: agent.agent_config.model,
                temperature: agent.agent_config.temperature,
                tool_servers: agent.agent_config.tool_servers ?? [],
                status: agent.status,
                max_concurrent_tasks: agent.max_concurrent_tasks ?? 5,
                budget_max_per_task: agent.budget_max_per_task ?? 500000,
                budget_max_per_hour: agent.budget_max_per_hour ?? 5000000,
                sandbox_enabled: agent.agent_config.sandbox?.enabled ?? false,
                sandbox_template: agent.agent_config.sandbox?.template ?? '',
                sandbox_vcpu: agent.agent_config.sandbox?.vcpu ?? 2,
                sandbox_memory_mb: agent.agent_config.sandbox?.memory_mb ?? 2048,
                sandbox_timeout_seconds: agent.agent_config.sandbox?.timeout_seconds ?? 3600,
                memory_enabled: agent.agent_config.memory?.enabled ?? false,
                memory_summarizer_model: agent.agent_config.memory?.summarizer_model ?? '',
                memory_max_entries: agent.agent_config.memory?.max_entries?.toString() ?? '',
            });
            // Reset context_management from the loaded agent data.
            const loaded = agent.agent_config.context_management;
            loadedCtxMgmt.current = loaded;
            setCtxMgmt(loaded);
            ctxMgmtDirty.current = false;
        }
    }, [agent, form]);

    const handleCtxMgmtChange = useCallback((next: ContextManagementConfig) => {
        ctxMgmtDirty.current = true;
        setCtxMgmt(next);
    }, []);

    useEffect(() => {
        if (!ctxMgmt?.summarizer_model) return;
        const stillValid = models.some(
            (m) => m.model_id === ctxMgmt.summarizer_model
                && (!ctxMgmt.summarizer_provider || m.provider === ctxMgmt.summarizer_provider)
        );
        if (!stillValid) {
            ctxMgmtDirty.current = true;
            setCtxMgmt({
                ...ctxMgmt,
                summarizer_model: undefined,
                summarizer_provider: undefined,
            });
        }
    }, [models, ctxMgmt]);

    function onSubmit(data: AgentDetailFormValues) {
        if (!agentId) return;
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
        const hasExistingMemoryConfig = !!agent?.agent_config?.memory;
        const hasMemoryConfig = hasExistingMemoryConfig || data.memory_enabled || !!summarizerModel || parsedMaxEntries !== undefined;
        const memoryConfig = hasMemoryConfig
            ? {
                enabled: data.memory_enabled,
                ...(summarizerModel ? { summarizer_model: summarizerModel } : {}),
                ...(parsedMaxEntries !== undefined ? { max_entries: parsedMaxEntries } : {}),
            }
            : undefined;

        // Include context_management in the payload only if:
        //  (a) it was loaded from the server (preserve existing config), OR
        //  (b) the user has explicitly changed a field in this edit session.
        // When included, always serialize pre_tier3_memory_flush explicitly so
        // the saved value matches what the user sees in the UI — the worker
        // defaults missing values to `true`, which would silently diverge.
        const hasExistingCtxMgmt = !!loadedCtxMgmt.current;
        const shouldSendCtxMgmt = hasExistingCtxMgmt || ctxMgmtDirty.current;
        const summarizer = ctxMgmt?.summarizer_model?.trim();
        const summarizerProvider = ctxMgmt?.summarizer_provider?.trim();
        const excludeTools = ctxMgmt?.exclude_tools ?? [];
        const contextManagementPayload: ContextManagementConfig | undefined = shouldSendCtxMgmt
            ? {
                ...(summarizer ? { summarizer_model: summarizer } : {}),
                ...(summarizer && summarizerProvider ? { summarizer_provider: summarizerProvider } : {}),
                ...(excludeTools.length ? { exclude_tools: excludeTools } : {}),
                pre_tier3_memory_flush: !!ctxMgmt?.pre_tier3_memory_flush,
            }
            : undefined;

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
                        tool_servers: data.tool_servers,
                        ...(sandboxConfig ? { sandbox: sandboxConfig } : {}),
                        ...(memoryConfig ? { memory: memoryConfig } : {}),
                        ...(contextManagementPayload ? { context_management: contextManagementPayload } : {}),
                    },
                    status: data.status,
                    max_concurrent_tasks: data.max_concurrent_tasks,
                    budget_max_per_task: data.budget_max_per_task,
                    budget_max_per_hour: data.budget_max_per_hour,
                },
            },
            {
                onSuccess: () => {
                    toast.success('Agent updated', {
                        description: 'Configuration saved successfully.',
                    });
                    setIsEditing(false);
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
            <div className="space-y-6 animate-in fade-in duration-500">
                <div className="console-surface-strong rounded-[28px] p-6 md:p-7">
                    <span className="uppercase tracking-widest text-xs font-bold text-muted-foreground animate-pulse">Loading agent...</span>
                </div>
            </div>
        );
    }

    if (error || !agent) {
        return (
            <div className="space-y-6 animate-in fade-in duration-500">
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
    const selectedToolServers = form.watch('tool_servers');
    const sandboxEnabled = form.watch('sandbox_enabled');
    const memoryEnabledInForm = form.watch('memory_enabled');

    function handleCancel() {
        form.reset();
        setCtxMgmt(loadedCtxMgmt.current);
        ctxMgmtDirty.current = false;
        setIsEditing(false);
    }

    // Platform-managed tools are auto-added by the API — don't clutter the overview
    const AUTO_MANAGED_TOOLS = new Set([
        HUMAN_INPUT_TOOL_ID,
        'web_search', 'read_url', 'create_text_artifact',
        'sandbox_exec', 'sandbox_read_file', 'sandbox_write_file', 'export_sandbox_file',
    ]);
    const toolLabels = (agent.agent_config.allowed_tools ?? [])
        .filter(t => !AUTO_MANAGED_TOOLS.has(t))
        .map(id => ALL_TOOL_LABELS[id] ?? id);

    const readOnlyField = (label: string, value: React.ReactNode) => (
        <div>
            <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">{label}</span>
            <span className="text-foreground text-sm font-mono">{value}</span>
        </div>
    );

    const tabBaseClass = 'px-4 py-2 text-xs font-bold uppercase tracking-widest transition-all border-b-2 -mb-px';
    const tabActiveClass = 'border-primary text-primary';
    const tabInactiveClass = 'border-transparent text-muted-foreground hover:text-foreground';

    return (
        <div className="space-y-6 animate-in fade-in duration-500">
            <div className="console-surface-strong rounded-[28px] p-6 md:p-7 flex flex-col md:flex-row md:items-end justify-between gap-4">
                <div>
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
                {!onMemoryRoute && !isEditing && (
                    <Button
                        onClick={() => setIsEditing(true)}
                        variant="outline"
                        className="font-bold uppercase tracking-widest px-6 border-primary text-primary hover:bg-primary hover:text-black transition-all"
                    >
                        <Pencil className="w-4 h-4 mr-2" />
                        Edit
                    </Button>
                )}
            </div>

            <nav
                className="flex gap-1 border-b border-white/8 px-2"
                role="tablist"
                aria-label="Agent detail sections"
                data-testid="agent-detail-tabs"
            >
                <Link
                    to={basePath}
                    role="tab"
                    aria-selected={!onMemoryRoute}
                    className={`${tabBaseClass} ${!onMemoryRoute ? tabActiveClass : tabInactiveClass}`}
                    data-testid="agent-tab-overview"
                >
                    Overview
                </Link>
                {showMemoryTab && (
                    <Link
                        to={`${basePath}/memory`}
                        role="tab"
                        aria-selected={onMemoryRoute}
                        className={`${tabBaseClass} ${onMemoryRoute ? tabActiveClass : tabInactiveClass}`}
                        data-testid="agent-tab-memory"
                    >
                        Memory
                    </Link>
                )}
            </nav>

            {onMemoryRoute ? (
                <MemoryTab />
            ) : !isEditing ? (
                <div className="space-y-6">
                    <Card className="console-surface border-white/10">
                        <CardHeader className="border-b border-white/8 pb-4">
                            <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Configuration</CardTitle>
                        </CardHeader>
                        <CardContent className="pt-6 space-y-5">
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                {readOnlyField('Agent Name', agent.display_name)}
                                {readOnlyField('Model', `${agent.agent_config.provider} / ${agent.agent_config.model}`)}
                            </div>
                            <div>
                                <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">System Prompt</span>
                                <span className="text-foreground/80 text-sm font-mono whitespace-pre-wrap">{agent.agent_config.system_prompt}</span>
                            </div>
                            {readOnlyField('Temperature', agent.agent_config.temperature)}
                            {toolLabels.length > 0 && readOnlyField('Tools', toolLabels.join(', '))}
                            {agent.agent_config.memory && (
                                <div className="grid grid-cols-1 md:grid-cols-3 gap-6 pt-4 border-t border-white/8">
                                    <div>
                                        <span
                                            data-testid="agent-memory-status-label"
                                            className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px] whitespace-nowrap"
                                        >
                                            Memory Status
                                        </span>
                                        <span className="text-foreground text-sm font-mono">
                                            {agent.agent_config.memory.enabled ? 'Enabled' : 'Disabled'}
                                        </span>
                                    </div>
                                    <div>
                                        <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">Summarizer Model</span>
                                        <span className="text-foreground text-sm font-mono">
                                            {agent.agent_config.memory.summarizer_model || MEMORY_SUMMARIZER_PLATFORM_DEFAULT}
                                        </span>
                                    </div>
                                    <div>
                                        <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">Max Entries</span>
                                        <span className="text-foreground text-sm font-mono">
                                            {agent.agent_config.memory.max_entries ?? MEMORY_MAX_ENTRIES_PLATFORM_DEFAULT.toLocaleString()}
                                        </span>
                                    </div>
                                </div>
                            )}
                            {agent.agent_config.sandbox?.enabled && (
                                <div className="space-y-2">
                                    <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">Sandbox</span>
                                    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm font-mono">
                                        <div><span className="text-muted-foreground text-[10px] uppercase block">Template</span>{agent.agent_config.sandbox.template}</div>
                                        <div><span className="text-muted-foreground text-[10px] uppercase block">vCPU</span>{agent.agent_config.sandbox.vcpu}</div>
                                        <div><span className="text-muted-foreground text-[10px] uppercase block">Memory</span>{agent.agent_config.sandbox.memory_mb} MB</div>
                                        <div><span className="text-muted-foreground text-[10px] uppercase block">Sandbox Lifetime</span>{agent.agent_config.sandbox.timeout_seconds}s</div>
                                    </div>
                                </div>
                            )}
                            {agent?.agent_config?.context_management && (
                                <div className="pt-4 border-t border-white/8 space-y-2">
                                    <div className="uppercase tracking-widest text-muted-foreground text-[10px]">Long-Running Task Context</div>
                                    <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                                        <div>
                                            <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">Summarizer Model</span>
                                            <span className="text-foreground text-sm font-mono">
                                                {agent.agent_config.context_management.summarizer_model
                                                    ? agent.agent_config.context_management.summarizer_provider
                                                        ? `${agent.agent_config.context_management.summarizer_model} (${formatProviderLabel(agent.agent_config.context_management.summarizer_provider)})`
                                                        : agent.agent_config.context_management.summarizer_model
                                                    : 'Platform default'}
                                            </span>
                                        </div>
                                        <div>
                                            <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">Always Keep Outputs From</span>
                                            <span className="text-foreground text-sm font-mono">
                                                {agent.agent_config.context_management.exclude_tools?.length
                                                    ? agent.agent_config.context_management.exclude_tools.join(', ')
                                                    : 'None'}
                                            </span>
                                        </div>
                                        <div>
                                            <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">Save Important Facts Before Summarizing</span>
                                            <span className="text-foreground text-sm font-mono">
                                                {agent.agent_config.context_management.pre_tier3_memory_flush ? 'Enabled' : 'Disabled'}
                                            </span>
                                        </div>
                                    </div>
                                </div>
                            )}
                            {agent?.agent_config?.tool_servers && agent.agent_config.tool_servers.length > 0 && (
                                <div className="space-y-2">
                                    <div className="uppercase tracking-widest text-muted-foreground text-[10px]">Tool Servers</div>
                                    <div className="flex flex-wrap gap-2">
                                        {agent.agent_config.tool_servers.map((name: string) => (
                                            <Badge key={name} variant="outline" className="border-primary/30 text-primary">
                                                {name}
                                            </Badge>
                                        ))}
                                    </div>
                                </div>
                            )}
                        </CardContent>
                    </Card>

                    <Card className="console-surface border-white/10">
                        <CardHeader className="border-b border-white/8 pb-4">
                            <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Scheduling & Budget</CardTitle>
                        </CardHeader>
                        <CardContent className="pt-6">
                            <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                                {readOnlyField('Max Concurrent Tasks', agent.max_concurrent_tasks)}
                                {readOnlyField('Budget/Task', `$${formatUsd(agent.budget_max_per_task)}`)}
                                {readOnlyField('Budget/Hour', `$${formatUsd(agent.budget_max_per_hour)}`)}
                            </div>
                        </CardContent>
                    </Card>

                    <Card className="console-surface border-white/10">
                        <CardHeader className="border-b border-white/8 pb-4">
                            <CardTitle className="text-sm font-display uppercase tracking-widest">Lifecycle</CardTitle>
                        </CardHeader>
                        <CardContent className="pt-6">
                            {readOnlyField('Status', agent.status)}
                            {agent.status === 'disabled' && (
                                <p className="text-xs text-amber-400 mt-2">Disabled agents cannot be used for new task submissions.</p>
                            )}
                        </CardContent>
                    </Card>
                </div>
            ) : (
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
                                                            Persist cross-task memory entries for this agent and expose the Memory tab in the Console.
                                                        </p>
                                                    </div>
                                                </FormItem>
                                            )}
                                        />

                                        {memoryEnabledInForm && (
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
                                    memoryEnabled={memoryEnabledInForm}
                                    availableSummarizerModels={models}
                                    onChange={handleCtxMgmtChange}
                                />
                            </CardContent>
                        </Card>

                        <Card className="console-surface border-white/10">
                            <CardHeader className="border-b border-white/8 pb-4">
                                <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Scheduling & Budget</CardTitle>
                            </CardHeader>
                            <CardContent className="pt-6 space-y-6">
                                <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                                    <FormField
                                        control={form.control}
                                        name="max_concurrent_tasks"
                                        render={({ field }) => (
                                            <FormItem>
                                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Max Concurrent Tasks</FormLabel>
                                                <FormControl>
                                                    <Input
                                                        type="number"
                                                        min="1"
                                                        step="1"
                                                        className="rounded-none border-border bg-black/50 w-32"
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
                                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Budget per Task (microdollars)</FormLabel>
                                                <FormControl>
                                                    <Input
                                                        type="number"
                                                        min="1"
                                                        step="1"
                                                        className="rounded-none border-border bg-black/50 w-48"
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
                                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Budget per Hour (microdollars)</FormLabel>
                                                <FormControl>
                                                    <Input
                                                        type="number"
                                                        min="1"
                                                        step="1"
                                                        className="rounded-none border-border bg-black/50 w-48"
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

                        <div className="flex justify-end gap-3 pt-4 pb-12">
                            <Button
                                type="button"
                                variant="ghost"
                                onClick={handleCancel}
                                className="uppercase tracking-widest text-xs"
                            >
                                <X className="w-4 h-4 mr-2" />
                                Cancel
                            </Button>
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
            )}
        </div>
    );
}
