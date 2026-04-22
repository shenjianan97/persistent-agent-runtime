import { useState } from 'react';
import { ChevronDown, X } from 'lucide-react';
import { formatProviderLabel, groupModelsByProvider } from '@/lib/models';
import type { ModelResponse } from '@/types';

export interface ContextManagementConfig {
    summarizer_model?: string;
    summarizer_provider?: string;
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
    const summarizerProvider = currentValue.summarizer_provider ?? '';
    const preFlush = currentValue.pre_tier3_memory_flush ?? false;
    const selectedSummarizerValue = summarizerModel
        ? (summarizerProvider
            ? `${summarizerProvider}|${summarizerModel}`
            : (availableSummarizerModels.find((m) => m.model_id === summarizerModel)
                ? `${availableSummarizerModels.find((m) => m.model_id === summarizerModel)?.provider}|${summarizerModel}`
                : ''))
        : '';

    function handleSummarizerModelChange(e: React.ChangeEvent<HTMLSelectElement>) {
        const next = e.target.value;
        if (!next) {
            onChange({
                ...currentValue,
                summarizer_model: undefined,
                summarizer_provider: undefined,
            });
            return;
        }
        const separatorIndex = next.indexOf('|');
        const provider = next.slice(0, separatorIndex);
        const modelId = next.slice(separatorIndex + 1);
        onChange({
            ...currentValue,
            summarizer_model: modelId || undefined,
            summarizer_provider: provider || undefined,
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
                Long-Running Task Context
            </span>
            <div className="p-3 border border-border rounded-none bg-black/30 space-y-4">
                <p className="text-xs text-muted-foreground">
                    When tasks run for a long time, the platform may summarize older context to keep the
                    agent effective. These are advanced settings.
                </p>

                {/* summarizer_model */}
                <div className="space-y-1">
                    <label
                        htmlFor="ctx-summarizer-model"
                        className="uppercase tracking-widest text-muted-foreground/70 text-[10px]"
                    >
                        Summarizer Model
                    </label>
                    <div className="relative">
                        <select
                            id="ctx-summarizer-model"
                            data-testid="context-management-summarizer-model"
                            className="flex h-10 w-full border border-border bg-black/50 px-3 py-2 pr-10 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary focus-visible:ring-offset-0 rounded-none appearance-none"
                            value={selectedSummarizerValue}
                            onChange={handleSummarizerModelChange}
                        >
                            <option value="">Platform default</option>
                            {modelGroups.map((group) => (
                                <optgroup key={group.provider} label={group.label}>
                                    {group.models.map((m) => (
                                        <option key={`${m.provider}|${m.model_id}`} value={`${m.provider}|${m.model_id}`}>
                                            {m.display_name}
                                            {' '}
                                            (
                                            {formatProviderLabel(m.provider)}
                                            )
                                        </option>
                                    ))}
                                </optgroup>
                            ))}
                        </select>
                        <ChevronDown
                            aria-hidden="true"
                            className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                        />
                    </div>
                    <p className="text-xs text-muted-foreground mt-1">
                        Choose which model summarizes older context when a task gets long. Leave on
                        {' '}
                        Platform default
                        {' '}
                        unless you have a specific reason to change it.
                    </p>
                </div>

                {/* exclude_tools chip input */}
                <div
                    data-testid="context-management-exclude-tools"
                    className="space-y-2"
                >
                    <div className="flex items-center justify-between">
                        <label className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">
                            Always Keep Outputs From
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
                        Preserve outputs from these tools even when older context is reduced.
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
                                Save Important Facts Before Summarizing
                            </label>
                            <p className="text-xs text-muted-foreground mt-0.5">
                                Before older context is summarized for the first time, let the agent save
                                durable facts to memory.
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
