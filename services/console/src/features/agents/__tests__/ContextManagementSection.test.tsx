import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ContextManagementSection } from '../ContextManagementSection';
import type { ContextManagementConfig } from '../ContextManagementSection';
import type { ModelResponse } from '@/types';

const MOCK_MODELS: ModelResponse[] = [
    { provider: 'anthropic', model_id: 'claude-haiku-4-5', display_name: 'Claude Haiku 4.5' },
    { provider: 'anthropic', model_id: 'claude-sonnet-4-5', display_name: 'Claude Sonnet 4.5' },
    { provider: 'openai', model_id: 'gpt-4o', display_name: 'GPT-4o' },
];

afterEach(() => {
    cleanup();
});

describe('ContextManagementSection', () => {
    describe('rendering', () => {
        it('renders section header copy describing always-on infrastructure', () => {
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            expect(
                screen.getByText(/Context management is always-on platform infrastructure/i)
            ).toBeInTheDocument();
        });

        it('renders summarizer_model select with correct testid', () => {
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            expect(
                screen.getByTestId('context-management-summarizer-model')
            ).toBeInTheDocument();
        });

        it('renders exclude_tools input with correct testid', () => {
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            expect(
                screen.getByTestId('context-management-exclude-tools')
            ).toBeInTheDocument();
        });

        it('renders pre_tier3_memory_flush toggle with correct testid', () => {
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            expect(
                screen.getByTestId('context-management-pre-tier3-flush')
            ).toBeInTheDocument();
        });

        it('renders fields in order: summarizer_model -> exclude_tools -> pre_tier3_memory_flush', () => {
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            const summarizerModel = screen.getByTestId('context-management-summarizer-model');
            const excludeTools = screen.getByTestId('context-management-exclude-tools');
            const flushToggle = screen.getByTestId('context-management-pre-tier3-flush');

            // Use DOM order via compareDocumentPosition
            expect(
                summarizerModel.compareDocumentPosition(excludeTools) & Node.DOCUMENT_POSITION_FOLLOWING
            ).toBeTruthy();
            expect(
                excludeTools.compareDocumentPosition(flushToggle) & Node.DOCUMENT_POSITION_FOLLOWING
            ).toBeTruthy();
        });

        it('does NOT render an enabled toggle', () => {
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            expect(
                screen.queryByText(/enable context management/i)
            ).not.toBeInTheDocument();
            expect(
                screen.queryByTestId('context-management-enabled')
            ).not.toBeInTheDocument();
        });

        it('populates summarizer_model dropdown with available models', () => {
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            expect(screen.getByRole('option', { name: 'Claude Haiku 4.5' })).toBeInTheDocument();
            expect(screen.getByRole('option', { name: 'Claude Sonnet 4.5' })).toBeInTheDocument();
            expect(screen.getByRole('option', { name: 'GPT-4o' })).toBeInTheDocument();
        });

        it('renders existing exclude_tools chips when value is provided', () => {
            render(
                <ContextManagementSection
                    value={{ exclude_tools: ['web_search', 'sandbox_exec'] }}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            expect(screen.getByText('web_search')).toBeInTheDocument();
            expect(screen.getByText('sandbox_exec')).toBeInTheDocument();
        });

        it('renders pre_tier3_memory_flush checked when value is true', () => {
            render(
                <ContextManagementSection
                    value={{ pre_tier3_memory_flush: true }}
                    memoryEnabled={true}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            const checkbox = screen.getByTestId('context-management-pre-tier3-flush');
            expect(checkbox).toBeChecked();
        });

        it('pre_tier3_memory_flush is unchecked by default when value is undefined', () => {
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={true}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            const checkbox = screen.getByTestId('context-management-pre-tier3-flush');
            expect(checkbox).not.toBeChecked();
        });
    });

    describe('onChange callbacks', () => {
        it('calls onChange with updated summarizer_model when dropdown changes', () => {
            const handleChange = vi.fn();
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={handleChange}
                />
            );
            const select = screen.getByTestId('context-management-summarizer-model');
            fireEvent.change(select, { target: { value: 'claude-haiku-4-5' } });
            expect(handleChange).toHaveBeenCalledWith(
                expect.objectContaining({ summarizer_model: 'claude-haiku-4-5' })
            );
        });

        it('calls onChange with updated exclude_tools when a new tool is added', () => {
            const handleChange = vi.fn();
            render(
                <ContextManagementSection
                    value={{ exclude_tools: ['web_search'] }}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={handleChange}
                />
            );
            const input = screen.getByPlaceholderText(/add tool name/i);
            fireEvent.change(input, { target: { value: 'sandbox_exec' } });
            fireEvent.keyDown(input, { key: 'Enter' });
            expect(handleChange).toHaveBeenCalledWith(
                expect.objectContaining({
                    exclude_tools: expect.arrayContaining(['web_search', 'sandbox_exec']),
                })
            );
        });

        it('calls onChange with updated pre_tier3_memory_flush when toggle is clicked', () => {
            const handleChange = vi.fn();
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={true}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={handleChange}
                />
            );
            const checkbox = screen.getByTestId('context-management-pre-tier3-flush');
            fireEvent.click(checkbox);
            expect(handleChange).toHaveBeenCalledWith(
                expect.objectContaining({ pre_tier3_memory_flush: true })
            );
        });

        it('calls onChange removing a chip when delete button is clicked', () => {
            const handleChange = vi.fn();
            render(
                <ContextManagementSection
                    value={{ exclude_tools: ['web_search', 'sandbox_exec'] }}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={handleChange}
                />
            );
            // Find the remove button for 'web_search'
            const webSearchChip = screen.getByText('web_search').closest('[data-chip]') as HTMLElement;
            const removeBtn = webSearchChip?.querySelector('button') as HTMLElement;
            fireEvent.click(removeBtn);
            expect(handleChange).toHaveBeenCalledWith(
                expect.objectContaining({
                    exclude_tools: ['sandbox_exec'],
                })
            );
        });
    });

    describe('pre_tier3_memory_flush tooltip when memory disabled', () => {
        it('disables the toggle when memoryEnabled is false', () => {
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            const checkbox = screen.getByTestId('context-management-pre-tier3-flush');
            expect(checkbox).toBeDisabled();
        });

        it('does not disable the toggle when memoryEnabled is true', () => {
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={true}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            const checkbox = screen.getByTestId('context-management-pre-tier3-flush');
            expect(checkbox).not.toBeDisabled();
        });

        it('renders tooltip text about requiring memory when memoryEnabled is false', () => {
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            expect(
                screen.getByText(/Requires memory to be enabled/i)
            ).toBeInTheDocument();
        });
    });

    describe('exclude_tools cap at 50', () => {
        it('shows inline error when attempting to add a 51st entry', () => {
            const fiftyTools = Array.from({ length: 50 }, (_, i) => `tool_${i}`);
            render(
                <ContextManagementSection
                    value={{ exclude_tools: fiftyTools }}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            const input = screen.getByPlaceholderText(/add tool name/i);
            fireEvent.change(input, { target: { value: 'tool_51' } });
            fireEvent.keyDown(input, { key: 'Enter' });
            expect(screen.getByText(/Maximum 50 entries/i)).toBeInTheDocument();
        });

        it('does not call onChange when at cap', () => {
            const fiftyTools = Array.from({ length: 50 }, (_, i) => `tool_${i}`);
            const handleChange = vi.fn();
            render(
                <ContextManagementSection
                    value={{ exclude_tools: fiftyTools }}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={handleChange}
                />
            );
            const input = screen.getByPlaceholderText(/add tool name/i);
            fireEvent.change(input, { target: { value: 'tool_51' } });
            fireEvent.keyDown(input, { key: 'Enter' });
            expect(handleChange).not.toHaveBeenCalled();
        });

        it('shows current count when tools are present', () => {
            const tools = ['web_search', 'sandbox_exec'];
            render(
                <ContextManagementSection
                    value={{ exclude_tools: tools }}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={vi.fn()}
                />
            );
            expect(screen.getByText(/2\s*\/\s*50/)).toBeInTheDocument();
        });
    });

    describe('payload shape: dont-send-defaults-on-save', () => {
        it('preserves existing value config shape when summarizer_model is set', () => {
            const handleChange = vi.fn();
            const existingValue: ContextManagementConfig = {
                summarizer_model: 'claude-haiku-4-5',
                exclude_tools: ['web_search'],
                pre_tier3_memory_flush: true,
            };
            render(
                <ContextManagementSection
                    value={existingValue}
                    memoryEnabled={true}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={handleChange}
                />
            );

            // Verify all fields render with their existing values
            const select = screen.getByTestId('context-management-summarizer-model') as HTMLSelectElement;
            expect(select.value).toBe('claude-haiku-4-5');

            expect(screen.getByText('web_search')).toBeInTheDocument();

            const checkbox = screen.getByTestId('context-management-pre-tier3-flush');
            expect(checkbox).toBeChecked();
        });

        it('does not call onChange on initial mount (no spurious updates)', () => {
            const handleChange = vi.fn();
            render(
                <ContextManagementSection
                    value={undefined}
                    memoryEnabled={false}
                    availableSummarizerModels={MOCK_MODELS}
                    onChange={handleChange}
                />
            );
            expect(handleChange).not.toHaveBeenCalled();
        });
    });
});
