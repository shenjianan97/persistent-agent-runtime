import { BrowserRouter, Routes, Route, Navigate } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AppShell } from './layout/AppShell';
import { DashboardPage } from './features/dashboard/DashboardPage';
import { SubmitTaskPage } from './features/submit/SubmitTaskPage';
import { TaskDetailPage } from './features/task-detail/TaskDetailPage';
import { DeadLetterPage } from './features/dead-letter/DeadLetterPage';
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
                        <Route path="/tasks/new" element={<SubmitTaskPage />} />
                        <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
                        <Route path="/dead-letter" element={<DeadLetterPage />} />
                        <Route path="*" element={<Navigate to="/" replace />} />
                    </Route>
                </Routes>
            </BrowserRouter>
            {/* Industrial brutalist toaster styling */}
            <Toaster
                theme="dark"
                toastOptions={{
                    style: {
                        borderRadius: '0px',
                        border: '1px solid var(--border)',
                        background: 'var(--background)',
                        color: 'var(--foreground)',
                        fontFamily: 'var(--font-mono)'
                    }
                }}
            />
        </QueryClientProvider>
    );
}

export default App;
