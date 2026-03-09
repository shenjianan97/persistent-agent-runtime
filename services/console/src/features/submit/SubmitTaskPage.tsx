import { useNavigate } from 'react-router';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { useSubmitTask } from './useSubmitTask';
import { submitTaskSchema, SubmitTaskFormValues, SUPPORTED_MODELS, ALLOWED_TOOLS } from './schema';
import { toast } from 'sonner';

import {
    Form, FormControl, FormDescription, FormField, FormItem, FormLabel, FormMessage
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { PlaySquare } from 'lucide-react';

export function SubmitTaskPage() {
    const navigate = useNavigate();
    const mutation = useSubmitTask();

    const form = useForm<SubmitTaskFormValues>({
        resolver: zodResolver(submitTaskSchema),
        defaultValues: {
            agent_id: '',
            input: '',
            system_prompt: 'You are a helpful assistant. Provide concise and accurate answers.',
            model: 'claude-sonnet-4-20250514',
            temperature: 0.7,
            allowed_tools: ['web_search', 'read_url', 'calculator'],
            max_steps: 100,
            max_retries: 3,
            task_timeout_seconds: 3600,
        },
    });

    function onSubmit(data: SubmitTaskFormValues) {
        mutation.mutate(data, {
            onSuccess: (response) => {
                toast.success(`Task ${response.task_id} submitted`, {
                    description: "Execution initialized.",
                });
                navigate(`/tasks/${response.task_id}`);
            },
            onError: (error: Error) => {
                toast.error("Submission failed", {
                    description: error.message || "Unknown error occurred.",
                });
            }
        });
    }

    return (
        <div className="max-w-4xl mx-auto animate-in fade-in duration-500">
            <div className="mb-8">
                <h2 className="text-2xl font-display font-medium uppercase tracking-wider mb-2 flex items-center gap-2">
                    <PlaySquare className="w-6 h-6 text-primary" />
                    Dispatch Task
                </h2>
                <p className="text-muted-foreground w-full md:w-2/3">
                    Initialize a new durable execution job. The task will be queued and picked up by an available worker.
                </p>
            </div>

            <Form {...form}>
                <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-6">
                    <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
                        <CardHeader className="border-b border-border/40 pb-4">
                            <CardTitle className="text-sm font-display uppercase tracking-widest text-primary">Identity & Prompt</CardTitle>
                        </CardHeader>
                        <CardContent className="pt-6 space-y-6">
                            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                <FormField
                                    control={form.control}
                                    name="agent_id"
                                    render={({ field }) => (
                                        <FormItem>
                                            <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Agent ID</FormLabel>
                                            <FormControl>
                                                <Input className="rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1" placeholder="e.g., e2e-test" {...field} />
                                            </FormControl>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
                                />
                                <FormField
                                    control={form.control}
                                    name="model"
                                    render={({ field }) => (
                                        <FormItem>
                                            <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Model</FormLabel>
                                            <FormControl>
                                                <select
                                                    className="flex h-10 w-full border border-border bg-black/50 px-3 py-2 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary focus-visible:ring-offset-0 disabled:cursor-not-allowed disabled:opacity-50 rounded-none appearance-none"
                                                    {...field}
                                                >
                                                    <option value="" disabled>Select model</option>
                                                    {SUPPORTED_MODELS.map(model => (
                                                        <option key={model} value={model}>{model}</option>
                                                    ))}
                                                </select>
                                            </FormControl>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
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

                    <Card className="rounded-none border-border/40 bg-black/40 backdrop-blur shadow-none">
                        <CardHeader className="border-b border-border/40 pb-4">
                            <CardTitle className="text-sm font-display uppercase tracking-widest">Execution Parameters</CardTitle>
                        </CardHeader>
                        <CardContent className="pt-6">
                            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
                                <FormField
                                    control={form.control}
                                    name="temperature"
                                    render={({ field }) => (
                                        <FormItem>
                                            <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Temperature</FormLabel>
                                            <FormControl>
                                                <Input type="number" step="0.1" min="0" max="2" className="rounded-none border-border bg-black/50" {...field} onChange={e => field.onChange(parseFloat(e.target.value))} />
                                            </FormControl>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
                                />

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
                                                <Input type="number" min="60" max="86400" className="rounded-none border-border bg-black/50" {...field} onChange={e => field.onChange(parseInt(e.target.value, 10))} />
                                            </FormControl>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
                                />
                            </div>

                            <div className="mt-8 border-t border-border/40 pt-6">
                                <FormField
                                    control={form.control}
                                    name="allowed_tools"
                                    render={() => (
                                        <FormItem>
                                            <div className="mb-4">
                                                <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Allowed Tools</FormLabel>
                                                <FormDescription className="text-xs">
                                                    Grant capabilities to this execution
                                                </FormDescription>
                                            </div>
                                            <div className="flex flex-wrap gap-4">
                                                {ALLOWED_TOOLS.map((item) => (
                                                    <FormField
                                                        key={item.id}
                                                        control={form.control}
                                                        name="allowed_tools"
                                                        render={({ field }) => {
                                                            return (
                                                                <FormItem
                                                                    key={item.id}
                                                                    className="flex flex-row items-start space-x-3 space-y-0"
                                                                >
                                                                    <FormControl>
                                                                        <Checkbox
                                                                            className="rounded-none border-primary data-[state=checked]:bg-primary data-[state=checked]:text-black"
                                                                            checked={field.value?.includes(item.id)}
                                                                            onCheckedChange={(checked) => {
                                                                                return checked
                                                                                    ? field.onChange([...(field.value || []), item.id])
                                                                                    : field.onChange(
                                                                                        field.value?.filter(
                                                                                            (value) => value !== item.id
                                                                                        )
                                                                                    )
                                                                            }}
                                                                        />
                                                                    </FormControl>
                                                                    <FormLabel className="font-normal font-mono cursor-pointer">
                                                                        {item.label}
                                                                    </FormLabel>
                                                                </FormItem>
                                                            )
                                                        }}
                                                    />
                                                ))}
                                            </div>
                                            <FormMessage className="text-destructive font-bold text-xs" />
                                        </FormItem>
                                    )}
                                />
                            </div>
                        </CardContent>
                    </Card>

                    <div className="flex justify-end pt-4 pb-12">
                        <Button
                            type="submit"
                            disabled={mutation.isPending}
                            className="rounded-none font-bold uppercase tracking-widest px-8 hover:saturate-150 transition-all border border-primary text-black"
                        >
                            {mutation.isPending ? "INITIALIZING..." : "DISPATCH TASK"}
                        </Button>
                    </div>
                </form>
            </Form>
        </div>
    );
}
