import { Settings } from 'lucide-react';
import { LangfuseEndpointList } from './LangfuseEndpointList';

export function SettingsPage() {
    return (
        <div className="max-w-4xl mx-auto animate-in fade-in duration-500">
            <div className="console-surface-strong rounded-[28px] p-6 md:p-7 mb-8">
                <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.24em] text-primary">Configuration</div>
                <h2 className="text-3xl font-display font-semibold tracking-tight mb-2 flex items-center gap-2">
                    <Settings className="w-6 h-6 text-primary drop-shadow-[0_0_12px_var(--color-primary)]" />
                    Settings
                </h2>
                <p className="text-muted-foreground w-full md:w-2/3">
                    Manage integrations and platform configuration.
                </p>
            </div>

            <div className="console-surface border-white/10 rounded-2xl p-6">
                <LangfuseEndpointList />
            </div>
        </div>
    );
}
