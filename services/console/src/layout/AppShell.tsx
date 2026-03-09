import { Outlet } from 'react-router';
import { Sidebar } from './Sidebar';
import { Header } from './Header';

export function AppShell() {
    return (
        <div className="flex h-screen w-full bg-background text-foreground overflow-hidden font-mono antialiased">
            <Sidebar />
            <div className="flex flex-col flex-1 min-w-0">
                <Header />
                <main className="flex-1 overflow-auto p-4 md:p-8 bg-[#0a0a0a] bg-[radial-gradient(#ffffff0a_1px,transparent_1px)] [background-size:24px_24px]">
                    <div className="mx-auto w-full max-w-6xl">
                        <Outlet />
                    </div>
                </main>
            </div>
        </div>
    );
}
