import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { CheckpointTimeline } from '../CheckpointTimeline';
import type { CheckpointResponse, TaskEventResponse } from '@/types';

const baseCheckpoint: CheckpointResponse = {
    checkpoint_id: 'cp-1',
    task_id: 'task-1',
    step_number: 1,
    node_name: 'agent',
    worker_id: 'worker-1',
    cost_microdollars: 0,
    created_at: '2026-03-11T00:00:01Z',
    event: { type: 'input', title: 'User Input', summary: 'demo' },
};

const compactionEvent: TaskEventResponse = {
    event_id: 'ev-compaction',
    task_id: 'task-1',
    agent_id: 'agent-1',
    event_type: 'task_compaction_fired',
    created_at: '2026-03-11T00:00:05Z',
    details: {
        tier: 3,
        summarizer_model_id: 'claude-haiku-4-5',
        tokens_in: 91426,
        tokens_out: 1445,
        turns_summarized: 32,
        first_turn_index: 10,
        last_turn_index: 42,
        summary_bytes: 1800,
    },
};

describe('CheckpointTimeline — compaction marker', () => {
    afterEach(cleanup);

    it('renders task_compaction_fired with Context Compacted label and detail text', () => {
        render(
            <CheckpointTimeline
                checkpoints={[baseCheckpoint]}
                hitlEvents={[compactionEvent]}
                isRunning={false}
            />,
        );
        expect(screen.getByText('Context Compacted')).toBeInTheDocument();
        expect(
            screen.getByText(/32 turns \(10→42\).*91,426 in → 1,445 out.*claude-haiku-4-5/),
        ).toBeInTheDocument();
    });

    it('orders the compaction marker chronologically with checkpoints', () => {
        const laterCheckpoint: CheckpointResponse = {
            ...baseCheckpoint,
            checkpoint_id: 'cp-2',
            step_number: 2,
            created_at: '2026-03-11T00:00:10Z',
        };
        render(
            <CheckpointTimeline
                checkpoints={[baseCheckpoint, laterCheckpoint]}
                hitlEvents={[compactionEvent]}
                isRunning={false}
            />,
        );
        // Rendered order: cp-1 (00:01) → compaction (00:05) → cp-2 (00:10)
        const headings = screen.getAllByText(/Step \d|Context Compacted/);
        expect(headings.map((n) => n.textContent)).toEqual([
            'Step 1',
            'Context Compacted',
            'Step 2',
        ]);
    });
});
