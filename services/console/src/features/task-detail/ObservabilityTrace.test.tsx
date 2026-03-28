import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { ObservabilityTrace } from './ObservabilityTrace';
import { TaskObservabilityResponse } from '@/types';

afterEach(() => {
    cleanup();
});

describe('ObservabilityTrace', () => {
    it('renders a historical no-trace message for terminal tasks without execution items', () => {
        const observability: TaskObservabilityResponse = {
            enabled: true,
            task_id: 'task-1',
            agent_id: 'agent-1',
            status: 'completed',
            trace_id: null,
            total_cost_microdollars: 0,
            input_tokens: 0,
            output_tokens: 0,
            total_tokens: 0,
            duration_ms: null,
            spans: [],
            items: [],
        };

        render(<ObservabilityTrace observability={observability} />);

        expect(screen.getByText('No execution trace was recorded for this task.')).toBeInTheDocument();
    });

    it('keeps durable markers visible for historical tasks without a trace', () => {
        const observability: TaskObservabilityResponse = {
            enabled: true,
            task_id: 'task-1',
            agent_id: 'agent-1',
            status: 'completed',
            trace_id: null,
            total_cost_microdollars: 0,
            input_tokens: 0,
            output_tokens: 0,
            total_tokens: 0,
            duration_ms: null,
            spans: [],
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
                    cost_microdollars: 0,
                    input_tokens: 0,
                    output_tokens: 0,
                    total_tokens: 0,
                    duration_ms: null,
                    input: null,
                    output: null,
                    started_at: '2026-03-11T00:00:01Z',
                    ended_at: null,
                },
            ],
        };

        render(<ObservabilityTrace observability={observability} />);

        expect(screen.getByText('No traced model or tool calls were recorded for this task. Durable progress is shown below.')).toBeInTheDocument();
        expect(screen.getByText('Durable progress')).toBeInTheDocument();
        expect(screen.getByText('Checkpoint saved')).toBeInTheDocument();
    });

    it('renders unified execution items from spans and runtime markers', () => {
        const observability: TaskObservabilityResponse = {
            enabled: true,
            task_id: 'task-1',
            agent_id: 'agent-1',
            status: 'dead_letter',
            trace_id: 'trace-1',
            total_cost_microdollars: 1500,
            input_tokens: 10,
            output_tokens: 5,
            total_tokens: 15,
            duration_ms: 500,
            spans: [],
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
                    cost_microdollars: 0,
                    input_tokens: 0,
                    output_tokens: 0,
                    total_tokens: 0,
                    duration_ms: null,
                    input: null,
                    output: null,
                    started_at: '2026-03-11T00:00:01Z',
                    ended_at: null,
                },
                {
                    item_id: 'system-1',
                    parent_item_id: null,
                    kind: 'system_span',
                    title: 'Runtime bookkeeping',
                    summary: 'Internal graph routing state',
                    step_number: null,
                    node_name: 'loop',
                    tool_name: null,
                    model_name: null,
                    cost_microdollars: 0,
                    input_tokens: 0,
                    output_tokens: 0,
                    total_tokens: 0,
                    duration_ms: 15,
                    input: null,
                    output: null,
                    started_at: '2026-03-11T00:00:01.500Z',
                    ended_at: '2026-03-11T00:00:01.515Z',
                },
                {
                    item_id: 'span-1',
                    parent_item_id: null,
                    kind: 'tool_span',
                    title: 'Tool: read_url',
                    summary: 'read_url returned content',
                    step_number: 2,
                    node_name: 'loop',
                    tool_name: 'read_url',
                    model_name: null,
                    cost_microdollars: 1500,
                    input_tokens: 0,
                    output_tokens: 0,
                    total_tokens: 0,
                    duration_ms: 500,
                    input: { url: 'https://example.com' },
                    output: { title: 'Example Domain' },
                    started_at: '2026-03-11T00:00:02Z',
                    ended_at: '2026-03-11T00:00:02.500Z',
                },
            ],
        };

        render(<ObservabilityTrace observability={observability} />);

        expect(screen.getByText('Execution')).toBeInTheDocument();
        expect(screen.getByText('Key steps')).toBeInTheDocument();
        expect(screen.getByText('Tool: read_url')).toBeInTheDocument();
        expect(screen.getByText('1 tool call • 1 checkpoint')).toBeInTheDocument();
        expect(screen.getByText('Tool call')).toBeInTheDocument();
        expect(screen.getByText('1 durable save recorded after this step.')).toBeInTheDocument();
        expect(screen.queryByText('Durable progress')).not.toBeInTheDocument();
        expect(screen.queryByText('Checkpoint saved')).not.toBeInTheDocument();
        expect(screen.queryByText('LLM call')).not.toBeInTheDocument();
        expect(screen.queryByText('Runtime bookkeeping')).not.toBeInTheDocument();
        expect(screen.queryByText(/Show runtime internals/i)).not.toBeInTheDocument();
    });
});
