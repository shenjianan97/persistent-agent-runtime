import { BrowserRouter, Routes, Route, Navigate } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AppShell } from './layout/AppShell';
import { DashboardPage } from './features/dashboard/DashboardPage';
import { SubmitTaskPage } from './features/submit/SubmitTaskPage';
import { TaskDetailPage } from './features/task-detail/TaskDetailPage';
import { TaskListPage } from './features/task-list/TaskListPage';
import { DeadLetterPage } from './features/dead-letter/DeadLetterPage';
import { SettingsPage } from './features/settings/SettingsPage';
import { AgentsListPage } from './features/agents/AgentsListPage';
import { AgentDetailPage } from './features/agents/AgentDetailPage';
import { ToolServersListPage } from './features/tool-servers/ToolServersListPage';
import { ToolServerDetailPage } from './features/tool-servers/ToolServerDetailPage';
import { Toaster } from 'sonner';

const queryClient = new QueryClient({
    defaultOptions: {
        queries: {
            retry: 1,
            refetchOnWindowFocus: false,
        },
    },
});

function App() {
    return (
        <QueryClientProvider client={queryClient}>
            <BrowserRouter>
                <Routes>
                    <Route element={<AppShell />}>
                        <Route path="/" element={<DashboardPage />} />
                        <Route path="/tasks" element={<TaskListPage />} />
                        <Route path="/tasks/new" element={<SubmitTaskPage />} />
                        <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
                        <Route path="/agents" element={<AgentsListPage />} />
                        <Route path="/agents/:agentId" element={<AgentDetailPage />} />
                        <Route path="/agents/:agentId/memory" element={<AgentDetailPage />} />
                        <Route path="/agents/:agentId/memory/:memoryId" element={<AgentDetailPage />} />
                        <Route path="/tool-servers" element={<ToolServersListPage />} />
                        <Route path="/tool-servers/:serverId" element={<ToolServerDetailPage />} />
                        <Route path="/dead-letter" element={<DeadLetterPage />} />
                        <Route path="/settings" element={<SettingsPage />} />
                        <Route path="*" element={<Navigate to="/" replace />} />
                    </Route>
                </Routes>
            </BrowserRouter>
            {/* Industrial brutalist toaster styling */}
            <Toaster
                theme="dark"
                toastOptions={{
                    style: {
                        borderRadius: '18px',
                        border: '1px solid rgba(104, 145, 190, 0.16)',
                        background: 'linear-gradient(180deg, rgba(18, 27, 42, 0.94), rgba(12, 19, 31, 0.92))',
                        color: 'var(--foreground)',
                        fontFamily: 'var(--font-mono)',
                        boxShadow: '0 18px 48px rgba(0, 0, 0, 0.24)'
                    }
                }}
            />
        </QueryClientProvider>
    );
}

export default App;
