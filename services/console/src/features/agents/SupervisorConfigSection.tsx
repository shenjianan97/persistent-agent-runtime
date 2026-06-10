import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';

/**
 * Deep Research configuration sub-object (internal topology = `supervisor`).
 *
 * Customer-facing name is **Deep Research** — the word "Supervisor" appears
 * only in `data-testid`s / payload keys, never in visible copy (two-layer
 * naming, Agent Modes design). Field names match the S1 API contract
 * (`SupervisorConfigRequest`) verbatim. The section is gated by the parent's
 * `topology === 'supervisor'` check (returns null when `applicable` is false).
 *
 * UX (S11 refinements): the whole body sits behind a "Customize" disclosure,
 * collapsed by default, so a Deep Research agent is creatable with zero edits —
 * the server `research` preset supplies the real defaults. The number fields
 * surface their defaults as *placeholders*, never set values, so an untouched
 * field stays `undefined` and the parent's only-send-what-was-set payload
 * builder omits it. "Allowed Sources" is a checkbox checklist nested under an
 * "Advanced" disclosure. On `disabled` (Detail page read-only) the body renders
 * expanded with no toggles so persisted values are always visible.
 */
export interface SupervisorConfig {
    max_fanout_per_iteration?: number;
    max_iterations?: number;
    source_allowlist?: string[];
    writer_style?: 'formal_report' | 'annotated_bullets';
    scope_clarification_enabled?: boolean;
}

/** Minimal tool-server shape the source checklist needs (name = value + label). */
export interface SupervisorToolServerOption {
    name: string;
}

interface Props {
    value: SupervisorConfig | undefined;
    onChange: (next: SupervisorConfig) => void;
    /**
     * Read-only-on-edit posture. The Detail page mounts this section with
     * `disabled` so the Deep Research knobs can be inspected but not mutated
     * (conservative — invariant #2 keeps topology immutable; the sub-object is
     * treated as immutable too unless a future task explicitly allows edits).
     * When disabled the body renders expanded (no Customize/Advanced toggles).
     */
    disabled?: boolean;
    /**
     * When false, the section renders nothing. The parent passes `false` for
     * non-supervisor presets so the gate is purely the parent's topology check.
     */
    applicable: boolean;
    /**
     * The agent's active tool servers, surfaced as selectable sources in the
     * Allowed-Sources checklist (name used as both value and label). The parent
     * (CreateAgentDialog) already loads these via `useToolServers('active')`.
     */
    toolServers?: SupervisorToolServerOption[];
}

const WRITER_STYLE_OPTIONS: {
    value: NonNullable<SupervisorConfig['writer_style']>;
    label: string;
    description: string;
}[] = [
    {
        value: 'formal_report',
        label: 'Formal report',
        description: 'Prose with sections & headings. Reads like a written briefing.',
    },
    {
        value: 'annotated_bullets',
        label: 'Annotated bullets',
        description: 'Bulleted findings, each with its source citation. Scannable.',
    },
];

/** Built-in web sources every Deep Research agent can use. */
const BASE_SOURCES: { value: string; label: string }[] = [
    { value: 'web_search', label: 'Web search' },
    { value: 'read_url', label: 'Read web page' },
];

const DEFAULT_WRITER_STYLE: NonNullable<SupervisorConfig['writer_style']> = 'formal_report';

export function SupervisorConfigSection({
    value,
    onChange,
    disabled = false,
    applicable,
    toolServers = [],
}: Props) {
    // Disclosure state. When disabled (read-only Detail page) we render expanded
    // so persisted values are never hidden behind a collapsed toggle.
    const [customizeOpen, setCustomizeOpen] = useState(false);
    const [advancedOpen, setAdvancedOpen] = useState(false);

    if (!applicable) return null;

    const currentValue: SupervisorConfig = value ?? {};
    const maxFanout = currentValue.max_fanout_per_iteration;
    const maxIterations = currentValue.max_iterations;
    const sourceAllowlist = currentValue.source_allowlist ?? [];
    // Displayed default is Formal report; the dirty gate in the parent keeps it
    // unsent unless the user changes it (server preset already defaults to it).
    const writerStyle = currentValue.writer_style ?? DEFAULT_WRITER_STYLE;
    const scopeClarification = currentValue.scope_clarification_enabled ?? false;

    // "All available sources" = no restriction = empty/absent allowlist.
    const allSourcesSelected = sourceAllowlist.length === 0;

    const sourceOptions = [
        ...BASE_SOURCES,
        ...toolServers.map((s) => ({ value: s.name, label: s.name })),
    ];
    // Persisted sources not in the base/tool-server lists (e.g. a tool server
    // that has since been removed, or the read-only Detail page which doesn't
    // pass the agent's tool servers) must still render so values aren't hidden.
    const knownValues = new Set(sourceOptions.map((o) => o.value));
    for (const name of sourceAllowlist) {
        if (!knownValues.has(name)) {
            sourceOptions.push({ value: name, label: name });
            knownValues.add(name);
        }
    }

    const bodyVisible = disabled || customizeOpen;
    const advancedVisible = disabled || advancedOpen;

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

    /** Toggle a specific source. Checking unchecks "All"; unchecking the last reverts to "All". */
    function handleToggleSource(name: string, checked: boolean) {
        const next = checked
            ? [...sourceAllowlist, name]
            : sourceAllowlist.filter((s) => s !== name);
        onChange({
            ...currentValue,
            // Empty ⇒ "All available sources" ⇒ omit the key entirely.
            source_allowlist: next.length ? next : undefined,
        });
    }

    /** Selecting "All available sources" clears any specific selections. */
    function handleSelectAllSources() {
        onChange({ ...currentValue, source_allowlist: undefined });
    }

    function handleWriterStyleChange(e: React.ChangeEvent<HTMLSelectElement>) {
        const next = e.target.value as NonNullable<SupervisorConfig['writer_style']>;
        onChange({ ...currentValue, writer_style: next });
    }

    function handleScopeClarificationChange(e: React.ChangeEvent<HTMLInputElement>) {
        onChange({
            ...currentValue,
            scope_clarification_enabled: e.target.checked,
        });
    }

    const selectedWriterDescription =
        WRITER_STYLE_OPTIONS.find((o) => o.value === writerStyle)?.description ?? '';

    return (
        <div className="space-y-3">
            <span className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
                Deep Research Configuration
            </span>
            <div className="p-3 border border-border rounded-none bg-black/30 space-y-4">
                {!disabled && (
                    <div className="flex items-start justify-between gap-3">
                        <p className="text-xs text-muted-foreground">
                            Using smart defaults &mdash; up to 5 sub-agents per round, a few rounds,
                            all sources.
                        </p>
                        <button
                            type="button"
                            onClick={() => setCustomizeOpen((v) => !v)}
                            aria-expanded={customizeOpen}
                            className="shrink-0 inline-flex items-center gap-1 text-xs uppercase tracking-widest text-primary hover:saturate-150 transition-all"
                        >
                            Customize
                            {customizeOpen ? (
                                <ChevronDown aria-hidden="true" className="h-3.5 w-3.5" />
                            ) : (
                                <ChevronRight aria-hidden="true" className="h-3.5 w-3.5" />
                            )}
                        </button>
                    </div>
                )}

                {bodyVisible && (
                    <div className="space-y-4">
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
                                placeholder="5 (default)"
                                data-testid="supervisor-config-max-fanout"
                                className="flex h-9 w-40 border border-border bg-black/50 px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary rounded-none disabled:cursor-not-allowed disabled:opacity-50"
                                value={maxFanout ?? ''}
                                onChange={handleMaxFanoutChange}
                                disabled={disabled}
                            />
                            <p className="text-xs text-muted-foreground mt-1">
                                How many sub-agents the agent can run in parallel in a single research
                                round (1&ndash;20). More breadth costs more.
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
                                placeholder="3 (default)"
                                data-testid="supervisor-config-max-iterations"
                                className="flex h-9 w-40 border border-border bg-black/50 px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary rounded-none disabled:cursor-not-allowed disabled:opacity-50"
                                value={maxIterations ?? ''}
                                onChange={handleMaxIterationsChange}
                                disabled={disabled}
                            />
                            <p className="text-xs text-muted-foreground mt-1">
                                How many rounds of fan-out the agent may run before it must write the
                                report (1&ndash;10).
                            </p>
                        </div>

                        {/* writer_style */}
                        <div className="space-y-1">
                            <label
                                htmlFor="supervisor-writer-style"
                                className="uppercase tracking-widest text-muted-foreground/70 text-[10px]"
                            >
                                Report Format
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
                                {selectedWriterDescription}
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
                                        Let the agent pause to ask a clarifying question before it
                                        starts researching. Turn off for headless / automated runs
                                        where no one is available to answer.
                                    </p>
                                </div>
                            </div>
                        </div>

                        {/* Advanced disclosure — Allowed Sources checklist */}
                        <div className="pt-1 border-t border-white/8">
                            {!disabled && (
                                <button
                                    type="button"
                                    onClick={() => setAdvancedOpen((v) => !v)}
                                    aria-expanded={advancedOpen}
                                    className="inline-flex items-center gap-1 text-xs uppercase tracking-widest text-muted-foreground hover:text-primary transition-colors pt-2"
                                >
                                    Advanced
                                    {advancedOpen ? (
                                        <ChevronDown aria-hidden="true" className="h-3.5 w-3.5" />
                                    ) : (
                                        <ChevronRight aria-hidden="true" className="h-3.5 w-3.5" />
                                    )}
                                </button>
                            )}

                            {advancedVisible && (
                                <div className="space-y-2 pt-3">
                                    <label className="uppercase tracking-widest text-muted-foreground/70 text-[10px]">
                                        Allowed Sources
                                    </label>
                                    <div
                                        data-testid="supervisor-config-source-allowlist"
                                        className="space-y-1.5"
                                    >
                                        <label className="flex items-center gap-2 cursor-pointer text-sm">
                                            <input
                                                type="checkbox"
                                                className="accent-primary disabled:cursor-not-allowed disabled:opacity-50"
                                                checked={allSourcesSelected}
                                                onChange={handleSelectAllSources}
                                                disabled={disabled}
                                                aria-label="All available sources"
                                            />
                                            <span>All available sources</span>
                                        </label>
                                        {sourceOptions.map((opt) => (
                                            <label
                                                key={opt.value}
                                                className="flex items-center gap-2 cursor-pointer text-sm pl-5"
                                            >
                                                <input
                                                    type="checkbox"
                                                    className="accent-primary disabled:cursor-not-allowed disabled:opacity-50"
                                                    checked={sourceAllowlist.includes(opt.value)}
                                                    onChange={(e) =>
                                                        handleToggleSource(opt.value, e.target.checked)
                                                    }
                                                    disabled={disabled}
                                                    aria-label={opt.label}
                                                />
                                                <span className="font-mono">{opt.label}</span>
                                            </label>
                                        ))}
                                    </div>
                                    <p className="text-xs text-muted-foreground">
                                        Restrict which web tools or document stores the sub-agents may
                                        use. Leave &ldquo;All available sources&rdquo; checked to allow
                                        everything.
                                    </p>
                                </div>
                            )}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
