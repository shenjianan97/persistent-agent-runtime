import { useState } from 'react';
import { ChevronDown, X } from 'lucide-react';

/**
 * Deep Research configuration sub-object (internal topology = `supervisor`).
 *
 * Customer-facing name is **Deep Research** — the word "Supervisor" appears
 * only in `data-testid`s / payload keys, never in visible copy (two-layer
 * naming, Agent Modes design). Field names match the S1 API contract
 * (`SupervisorConfigRequest`) verbatim. The section is gated by the parent's
 * `topology === 'supervisor'` check (returns null when `value` is undefined
 * AND the parent didn't mount it for a supervisor agent — see Props.applicable).
 */
export interface SupervisorConfig {
    max_fanout_per_iteration?: number;
    max_iterations?: number;
    source_allowlist?: string[];
    writer_style?: 'formal_report' | 'annotated_bullets';
    scope_clarification_enabled?: boolean;
}

interface Props {
    value: SupervisorConfig | undefined;
    onChange: (next: SupervisorConfig) => void;
    /**
     * Read-only-on-edit posture. The Detail page mounts this section with
     * `disabled` so the Deep Research knobs can be inspected but not mutated
     * (conservative — invariant #2 keeps topology immutable; the sub-object is
     * treated as immutable too unless a future task explicitly allows edits).
     */
    disabled?: boolean;
    /**
     * When false, the section renders nothing. The parent passes `false` for
     * non-supervisor presets so the gate is purely the parent's topology check.
     */
    applicable: boolean;
}

const MAX_SOURCE_ALLOWLIST = 50;

const WRITER_STYLE_OPTIONS: { value: NonNullable<SupervisorConfig['writer_style']>; label: string }[] = [
    { value: 'formal_report', label: 'Formal report' },
    { value: 'annotated_bullets', label: 'Annotated bullets' },
];

export function SupervisorConfigSection({ value, onChange, disabled = false, applicable }: Props) {
    const [chipInput, setChipInput] = useState('');
    const [capError, setCapError] = useState(false);

    if (!applicable) return null;

    const currentValue: SupervisorConfig = value ?? {};
    const maxFanout = currentValue.max_fanout_per_iteration;
    const maxIterations = currentValue.max_iterations;
    const sourceAllowlist = currentValue.source_allowlist ?? [];
    const writerStyle = currentValue.writer_style ?? '';
    const scopeClarification = currentValue.scope_clarification_enabled ?? false;

    function handleMaxFanoutChange(e: React.ChangeEvent<HTMLInputElement>) {
        const raw = e.target.value;
        onChange({
            ...currentValue,
            max_fanout_per_iteration: raw === '' ? undefined : Number.parseInt(raw, 10),
        });
    }

    function handleMaxIterationsChange(e: React.ChangeEvent<HTMLInputElement>) {
        const raw = e.target.value;
        onChange({
            ...currentValue,
            max_iterations: raw === '' ? undefined : Number.parseInt(raw, 10),
        });
    }

    function handleAddSource(name: string) {
        const trimmed = name.trim();
        if (!trimmed) return;
        if (sourceAllowlist.length >= MAX_SOURCE_ALLOWLIST) {
            setCapError(true);
            return;
        }
        setCapError(false);
        setChipInput('');
        onChange({
            ...currentValue,
            source_allowlist: [...sourceAllowlist, trimmed],
        });
    }

    function handleRemoveSource(name: string) {
        setCapError(false);
        onChange({
            ...currentValue,
            source_allowlist: sourceAllowlist.filter((s) => s !== name),
        });
    }

    function handleChipKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
        if (e.key === 'Enter') {
            e.preventDefault();
            if (sourceAllowlist.length >= MAX_SOURCE_ALLOWLIST) {
                setCapError(true);
                return;
            }
            handleAddSource(chipInput);
        }
    }

    function handleChipInputChange(e: React.ChangeEvent<HTMLInputElement>) {
        setChipInput(e.target.value);
        if (capError) setCapError(false);
    }

    function handleWriterStyleChange(e: React.ChangeEvent<HTMLSelectElement>) {
        const next = e.target.value;
        onChange({
            ...currentValue,
            writer_style: next === '' ? undefined : (next as SupervisorConfig['writer_style']),
        });
    }

    function handleScopeClarificationChange(e: React.ChangeEvent<HTMLInputElement>) {
        onChange({
            ...currentValue,
            scope_clarification_enabled: e.target.checked,
        });
    }

    const atCap = sourceAllowlist.length >= MAX_SOURCE_ALLOWLIST;

    return (
        <div className="space-y-3">
            <span className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
                Deep Research Configuration
            </span>
            <div className="p-3 border border-border rounded-none bg-black/30 space-y-4">
                <p className="text-xs text-muted-foreground">
                    How the agent scopes, fans out, and writes. These settings tune a long-running
                    research run; leave them blank to use the platform defaults for the Deep Research
                    preset.
                </p>

                {/* max_fanout_per_iteration */}
                <div className="space-y-1">
                    <label
                        htmlFor="supervisor-max-fanout"
                        className="uppercase tracking-widest text-muted-foreground/70 text-[10px]"
                    >
                        Sub-agents Per Round
                    </label>
                    <input
                        id="supervisor-max-fanout"
                        type="number"
                        min="1"
                        max="20"
                        step="1"
                        data-testid="supervisor-config-max-fanout"
                        className="flex h-9 w-32 border border-border bg-black/50 px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary rounded-none disabled:cursor-not-allowed disabled:opacity-50"
                        value={maxFanout ?? ''}
                        onChange={handleMaxFanoutChange}
                        disabled={disabled}
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                        How many sub-agents the agent can run in parallel in a single research round
                        (1&ndash;20). More breadth costs more.
                    </p>
                </div>

                {/* max_iterations */}
                <div className="space-y-1">
                    <label
                        htmlFor="supervisor-max-iterations"
                        className="uppercase tracking-widest text-muted-foreground/70 text-[10px]"
                    >
                        Maximum Research Rounds
                    </label>
                    <input
                        id="supervisor-max-iterations"
                        type="number"
                        min="1"
                        max="10"
                        step="1"
                        data-testid="supervisor-config-max-iterations"
                        className="flex h-9 w-32 border border-border bg-black/50 px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary rounded-none disabled:cursor-not-allowed disabled:opacity-50"
                        value={maxIterations ?? ''}
                        onChange={handleMaxIterationsChange}
                        disabled={disabled}
                    />
                    <p className="text-xs text-muted-foreground mt-1">
                        How many rounds of fan-out the agent may run before it must write the report
                        (1&ndash;10).
                    </p>
                </div>

                {/* source_allowlist chip input */}
                <div data-testid="supervisor-config-source-allowlist" className="space-y-2">
                    <div className="flex items-center justify-between">
                        <label className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">
                            Allowed Sources
                        </label>
                        <span className="text-[10px] text-muted-foreground">
                            {sourceAllowlist.length}&nbsp;/&nbsp;50
                        </span>
                    </div>

                    {sourceAllowlist.length > 0 && (
                        <div className="flex flex-wrap gap-1">
                            {sourceAllowlist.map((source) => (
                                <span
                                    key={source}
                                    data-chip
                                    className="inline-flex items-center gap-1 px-2 py-0.5 bg-primary/10 border border-primary/30 text-primary text-xs rounded-none font-mono"
                                >
                                    {source}
                                    {!disabled && (
                                        <button
                                            type="button"
                                            onClick={() => handleRemoveSource(source)}
                                            className="ml-0.5 hover:text-destructive transition-colors"
                                            aria-label={`Remove ${source}`}
                                        >
                                            <X className="w-3 h-3" />
                                        </button>
                                    )}
                                </span>
                            ))}
                        </div>
                    )}

                    {!disabled && (
                        <input
                            type="text"
                            placeholder="Add source name and press Enter"
                            className="flex h-9 w-full border border-border bg-black/50 px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary rounded-none font-mono disabled:cursor-not-allowed disabled:opacity-50"
                            value={chipInput}
                            onChange={handleChipInputChange}
                            onKeyDown={handleChipKeyDown}
                            disabled={atCap}
                        />
                    )}

                    {capError && (
                        <p className="text-xs text-destructive font-bold">
                            Maximum 50 entries
                        </p>
                    )}

                    <p className="text-xs text-muted-foreground">
                        Restrict which web tools or document stores the sub-agents may use. Leave empty
                        to allow all available sources.
                    </p>
                </div>

                {/* writer_style */}
                <div className="space-y-1">
                    <label
                        htmlFor="supervisor-writer-style"
                        className="uppercase tracking-widest text-muted-foreground/70 text-[10px]"
                    >
                        Report Style
                    </label>
                    <div className="relative">
                        <select
                            id="supervisor-writer-style"
                            data-testid="supervisor-config-writer-style"
                            className="flex h-10 w-full border border-border bg-black/50 px-3 py-2 pr-10 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary rounded-none appearance-none disabled:cursor-not-allowed disabled:opacity-50"
                            value={writerStyle}
                            onChange={handleWriterStyleChange}
                            disabled={disabled}
                        >
                            <option value="">Platform default</option>
                            {WRITER_STYLE_OPTIONS.map((opt) => (
                                <option key={opt.value} value={opt.value}>
                                    {opt.label}
                                </option>
                            ))}
                        </select>
                        <ChevronDown
                            aria-hidden="true"
                            className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
                        />
                    </div>
                    <p className="text-xs text-muted-foreground mt-1">
                        Choose how the final report is written &mdash; a formal report or annotated
                        bullet points.
                    </p>
                </div>

                {/* scope_clarification_enabled */}
                <div className="space-y-1">
                    <div className="flex flex-row items-start gap-3">
                        <input
                            type="checkbox"
                            id="supervisor-scope-clarification"
                            data-testid="supervisor-config-scope-clarification"
                            className="accent-primary mt-0.5 disabled:cursor-not-allowed disabled:opacity-50"
                            checked={scopeClarification}
                            onChange={handleScopeClarificationChange}
                            disabled={disabled}
                        />
                        <div>
                            <label
                                htmlFor="supervisor-scope-clarification"
                                className="font-normal font-mono cursor-pointer text-sm"
                            >
                                Ask Clarifying Questions
                            </label>
                            <p className="text-xs text-muted-foreground mt-0.5">
                                Let the agent pause to ask a clarifying question before it starts
                                researching. Turn off for headless / automated runs where no one is
                                available to answer.
                            </p>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
