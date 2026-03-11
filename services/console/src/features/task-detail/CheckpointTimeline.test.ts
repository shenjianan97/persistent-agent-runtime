import { describe, expect, it } from 'vitest';

import { getResumeMarkers, getTerminalFailureMarker } from './CheckpointTimeline';
import { CheckpointResponse } from '@/types';

const checkpoints: CheckpointResponse[] = [
    {
        checkpoint_id: 'cp-1',
        task_id: 'task-1',
        step_number: 1,
        node_name: 'input',
        worker_id: 'worker-1',
        cost_microdollars: 0,
        created_at: '2026-03-11T00:00:01Z',
        event: {
            type: 'input',
            title: 'User Input',
            summary: 'demo',
        },
    },
    {
        checkpoint_id: 'cp-2',
        task_id: 'task-1',
        step_number: 2,
        node_name: 'loop',
        worker_id: 'worker-1',
        cost_microdollars: 0,
        created_at: '2026-03-11T00:00:02Z',
        event: {
            type: 'tool_call',
            title: 'Tool Call: read_url',
            summary: 'call',
        },
    },
    {
        checkpoint_id: 'cp-3',
        task_id: 'task-1',
        step_number: 3,
        node_name: 'loop',
        worker_id: 'worker-2',
        cost_microdollars: 0,
        created_at: '2026-03-11T00:00:04Z',
        event: {
            type: 'tool_result',
            title: 'Tool Result: read_url',
            summary: 'result',
        },
    },
];

describe('getResumeMarkers', () => {
    it('marks the first checkpoint written after a retry as a resumed attempt', () => {
        const markers = getResumeMarkers(checkpoints, ['2026-03-11T00:00:03Z']);

        expect(markers.get('cp-3')).toEqual({
            resumedAfterStep: 2,
        });
    });

    it('skips retries that do not produce a checkpoint before the next retry', () => {
        const markers = getResumeMarkers(checkpoints, [
            '2026-03-11T00:00:02.500Z',
            '2026-03-11T00:00:03.500Z',
        ]);

        expect(markers.has('cp-3')).toBe(true);
        expect(markers.get('cp-3')).toEqual({
            resumedAfterStep: 2,
        });
    });
});

describe('getTerminalFailureMarker', () => {
    it('marks failures that happened after the last saved checkpoint', () => {
        expect(
            getTerminalFailureMarker(
                checkpoints,
                'dead_letter',
                ['2026-03-11T00:00:05Z'],
                'retries_exhausted',
                'retryable_error',
                'network down',
                '2026-03-11T00:00:06Z',
            ),
        ).toEqual({
            failedAfterStep: 3,
            reason: 'retries_exhausted',
            errorCode: 'retryable_error',
            failedAt: '2026-03-11T00:00:06Z',
            failedBeforeNextCheckpoint: true,
        });
    });
});
