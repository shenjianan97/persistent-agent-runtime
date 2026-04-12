import { Outlet } from 'react-router';
import { Sidebar } from './Sidebar';
import { Header } from './Header';

export function AppShell() {
    return (
        <div className="flex h-screen w-full bg-background text-foreground overflow-hidden font-sans antialiased">
            <Sidebar />
            <div className="flex flex-col flex-1 min-w-0">
                <Header />
                <main className="flex-1 overflow-auto p-4 md:p-8 bg-[radial-gradient(circle_at_top_left,rgba(99,215,255,0.08),transparent_22%),radial-gradient(circle_at_100%_0%,rgba(92,141,255,0.08),transparent_18%),linear-gradient(180deg,#09121f_0%,#08111d_48%,#060c15_100%)]">
                    <div className="mx-auto w-full max-w-6xl">
                        <Outlet />
                    </div>
                </main>
            </div>
        </div>
    );
}
