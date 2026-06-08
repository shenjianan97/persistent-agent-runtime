import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SupervisorConfigSection } from '../SupervisorConfigSection';
import type { SupervisorConfig } from '../SupervisorConfigSection';

afterEach(() => {
    cleanup();
});

describe('SupervisorConfigSection', () => {
    describe('applicability gate', () => {
        it('renders nothing when applicable is false', () => {
            const { container } = render(
                <SupervisorConfigSection value={undefined} onChange={vi.fn()} applicable={false} />
            );
            expect(container.firstChild).toBeNull();
        });

        it('renders the section header when applicable', () => {
            render(<SupervisorConfigSection value={undefined} onChange={vi.fn()} applicable />);
            expect(screen.getByText('Deep Research Configuration')).toBeInTheDocument();
        });

        it('never leaks the internal word "Supervisor" into visible copy', () => {
            const { container } = render(
                <SupervisorConfigSection
                    value={{ source_allowlist: ['web_search'], writer_style: 'formal_report' }}
                    onChange={vi.fn()}
                    applicable
                />
            );
            // data-testids carry "supervisor" by design; the rendered TEXT must not.
            expect(container.textContent).not.toMatch(/supervisor/i);
        });
    });

    describe('field rendering + testids', () => {
        it('renders all five fields with their data-testids', () => {
            render(<SupervisorConfigSection value={undefined} onChange={vi.fn()} applicable />);
            expect(screen.getByTestId('supervisor-config-max-fanout')).toBeInTheDocument();
            expect(screen.getByTestId('supervisor-config-max-iterations')).toBeInTheDocument();
            expect(screen.getByTestId('supervisor-config-source-allowlist')).toBeInTheDocument();
            expect(screen.getByTestId('supervisor-config-writer-style')).toBeInTheDocument();
            expect(screen.getByTestId('supervisor-config-scope-clarification')).toBeInTheDocument();
        });

        it('renders fields in spec order', () => {
            render(<SupervisorConfigSection value={undefined} onChange={vi.fn()} applicable />);
            const fanout = screen.getByTestId('supervisor-config-max-fanout');
            const iterations = screen.getByTestId('supervisor-config-max-iterations');
            const allowlist = screen.getByTestId('supervisor-config-source-allowlist');
            const style = screen.getByTestId('supervisor-config-writer-style');
            const scope = screen.getByTestId('supervisor-config-scope-clarification');
            expect(fanout.compareDocumentPosition(iterations) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
            expect(iterations.compareDocumentPosition(allowlist) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
            expect(allowlist.compareDocumentPosition(style) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
            expect(style.compareDocumentPosition(scope) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        });

        it('max-fanout and max-iterations are number inputs', () => {
            render(<SupervisorConfigSection value={undefined} onChange={vi.fn()} applicable />);
            expect(screen.getByTestId('supervisor-config-max-fanout')).toHaveAttribute('type', 'number');
            expect(screen.getByTestId('supervisor-config-max-iterations')).toHaveAttribute('type', 'number');
        });

        it('writer_style offers exactly the two enum values plus a default', () => {
            render(<SupervisorConfigSection value={undefined} onChange={vi.fn()} applicable />);
            const select = screen.getByTestId('supervisor-config-writer-style') as HTMLSelectElement;
            const optionValues = Array.from(select.options).map((o) => o.value);
            expect(optionValues).toEqual(['', 'formal_report', 'annotated_bullets']);
            expect(screen.getByRole('option', { name: 'Formal report' })).toBeInTheDocument();
            expect(screen.getByRole('option', { name: 'Annotated bullets' })).toBeInTheDocument();
        });

        it('renders existing values', () => {
            const value: SupervisorConfig = {
                max_fanout_per_iteration: 7,
                max_iterations: 4,
                source_allowlist: ['web_search', 'docs'],
                writer_style: 'annotated_bullets',
                scope_clarification_enabled: true,
            };
            render(<SupervisorConfigSection value={value} onChange={vi.fn()} applicable />);
            expect((screen.getByTestId('supervisor-config-max-fanout') as HTMLInputElement).value).toBe('7');
            expect((screen.getByTestId('supervisor-config-max-iterations') as HTMLInputElement).value).toBe('4');
            expect((screen.getByTestId('supervisor-config-writer-style') as HTMLSelectElement).value).toBe('annotated_bullets');
            expect(screen.getByTestId('supervisor-config-scope-clarification')).toBeChecked();
            expect(screen.getByText('web_search')).toBeInTheDocument();
            expect(screen.getByText('docs')).toBeInTheDocument();
        });
    });

    describe('onChange callbacks', () => {
        it('fires onChange when max_fanout changes', () => {
            const onChange = vi.fn();
            render(<SupervisorConfigSection value={undefined} onChange={onChange} applicable />);
            fireEvent.change(screen.getByTestId('supervisor-config-max-fanout'), { target: { value: '5' } });
            expect(onChange).toHaveBeenCalledWith(
                expect.objectContaining({ max_fanout_per_iteration: 5 })
            );
        });

        it('fires onChange when max_iterations changes', () => {
            const onChange = vi.fn();
            render(<SupervisorConfigSection value={undefined} onChange={onChange} applicable />);
            fireEvent.change(screen.getByTestId('supervisor-config-max-iterations'), { target: { value: '3' } });
            expect(onChange).toHaveBeenCalledWith(
                expect.objectContaining({ max_iterations: 3 })
            );
        });

        it('fires onChange with the enum value when writer_style changes', () => {
            const onChange = vi.fn();
            render(<SupervisorConfigSection value={undefined} onChange={onChange} applicable />);
            fireEvent.change(screen.getByTestId('supervisor-config-writer-style'), {
                target: { value: 'formal_report' },
            });
            expect(onChange).toHaveBeenCalledWith(
                expect.objectContaining({ writer_style: 'formal_report' })
            );
        });

        it('fires onChange when scope_clarification is toggled', () => {
            const onChange = vi.fn();
            render(<SupervisorConfigSection value={undefined} onChange={onChange} applicable />);
            fireEvent.click(screen.getByTestId('supervisor-config-scope-clarification'));
            expect(onChange).toHaveBeenCalledWith(
                expect.objectContaining({ scope_clarification_enabled: true })
            );
        });

        it('fires onChange adding a source chip on Enter', () => {
            const onChange = vi.fn();
            render(
                <SupervisorConfigSection value={{ source_allowlist: ['web_search'] }} onChange={onChange} applicable />
            );
            const input = screen.getByPlaceholderText(/add source name/i);
            fireEvent.change(input, { target: { value: 'internal_docs' } });
            fireEvent.keyDown(input, { key: 'Enter' });
            expect(onChange).toHaveBeenCalledWith(
                expect.objectContaining({
                    source_allowlist: expect.arrayContaining(['web_search', 'internal_docs']),
                })
            );
        });

        it('fires onChange removing a source chip', () => {
            const onChange = vi.fn();
            render(
                <SupervisorConfigSection
                    value={{ source_allowlist: ['web_search', 'docs'] }}
                    onChange={onChange}
                    applicable
                />
            );
            const chip = screen.getByText('web_search').closest('[data-chip]') as HTMLElement;
            const removeBtn = chip.querySelector('button') as HTMLElement;
            fireEvent.click(removeBtn);
            expect(onChange).toHaveBeenCalledWith(
                expect.objectContaining({ source_allowlist: ['docs'] })
            );
        });

        it('does not fire onChange on initial mount', () => {
            const onChange = vi.fn();
            render(<SupervisorConfigSection value={undefined} onChange={onChange} applicable />);
            expect(onChange).not.toHaveBeenCalled();
        });
    });

    describe('source_allowlist cap at 50', () => {
        it('shows inline error when attempting to add a 51st entry', () => {
            const fifty = Array.from({ length: 50 }, (_, i) => `source_${i}`);
            render(<SupervisorConfigSection value={{ source_allowlist: fifty }} onChange={vi.fn()} applicable />);
            const input = screen.getByPlaceholderText(/add source name/i);
            fireEvent.change(input, { target: { value: 'source_51' } });
            fireEvent.keyDown(input, { key: 'Enter' });
            expect(screen.getByText(/Maximum 50 entries/i)).toBeInTheDocument();
        });

        it('does not call onChange at cap', () => {
            const fifty = Array.from({ length: 50 }, (_, i) => `source_${i}`);
            const onChange = vi.fn();
            render(<SupervisorConfigSection value={{ source_allowlist: fifty }} onChange={onChange} applicable />);
            const input = screen.getByPlaceholderText(/add source name/i);
            fireEvent.change(input, { target: { value: 'source_51' } });
            fireEvent.keyDown(input, { key: 'Enter' });
            expect(onChange).not.toHaveBeenCalled();
        });

        it('shows the current count', () => {
            render(
                <SupervisorConfigSection
                    value={{ source_allowlist: ['a', 'b', 'c'] }}
                    onChange={vi.fn()}
                    applicable
                />
            );
            expect(screen.getByText(/3\s*\/\s*50/)).toBeInTheDocument();
        });
    });

    describe('disabled (read-only-on-edit posture)', () => {
        it('disables every control', () => {
            render(
                <SupervisorConfigSection
                    value={{ source_allowlist: ['web_search'], writer_style: 'formal_report' }}
                    onChange={vi.fn()}
                    applicable
                    disabled
                />
            );
            expect(screen.getByTestId('supervisor-config-max-fanout')).toBeDisabled();
            expect(screen.getByTestId('supervisor-config-max-iterations')).toBeDisabled();
            expect(screen.getByTestId('supervisor-config-writer-style')).toBeDisabled();
            expect(screen.getByTestId('supervisor-config-scope-clarification')).toBeDisabled();
            // chip add-input + remove buttons are not rendered when disabled
            expect(screen.queryByPlaceholderText(/add source name/i)).not.toBeInTheDocument();
            expect(screen.queryByLabelText(/remove web_search/i)).not.toBeInTheDocument();
            // chips themselves remain visible (read-only display)
            expect(screen.getByText('web_search')).toBeInTheDocument();
        });
    });
});
