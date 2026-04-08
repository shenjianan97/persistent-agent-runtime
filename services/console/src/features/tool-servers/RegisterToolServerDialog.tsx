import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { toast } from 'sonner';
import {
    Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/ui/dialog';
import {
    Form, FormControl, FormField, FormItem, FormLabel, FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { useCreateToolServer } from './useToolServers';

const registerSchema = z.object({
    name: z.string()
        .min(1, 'Name is required')
        .max(100, 'Name must be 100 characters or less')
        .regex(/^[a-z0-9][a-z0-9-]*$/, 'Must start with lowercase alphanumeric and contain only lowercase letters, numbers, and hyphens'),
    url: z.string()
        .min(1, 'URL is required')
        .max(2048, 'URL must be 2048 characters or less')
        .url('Must be a valid URL'),
    auth_type: z.enum(['none', 'bearer_token']),
    auth_token: z.string().optional(),
}).refine(
    (data) => data.auth_type !== 'bearer_token' || (data.auth_token && data.auth_token.length > 0),
    { message: 'Auth token is required for Bearer Token auth', path: ['auth_token'] }
);

type RegisterFormValues = z.infer<typeof registerSchema>;

interface RegisterToolServerDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
}

export function RegisterToolServerDialog({ open, onOpenChange }: RegisterToolServerDialogProps) {
    const mutation = useCreateToolServer();
    const form = useForm<RegisterFormValues>({
        resolver: zodResolver(registerSchema),
        defaultValues: {
            name: '',
            url: '',
            auth_type: 'none',
            auth_token: '',
        },
    });

    const authType = form.watch('auth_type');

    function onSubmit(data: RegisterFormValues) {
        mutation.mutate(
            {
                name: data.name,
                url: data.url,
                auth_type: data.auth_type,
                auth_token: data.auth_type === 'bearer_token' ? data.auth_token : undefined,
            },
            {
                onSuccess: () => {
                    toast.success('Tool server registered', {
                        description: `${data.name} is now available for agents.`,
                    });
                    form.reset();
                    onOpenChange(false);
                },
                onError: (error: Error) => {
                    toast.error('Failed to register tool server', {
                        description: error.message || 'Unknown error occurred.',
                    });
                },
            }
        );
    }

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="sm:max-w-[500px] console-surface border-white/10 rounded-2xl">
                <DialogHeader>
                    <DialogTitle className="text-lg font-display uppercase tracking-widest text-primary">
                        Register Tool Server
                    </DialogTitle>
                </DialogHeader>

                <Form {...form}>
                    <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-5">
                        <FormField
                            control={form.control}
                            name="name"
                            render={({ field }) => (
                                <FormItem>
                                    <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Name</FormLabel>
                                    <FormControl>
                                        <Input
                                            className="rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1"
                                            placeholder="e.g., jira-tools"
                                            {...field}
                                        />
                                    </FormControl>
                                    <FormMessage className="text-destructive font-bold text-xs" />
                                </FormItem>
                            )}
                        />

                        <FormField
                            control={form.control}
                            name="url"
                            render={({ field }) => (
                                <FormItem>
                                    <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">URL</FormLabel>
                                    <FormControl>
                                        <Input
                                            className="rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1"
                                            placeholder="e.g., http://localhost:9000/mcp"
                                            {...field}
                                        />
                                    </FormControl>
                                    <FormMessage className="text-destructive font-bold text-xs" />
                                </FormItem>
                            )}
                        />

                        <FormField
                            control={form.control}
                            name="auth_type"
                            render={({ field }) => (
                                <FormItem>
                                    <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Authentication</FormLabel>
                                    <FormControl>
                                        <select
                                            className="flex h-10 w-full border border-border bg-black/50 px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary focus-visible:ring-offset-0 rounded-none appearance-none"
                                            value={field.value}
                                            onChange={field.onChange}
                                        >
                                            <option value="none">None</option>
                                            <option value="bearer_token">Bearer Token</option>
                                        </select>
                                    </FormControl>
                                    <FormMessage className="text-destructive font-bold text-xs" />
                                </FormItem>
                            )}
                        />

                        {authType === 'bearer_token' && (
                            <FormField
                                control={form.control}
                                name="auth_token"
                                render={({ field }) => (
                                    <FormItem>
                                        <FormLabel className="uppercase tracking-widest text-muted-foreground text-xs">Auth Token</FormLabel>
                                        <FormControl>
                                            <Input
                                                type="password"
                                                className="rounded-none border-border bg-black/50 focus-visible:ring-primary focus-visible:ring-1"
                                                placeholder="Bearer token"
                                                {...field}
                                            />
                                        </FormControl>
                                        <FormMessage className="text-destructive font-bold text-xs" />
                                    </FormItem>
                                )}
                            />
                        )}

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
                                {mutation.isPending ? 'Registering...' : 'Register'}
                            </Button>
                        </DialogFooter>
                    </form>
                </Form>
            </DialogContent>
        </Dialog>
    );
}
