import { useState } from 'react';
import { X } from 'lucide-react';
import { groupModelsByProvider } from '@/lib/models';
import type { ModelResponse } from '@/types';

export interface ContextManagementConfig {
    summarizer_model?: string;
    exclude_tools?: string[];
    pre_tier3_memory_flush?: boolean;
}

interface Props {
    value: ContextManagementConfig | undefined;
    memoryEnabled: boolean;
    availableSummarizerModels: ModelResponse[];
    onChange: (next: ContextManagementConfig) => void;
}

const MAX_EXCLUDE_TOOLS = 50;

export function ContextManagementSection({
    value,
    memoryEnabled,
    availableSummarizerModels,
    onChange,
}: Props) {
    const [chipInput, setChipInput] = useState('');
    const [capError, setCapError] = useState(false);

    const modelGroups = groupModelsByProvider(availableSummarizerModels);
    const currentValue: ContextManagementConfig = value ?? {};
    const excludeTools = currentValue.exclude_tools ?? [];
    const summarizerModel = currentValue.summarizer_model ?? '';
    const preFlush = currentValue.pre_tier3_memory_flush ?? false;

    function handleSummarizerModelChange(e: React.ChangeEvent<HTMLSelectElement>) {
        const next = e.target.value;
        onChange({
            ...currentValue,
            summarizer_model: next || undefined,
        });
    }

    function handleAddTool(toolName: string) {
        const trimmed = toolName.trim();
        if (!trimmed) return;

        if (excludeTools.length >= MAX_EXCLUDE_TOOLS) {
            setCapError(true);
            return;
        }

        setCapError(false);
        setChipInput('');
        onChange({
            ...currentValue,
            exclude_tools: [...excludeTools, trimmed],
        });
    }

    function handleRemoveTool(toolName: string) {
        setCapError(false);
        onChange({
            ...currentValue,
            exclude_tools: excludeTools.filter((t) => t !== toolName),
        });
    }

    function handleChipKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
        if (e.key === 'Enter') {
            e.preventDefault();
            if (excludeTools.length >= MAX_EXCLUDE_TOOLS) {
                setCapError(true);
                return;
            }
            handleAddTool(chipInput);
        }
    }

    function handleChipInputChange(e: React.ChangeEvent<HTMLInputElement>) {
        setChipInput(e.target.value);
        if (capError) setCapError(false);
    }

    function handlePreFlushChange(e: React.ChangeEvent<HTMLInputElement>) {
        onChange({
            ...currentValue,
            pre_tier3_memory_flush: e.target.checked,
        });
    }

    return (
        <div className="space-y-3">
            <span className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
                Context Management
            </span>
            <div className="p-3 border border-border rounded-none bg-black/30 space-y-4">
                <p className="text-xs text-muted-foreground">
                    Context management is always-on platform infrastructure; the fields below are tuning knobs, not an enable toggle.
                </p>

                {/* summarizer_model */}
                <div className="space-y-1">
                    <label
                        htmlFor="ctx-summarizer-model"
                        className="uppercase tracking-widest text-muted-foreground/70 text-[10px]"
                    >
                        Summarizer Model
                    </label>
                    <select
                        id="ctx-summarizer-model"
                        data-testid="context-management-summarizer-model"
                        className="flex h-10 w-full border border-border bg-black/50 px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary focus-visible:ring-offset-0 rounded-none appearance-none"
                        value={summarizerModel}
                        onChange={handleSummarizerModelChange}
                    >
                        <option value="">Platform default</option>
                        {modelGroups.map((group) => (
                            <optgroup key={group.provider} label={group.label}>
                                {group.models.map((m) => (
                                    <option key={m.model_id} value={m.model_id}>
                                        {m.display_name}
                                    </option>
                                ))}
                            </optgroup>
                        ))}
                    </select>
                    <p className="text-xs text-muted-foreground mt-1">
                        Model used for Tier 3 summarization. Leave as "Platform default" unless you need a specific model.
                    </p>
                </div>

                {/* exclude_tools chip input */}
                <div
                    data-testid="context-management-exclude-tools"
                    className="space-y-2"
                >
                    <div className="flex items-center justify-between">
                        <label className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">
                            Exclude Tools from Compaction
                        </label>
                        <span className="text-[10px] text-muted-foreground">
                            {excludeTools.length}&nbsp;/&nbsp;50
                        </span>
                    </div>

                    {/* Chips */}
                    {excludeTools.length > 0 && (
                        <div className="flex flex-wrap gap-1">
                            {excludeTools.map((tool) => (
                                <span
                                    key={tool}
                                    data-chip
                                    className="inline-flex items-center gap-1 px-2 py-0.5 bg-primary/10 border border-primary/30 text-primary text-xs rounded-none font-mono"
                                >
                                    {tool}
                                    <button
                                        type="button"
                                        onClick={() => handleRemoveTool(tool)}
                                        className="ml-0.5 hover:text-destructive transition-colors"
                                        aria-label={`Remove ${tool}`}
                                    >
                                        <X className="w-3 h-3" />
                                    </button>
                                </span>
                            ))}
                        </div>
                    )}

                    {/* Input */}
                    <input
                        type="text"
                        placeholder="Add tool name and press Enter"
                        className="flex h-9 w-full border border-border bg-black/50 px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary rounded-none font-mono"
                        value={chipInput}
                        onChange={handleChipInputChange}
                        onKeyDown={handleChipKeyDown}
                        disabled={excludeTools.length >= MAX_EXCLUDE_TOOLS}
                    />

                    {capError && (
                        <p className="text-xs text-destructive font-bold">
                            Maximum 50 entries
                        </p>
                    )}

                    <p className="text-xs text-muted-foreground">
                        Tool names whose results are never masked during Tier 1 compaction. Additive on top of the platform-seeded list.
                    </p>
                </div>

                {/* pre_tier3_memory_flush toggle */}
                <div className="space-y-1">
                    <div className="flex flex-row items-start gap-3">
                        <input
                            type="checkbox"
                            id="ctx-pre-tier3-flush"
                            data-testid="context-management-pre-tier3-flush"
                            className="accent-primary mt-0.5 disabled:cursor-not-allowed disabled:opacity-50"
                            checked={preFlush}
                            onChange={handlePreFlushChange}
                            disabled={!memoryEnabled}
                        />
                        <div>
                            <label
                                htmlFor="ctx-pre-tier3-flush"
                                className="font-normal font-mono cursor-pointer text-sm"
                            >
                                Pre-Tier-3 Memory Flush
                            </label>
                            <p className="text-xs text-muted-foreground mt-0.5">
                                Before the first Tier 3 summarization in a task, give the agent a chance to
                                call <code>memory_note</code> to preserve cross-task facts.
                            </p>
                            {!memoryEnabled && (
                                <p className="text-xs text-amber-400 mt-0.5">
                                    Requires memory to be enabled.
                                </p>
                            )}
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
