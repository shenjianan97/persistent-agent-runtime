import { useMemo } from 'react';
import { AlertCircle } from 'lucide-react';

/**
 * Per-entry overhead for formatting fences / separators in the injected
 * prompt prefix. Tuned to match the Task-10 contract: approx bytes =
 * sum(title + summary + observations) + 50 per entry.
 */
const PER_ENTRY_OVERHEAD_BYTES = 50;
/** At this threshold the indicator turns amber. Advisory only — never blocks. */
const LARGE_ATTACHMENT_WARNING_BYTES = 10 * 1024;

export interface TokenFootprintEntry {
    memory_id: string;
    title?: string;
    summary?: string;
    observations?: string[];
}

/** Exact byte count estimator. Exported for unit testing. */
export function computeAttachmentBytes(entries: TokenFootprintEntry[]): number {
    return entries.reduce((total, entry) => {
        const title = entry.title?.length ?? 0;
        const summary = entry.summary?.length ?? 0;
        const observations = (entry.observations ?? []).reduce(
            (sum, obs) => sum + obs.length,
            0
        );
        return total + title + summary + observations + PER_ENTRY_OVERHEAD_BYTES;
    }, 0);
}

function formatBytes(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    const kb = bytes / 1024;
    if (kb < 10) return `${kb.toFixed(2)} KB`;
    if (kb < 1024) return `${kb.toFixed(1)} KB`;
    return `${(kb / 1024).toFixed(1)} MB`;
}

interface TokenFootprintIndicatorProps {
    /**
     * The current selection's resolved detail records. Entries whose detail
     * is still loading should be omitted; the footprint underestimates rather
     * than flashing misleading numbers at the user.
     */
    entries: TokenFootprintEntry[];
    /** Total selection count (including entries whose detail hasn't loaded). */
    selectionCount: number;
}

/**
 * Plaintext token-footprint indicator for the attach-memory widget.
 *
 * Turns amber at >=10 KB with an explanatory tooltip. Informational only —
 * the design doc and task spec explicitly state this must not block
 * submission. When no entries are selected, the component renders nothing.
 */
export function TokenFootprintIndicator({
    entries,
    selectionCount,
}: TokenFootprintIndicatorProps) {
    const bytes = useMemo(() => computeAttachmentBytes(entries), [entries]);
    if (selectionCount === 0) return null;

    const isLarge = bytes >= LARGE_ATTACHMENT_WARNING_BYTES;
    const label = `Attached context: ~${formatBytes(bytes)} \u00B7 ${selectionCount} ${
        selectionCount === 1 ? 'entry' : 'entries'
    }`;

    return (
        <div
            role="status"
            aria-live="polite"
            data-testid="token-footprint-indicator"
            data-large={isLarge ? 'true' : 'false'}
            className={`flex items-center gap-2 text-xs font-mono ${
                isLarge ? 'text-amber-400' : 'text-muted-foreground'
            }`}
            title={
                isLarge
                    ? 'Large attachment context may increase cost and risk hitting context-window limits'
                    : undefined
            }
        >
            {isLarge ? <AlertCircle className="w-3.5 h-3.5" /> : null}
            <span>{label}</span>
        </div>
    );
}
