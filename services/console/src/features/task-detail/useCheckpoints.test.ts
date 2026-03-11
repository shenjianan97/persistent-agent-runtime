import { describe, expect, it } from 'vitest';

import { getCheckpointRefetchInterval } from './useCheckpoints';

describe('getCheckpointRefetchInterval', () => {
    it('keeps polling while the task is active', () => {
        expect(getCheckpointRefetchInterval('running', 2, 0)).toBe(3000);
        expect(getCheckpointRefetchInterval('queued', 2, 0)).toBe(3000);
    });

    it('keeps polling after terminal transition until checkpoint count catches up', () => {
        expect(getCheckpointRefetchInterval('dead_letter', 2, 0)).toBe(1000);
        expect(getCheckpointRefetchInterval('completed', 5, 3)).toBe(1000);
    });

    it('stops polling once terminal-state checkpoints are fully loaded', () => {
        expect(getCheckpointRefetchInterval('dead_letter', 2, 2)).toBe(false);
        expect(getCheckpointRefetchInterval('completed', 5, 5)).toBe(false);
    });
});
