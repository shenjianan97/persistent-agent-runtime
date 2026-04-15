import { useNavigate, useSearchParams, Link } from 'react-router';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { useSubmitTask } from './useSubmitTask';
import { useAgents, useAgent } from '@/features/agents/useAgents';
import { useLangfuseEndpoints } from '@/features/settings/useLangfuseEndpoints';
import { submitTaskSchema, SubmitTaskFormValues, ALL_TOOL_LABELS } from './schema';
import { FileAttachment } from './FileAttachment';
import { toast } from 'sonner';
import { formatUsd } from '@/lib/utils';
import { useState, useEffect } from 'react';


import {
    Form, FormControl, FormDescription, FormField, FormItem, FormLabel, FormMessage
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { PlaySquare, AlertCircle, Bot } from 'lucide-react';

export function SubmitTaskPage() {
    const navigate = useNavigate();
    const [searchParams] = useSearchParams();
    const queryAgentId = searchParams.get('agent_id') || '';

    const mutation = useSubmitTask();
    const { data: agents = [], isLoading: isLoadingAgents } = useAgents('active');
    const { data: langfuseEndpoints = [] } = useLangfuseEndpoints();

    const [queryParamError, setQueryParamError] = useState<string | null>(null);
    const [attachedFiles, setAttachedFiles] = useState<File[]>([]);

    const form = useForm<SubmitTaskFormValues>({
        resolver: zodResolver(submitTaskSchema),
        defaultValues: {
            agent_id: queryAgentId || '',
            input: '',
            max_steps: 100,
            max_retries: 3,
            task_timeout_seconds: import.meta.env.VITE_DEV_TASK_CONTROLS_ENABLED === 'true' ? 60 : 3600,
        },
    });

    const selectedAgentId = form.watch('agent_id');

    const { data: selectedAgent, isLoading: isLoadingAgent } = useAgent(selectedAgentId);

    const sandboxEnabled = selectedAgent?.agent_config?.sandbox?.enabled === true;

    // Validate query param agent_id against loaded agents
    useEffect(() => {
        if (!queryAgentId || isLoadingAgents) return;

        const match = agents.find(a => a.agent_id === queryAgentId);
        if (!match) {
            setQueryParamError(
                `Agent "${queryAgentId}" was not found or is not active. Please select another agent.`
            );
            form.setValue('agent_id', '');
        } else {
            setQueryParamError(null);
        }
    }, [queryAgentId, agents, isLoadingAgents, form]);

    function onSubmit(data: SubmitTaskFormValues) {
        mutation.mutate(
            {
                request: data,
                files: attachedFiles.length > 0 ? attachedFiles : undefined,
            },
            {
                onSuccess: (response) => {
                    toast.success(`Task ${response.task_id} submitted`, {
                        description: attachedFiles.length > 0
                            ? `Execution initialized with ${attachedFiles.length} file(s).`
                            : "Execution initialized.",
                    });
                    setAttachedFiles([]);
                    navigate(`/tasks/${response.task_id}`);
                },
                onError: (error: Error) => {
                    toast.error("Submission failed", {
                        description: error.message || "Unknown error occurred.",
                    });
                },
            }
        );
    }

    const noAgentsExist = !isLoadingAgents && agents.length === 0;

    return (
        <div className="space-y-6 animate-in fade-in duration-500">
            <div className="console-surface-strong rounded-[28px] p-6 md:p-7 mb-8">
                <h2 className="text-3xl font-display font-semibold tracking-tight mb-2 flex items-center gap-2">
                    <PlaySquare className="w-6 h-6 text-primary drop-shadow-[0_0_12px_var(--color-primary)]" />
                    Submit Task
                </h2>
                <p className="text-muted-foreground w-full md:w-2/3">
                    Select an agent and provide task-level inputs. The task will be queued and picked up by an available worker.
                </p>
            </div>

            {/* Query param error */}
            {queryParamError && (
                <div className="mb-6 flex items-start gap-3 p-4 rounded-lg border border-destructive/50 bg-destructive/10 text-destructive text-sm">
                    <AlertCircle className="w-5 h-5 shrink-0 mt-0.5" />
                    <span>{queryParamError}</span>
                </div>
            )}

            {/* Empty state: no agents exist */}
            {noAgentsExist && (
                <Card className="console-surface border-white/10">
                    <CardContent className="py-16 flex flex-col items-center justify-center gap-4 text-center">
                        <Bot className="w-12 h-12 text-muted-foreground opacity-30" />
                        <div>
                            <p className="text-sm font-semibold text-muted-foreground uppercase tracking-widest mb-2">No agents exist yet</p>
                            <p className="text-sm text-muted-foreground">Create an agent before submitting a task.</p>
                        </div>
                        <Link
                            to="/agents"
                            className="mt-2 inline-flex items-center gap-2 px-4 py-2 text-sm font-bold uppercase tracking-widest border border-primary text-primary hover:bg-primary hover:text-black transition-colors"
                        >
                            Go to Agents
                        </Link>
                    </CardContent>
                </Card>
            )}

            {!noAgentsExist && (
                <Form {...form}>
                    <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-6">
                        {/* Agent Selection */}
                        <Card className="console-surface border-white/10">
                            <CardHeader className="border-b border-white/8 pb-3">
                                <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Agent Selection</CardTitle>
                            </CardHeader>
                            <CardContent className="pt-2 space-y-6">
                                <FormField
                                    control={form.control}
                                    name="agent_id"
                                    render={() => (
                                        <FormItem>
                                            <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Agent</FormLabel>
                                            <FormControl>
                                                <select
                                                    className="flex h-10 w-full border border-border bg-black/50 px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary focus-visible:ring-offset-0 disabled:cursor-not-allowed disabled:opacity-50 rounded-none appearance-none"
                                                    value={selectedAgentId}
                                                    onChange={(e) => {
                                                        form.setValue('agent_id', e.target.value);
                                                        setQueryParamError(null);
                                                    }}
                                                >
                                                    <option value="" disabled>
                                                        {isLoadingAgents ? 'Loading agents...' : 'Select an agent'}
                                                    </option>
                                                    {agents.map((agent) => (
                                                        <option key={agent.agent_id} value={agent.agent_id}>
                                                            {agent.display_name} ({agent.agent_id})
                                                        </option>
                                                    ))}
                                                </select>
                                            </FormControl>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
                                />

                                {/* Agent Config Preview */}
                                {selectedAgentId && (
                                    <div className="rounded-lg bg-muted/10 border border-white/5 p-4 space-y-3">
                                        <div className="text-xs font-semibold uppercase tracking-widest text-muted-foreground mb-3">Agent Configuration (read-only)</div>
                                        {isLoadingAgent ? (
                                            <div className="text-xs text-muted-foreground animate-pulse uppercase tracking-widest">Loading agent config...</div>
                                        ) : selectedAgent ? (
                                            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs font-mono">
                                                <div>
                                                    <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">Provider / Model</span>
                                                    <span className="text-foreground">{selectedAgent.agent_config.provider} / {selectedAgent.agent_config.model}</span>
                                                </div>
                                                <div>
                                                    <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">Temperature</span>
                                                    <span className="text-foreground">{selectedAgent.agent_config.temperature}</span>
                                                </div>
                                                <div className="md:col-span-2">
                                                    <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">System Prompt</span>
                                                    <span className="text-foreground/80 whitespace-pre-wrap line-clamp-3">{selectedAgent.agent_config.system_prompt}</span>
                                                </div>
                                                {(() => {
                                                    const autoManaged = new Set(['request_human_input', 'web_search', 'read_url', 'create_text_artifact', 'sandbox_exec', 'sandbox_read_file', 'sandbox_write_file', 'export_sandbox_file']);
                                                    const userTools = (selectedAgent.agent_config.allowed_tools ?? []).filter(t => !autoManaged.has(t));
                                                    return userTools.length > 0 ? (
                                                        <div className="md:col-span-2">
                                                            <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">Tools</span>
                                                            <span className="text-foreground">{userTools.map(id => ALL_TOOL_LABELS[id] ?? id).join(', ')}</span>
                                                        </div>
                                                    ) : null;
                                                })()}
                                                {selectedAgent.agent_config.tool_servers && selectedAgent.agent_config.tool_servers.length > 0 && (
                                                    <div className="md:col-span-2">
                                                        <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">Tool Servers</span>
                                                        <span className="text-foreground">{selectedAgent.agent_config.tool_servers.join(', ')}</span>
                                                    </div>
                                                )}
                                                <div>
                                                    <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">Max Concurrent Tasks</span>
                                                    <span className="text-foreground">{selectedAgent.max_concurrent_tasks}</span>
                                                </div>
                                                <div>
                                                    <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">Budget/Task</span>
                                                    <span className="text-foreground">${formatUsd(selectedAgent.budget_max_per_task)}</span>
                                                </div>
                                                <div>
                                                    <span className="text-muted-foreground block mb-1 uppercase tracking-widest text-[10px]">Budget/Hour</span>
                                                    <span className="text-foreground">${formatUsd(selectedAgent.budget_max_per_hour)}</span>
                                                </div>
                                            </div>
                                        ) : (
                                            <div className="text-xs text-destructive">Failed to load agent configuration.</div>
                                        )}
                                    </div>
                                )}
                            </CardContent>
                        </Card>

                        {/* Task Inputs */}
                        <Card className="console-surface border-white/10">
                            <CardHeader className="border-b border-white/8 pb-3">
                                <CardTitle className="text-sm font-display uppercase tracking-widest">Task Input</CardTitle>
                            </CardHeader>
                            <CardContent className="pt-2 space-y-6">
                                <FormField
                                    control={form.control}
                                    name="input"
                                    render={({ field }) => (
                                        <FormItem>
                                            <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Input Directive</FormLabel>
                                            <FormControl>
                                                <Textarea
                                                    className="min-h-[150px] resize-y rounded-none border-border bg-black/50 focus-visible:ring-primary border-b-[3px] focus-visible:border-b-primary focus-visible:ring-0"
                                                    placeholder="What is 2+2?"
                                                    {...field}
                                                />
                                            </FormControl>
                                            <FormDescription className="text-xs text-muted-foreground mt-2">The actual instruction or context for the agent.</FormDescription>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
                                />
                            </CardContent>
                        </Card>

                        {/* File Attachments */}
                        <Card className="console-surface border-white/10">
                            <CardHeader className="border-b border-white/8 pb-3">
                                <CardTitle className="text-sm font-display uppercase tracking-widest">File Attachments</CardTitle>
                            </CardHeader>
                            <CardContent className="pt-4">
                                <FileAttachment
                                    files={attachedFiles}
                                    onFilesChange={setAttachedFiles}
                                    disabled={!sandboxEnabled}
                                    disabledReason={
                                        !selectedAgentId
                                            ? "Select an agent first"
                                            : !sandboxEnabled
                                            ? "File upload requires an agent with sandbox enabled"
                                            : undefined
                                    }
                                />
                            </CardContent>
                        </Card>

                        {/* Execution Parameters */}
                        <Card className="console-surface border-white/10">
                            <CardHeader className="border-b border-white/8 pb-3">
                                <CardTitle className="text-sm font-display uppercase tracking-widest">Execution Parameters</CardTitle>
                            </CardHeader>
                            <CardContent className="pt-2">
                                <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                                    <FormField
                                        control={form.control}
                                        name="max_steps"
                                        render={({ field }) => (
                                            <FormItem>
                                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Max Steps</FormLabel>
                                                <FormControl>
                                                    <Input type="number" min="1" max="1000" className="rounded-none border-border bg-black/50" {...field} onChange={e => field.onChange(parseInt(e.target.value, 10))} />
                                                </FormControl>
                                                <FormMessage className="text-destructive font-bold text-xs" />
                                            </FormItem>
                                        )}
                                    />

                                    <FormField
                                        control={form.control}
                                        name="max_retries"
                                        render={({ field }) => (
                                            <FormItem>
                                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Max Retries</FormLabel>
                                                <FormControl>
                                                    <Input type="number" min="0" max="10" className="rounded-none border-border bg-black/50" {...field} onChange={e => field.onChange(parseInt(e.target.value, 10))} />
                                                </FormControl>
                                                <FormMessage className="text-destructive font-bold text-xs" />
                                            </FormItem>
                                        )}
                                    />

                                    <FormField
                                        control={form.control}
                                        name="task_timeout_seconds"
                                        render={({ field }) => (
                                            <FormItem>
                                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Timeout (s)</FormLabel>
                                                <FormControl>
                                                    <Input type="number" min={import.meta.env.VITE_DEV_TASK_CONTROLS_ENABLED === 'true' ? "1" : "60"} max="86400" className="rounded-none border-border bg-black/50" {...field} onChange={e => field.onChange(parseInt(e.target.value, 10))} />
                                                </FormControl>
                                                <FormDescription className="text-xs text-muted-foreground">
                                                    {import.meta.env.VITE_DEV_TASK_CONTROLS_ENABLED === 'true'
                                                        ? 'Dev task controls enabled: short timeouts are allowed for local recovery testing.'
                                                        : 'Minimum timeout is 60 seconds.'}
                                                </FormDescription>
                                                <FormMessage className="text-destructive font-bold text-xs" />
                                            </FormItem>
                                        )}
                                    />
                                </div>
                            </CardContent>
                        </Card>

                        {/* Observability */}
                        <Card className="console-surface border-white/10">
                            <CardHeader className="border-b border-white/8 pb-3">
                                <CardTitle className="text-sm font-display uppercase tracking-widest">Observability</CardTitle>
                            </CardHeader>
                            <CardContent className="pt-2">
                                <FormField
                                    control={form.control}
                                    name="langfuse_endpoint_id"
                                    render={({ field }) => (
                                        <FormItem>
                                            <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Langfuse Endpoint</FormLabel>
                                            <FormControl>
                                                <select
                                                    className="flex h-10 w-full border border-border bg-black/50 px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary focus-visible:ring-offset-0 disabled:cursor-not-allowed disabled:opacity-50 rounded-none appearance-none"
                                                    value={field.value ?? ''}
                                                    onChange={(e) => field.onChange(e.target.value || undefined)}
                                                >
                                                    <option value="">None</option>
                                                    {langfuseEndpoints.map((ep) => (
                                                        <option key={ep.endpoint_id} value={ep.endpoint_id}>
                                                            {ep.name} ({ep.host})
                                                        </option>
                                                    ))}
                                                </select>
                                            </FormControl>
                                            <FormDescription className="text-xs text-muted-foreground mt-2">
                                                {langfuseEndpoints.length === 0
                                                    ? 'No endpoints configured \u2014 set up in Settings'
                                                    : 'Optional: send execution traces to a Langfuse instance'}
                                            </FormDescription>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
                                />
                            </CardContent>
                        </Card>

                        <div className="flex justify-end pt-4 pb-12">
                            <Button
                                type="submit"
                                disabled={mutation.isPending || !selectedAgentId}
                                className="rounded-none font-bold uppercase tracking-widest px-8 hover:saturate-150 transition-all border border-primary text-black"
                            >
                                {mutation.isPending ? "INITIALIZING..." : "SUBMIT TASK"}
                            </Button>
                        </div>
                    </form>
                </Form>
            )}
        </div>
    );
}
