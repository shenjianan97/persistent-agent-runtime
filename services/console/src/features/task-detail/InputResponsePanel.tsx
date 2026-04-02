import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/api/client';
import { TaskStatusResponse } from '@/types';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { MessageSquare, Send, Clock } from 'lucide-react';
import { toast } from 'sonner';
import { useTimeoutCountdown } from './useTimeoutCountdown';

interface InputResponsePanelProps {
    task: TaskStatusResponse;
    onActionComplete?: () => void;
}

export function InputResponsePanel({ task, onActionComplete }: InputResponsePanelProps) {
    const queryClient = useQueryClient();
    const [message, setMessage] = useState('');
    const remaining = useTimeoutCountdown(task.human_input_timeout_at);

    const respondMutation = useMutation({
        mutationFn: () => api.respondToTask(task.task_id, message),
        onSuccess: () => {
            toast.success('Response sent');
            setMessage('');
            queryClient.invalidateQueries({ queryKey: ['task', task.task_id] });
            onActionComplete?.();
        },
        onError: (err: Error) => toast.error(err.message || 'Failed to send response'),
    });

    const handleSubmit = () => {
        if (!message.trim()) {
            toast.error('Please enter a response');
            return;
        }
        respondMutation.mutate();
    };

    return (
        <Card className="border-blue-500/30 bg-blue-500/5 overflow-hidden relative">
            <div className="absolute top-0 left-0 w-1 h-full bg-blue-500" />
            <CardHeader className="border-b border-blue-500/20">
                <CardTitle className="text-sm font-display uppercase tracking-widest flex items-center gap-2 text-blue-400">
                    <MessageSquare className="w-4 h-4" /> Input Requested
                </CardTitle>
            </CardHeader>
            <CardContent className="pt-4 space-y-4">
                {task.pending_input_prompt && (
                    <div className="text-sm font-mono text-blue-300/80 bg-black/40 border border-blue-500/20 p-3 whitespace-pre-wrap">
                        {task.pending_input_prompt}
                    </div>
                )}

                {remaining !== null && (
                    <div className="flex items-center gap-2 text-xs font-mono text-muted-foreground">
                        <Clock className="w-3 h-3" />
                        <span>Timeout in <span className="text-blue-400 font-bold">{remaining}</span></span>
                    </div>
                )}

                <Textarea
                    placeholder="Type your response..."
                    value={message}
                    onChange={(e) => setMessage(e.target.value)}
                    className="border-blue-500/20 bg-black/40 text-sm min-h-[80px]"
                />

                <Button
                    size="sm"
                    className="uppercase tracking-[0.18em] font-bold text-xs bg-blue-500 text-white hover:bg-blue-400"
                    onClick={handleSubmit}
                    disabled={respondMutation.isPending}
                >
                    <Send className="w-4 h-4 mr-2" /> Send Response
                </Button>
            </CardContent>
        </Card>
    );
}
