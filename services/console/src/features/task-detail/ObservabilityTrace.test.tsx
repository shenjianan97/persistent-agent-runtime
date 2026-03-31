import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { ObservabilityTrace } from './ObservabilityTrace';
import { TaskObservabilityResponse } from '@/types';

afterEach(() => {
    cleanup();
});

describe('ObservabilityTrace', () => {
    it('renders a no-trace message for terminal tasks without execution items', () => {
        const observability: TaskObservabilityResponse = {
            enabled: true,
            task_id: 'task-1',
            agent_id: 'agent-1',
            status: 'completed',
            total_cost_microdollars: 0,
            input_tokens: 0,
            output_tokens: 0,
            total_tokens: 0,
            duration_ms: null,
            items: [],
        };

        render(<ObservabilityTrace observability={observability} />);

        expect(screen.getByText('No execution trace was recorded for this task.')).toBeInTheDocument();
    });

    it('renders checkpoint items for completed tasks', () => {
        const observability: TaskObservabilityResponse = {
            enabled: true,
            task_id: 'task-1',
            agent_id: 'agent-1',
            status: 'completed',
            total_cost_microdollars: 500,
            input_tokens: 10,
            output_tokens: 5,
            total_tokens: 15,
            duration_ms: null,
            items: [
                {
                    item_id: 'checkpoint-1',
                    parent_item_id: null,
                    kind: 'checkpoint_persisted',
                    title: 'Checkpoint saved',
                    summary: 'Saved durable progress at step 1.',
                    step_number: 1,
                    node_name: 'input',
                    tool_name: null,
                    model_name: null,
                    cost_microdollars: 500,
                    input_tokens: 10,
                    output_tokens: 5,
                    total_tokens: 15,
                    duration_ms: null,
                    input: null,
                    output: null,
                    started_at: '2026-03-11T00:00:01Z',
                    ended_at: null,
                },
            ],
        };

        render(<ObservabilityTrace observability={observability} />);

        expect(screen.getByText('Execution')).toBeInTheDocument();
        expect(screen.getByText('Checkpoint saved')).toBeInTheDocument();
        expect(screen.getByText('Checkpoint')).toBeInTheDocument();
        expect(screen.getByText('1 checkpoint')).toBeInTheDocument();
    });

    it('renders completed and retry items alongside checkpoints', () => {
        const observability: TaskObservabilityResponse = {
            enabled: true,
            task_id: 'task-1',
            agent_id: 'agent-1',
            status: 'completed',
            total_cost_microdollars: 1000,
            input_tokens: 20,
            output_tokens: 10,
            total_tokens: 30,
            duration_ms: 500,
            items: [
                {
                    item_id: 'checkpoint-1',
                    parent_item_id: null,
                    kind: 'checkpoint_persisted',
                    title: 'Checkpoint saved',
                    summary: 'Saved durable progress at step 1.',
                    step_number: 1,
                    node_name: 'input',
                    tool_name: null,
                    model_name: null,
                    cost_microdollars: 500,
                    input_tokens: 10,
                    output_tokens: 5,
                    total_tokens: 15,
                    duration_ms: null,
                    input: null,
                    output: null,
                    started_at: '2026-03-11T00:00:01Z',
                    ended_at: null,
                },
                {
                    item_id: 'completed-1',
                    parent_item_id: null,
                    kind: 'completed',
                    title: 'Task completed',
                    summary: 'Execution finished successfully.',
                    step_number: null,
                    node_name: null,
                    tool_name: null,
                    model_name: null,
                    cost_microdollars: 0,
                    input_tokens: 0,
                    output_tokens: 0,
                    total_tokens: 0,
                    duration_ms: null,
                    input: null,
                    output: null,
                    started_at: '2026-03-11T00:00:02Z',
                    ended_at: null,
                },
            ],
        };

        render(<ObservabilityTrace observability={observability} />);

        expect(screen.getByText('Checkpoint saved')).toBeInTheDocument();
        expect(screen.getByText('Task completed')).toBeInTheDocument();
        expect(screen.getByText('Completed')).toBeInTheDocument();
    });
});
