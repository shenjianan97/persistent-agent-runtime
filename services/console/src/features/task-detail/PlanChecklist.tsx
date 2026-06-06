import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';
import type { PlanItem } from '@/types';

// ------------------------------------------------------------------
// Status helpers
// ------------------------------------------------------------------

type KnownStatus = 'pending' | 'in_progress' | 'completed';

/**
 * Normalise any value (including null / unknown future strings) to one of the
 * three known statuses, defaulting to 'pending' for anything unrecognised.
 * This keeps the renderer crash-free per the P3 wire-format guarantee.
 */
function normaliseStatus(raw: unknown): KnownStatus {
    if (raw === 'completed') return 'completed';
    if (raw === 'in_progress') return 'in_progress';
    // null, undefined, unknown string → treat as pending
    return 'pending';
}

function badgeLabel(status: KnownStatus): string {
    if (status === 'completed') return 'completed';
    if (status === 'in_progress') return 'in progress';
    return 'pending';
}

function badgeClass(status: KnownStatus): string {
    if (status === 'completed') {
        return 'bg-success/15 text-success border border-success/30';
    }
    if (status === 'in_progress') {
        return 'bg-primary/15 text-primary border border-primary/30';
    }
    // pending
    return 'bg-muted/20 text-muted-foreground border border-border/30';
}

// ------------------------------------------------------------------
// Per-item row
// ------------------------------------------------------------------

interface PlanItemRowProps {
    item: PlanItem;
    /** Zero-based index, used as a fallback key when id is null. */
    index: number;
}

function PlanItemRow({ item, index }: PlanItemRowProps) {
    const status = normaliseStatus(item.status);
    const isCompleted = status === 'completed';

    // id may be null (corrupted checkpoint — P3 tolerates it)
    const rowId = item.id ?? `index-${index}`;

    return (
        <div
            data-testid={`plan-item-${rowId}`}
            className="flex items-start gap-3 py-2 px-1 rounded-md hover:bg-muted/10 transition-colors"
        >
            {/* Read-only checkbox — disabled so there's no interactive affordance */}
            <input
                type="checkbox"
                checked={isCompleted}
                disabled
                readOnly
                aria-label={item.title ?? '(untitled)'}
                className="mt-0.5 h-4 w-4 shrink-0 accent-primary cursor-default"
            />

            {/* Title — strike-through when completed */}
            <span
                className={`flex-1 text-sm leading-snug ${
                    isCompleted ? 'line-through text-muted-foreground' : 'text-foreground'
                }`}
            >
                {item.title ?? ''}
            </span>

            {/* Status badge */}
            <span
                data-testid={`plan-item-${rowId}-badge`}
                className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium uppercase tracking-wide ${badgeClass(status)}`}
            >
                {badgeLabel(status)}
            </span>
        </div>
    );
}

// ------------------------------------------------------------------
// Container component
// ------------------------------------------------------------------

interface PlanChecklistProps {
    taskId: string;
}

/**
 * PlanChecklist — renders the agent's current plan on the task-detail page.
 *
 * Fetches `GET /v1/tasks/{taskId}/plan` via `api.getTaskPlan`.
 * Read-only; plan is agent-owned and there is no mutation API.
 * Renders nothing (null) when the plan is empty — an agent that never called
 * `plan_write` is the common case and should produce no visual clutter.
 */
export function PlanChecklist({ taskId }: PlanChecklistProps) {
    const { data, isLoading } = useQuery({
        queryKey: ['task-plan', taskId],
        queryFn: () => api.getTaskPlan(taskId),
        enabled: !!taskId,
    });

    if (isLoading) {
        return null;
    }

    const plan = data?.plan ?? [];

    if (plan.length === 0) {
        // Empty plan — render nothing (common case: agent never called plan_write)
        return null;
    }

    return (
        <div
            data-testid="plan-checklist"
            className="console-surface rounded-[24px] p-5 space-y-1"
        >
            <h3 className="text-xs font-bold uppercase tracking-widest text-muted-foreground mb-3">
                Plan
            </h3>
            <div className="space-y-0.5">
                {plan.map((item: PlanItem, index: number) => (
                    <PlanItemRow
                        key={item.id ?? `index-${index}`}
                        item={item}
                        index={index}
                    />
                ))}
            </div>
        </div>
    );
}
