import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SupervisorConfigSection } from '../SupervisorConfigSection';
import type { SupervisorConfig } from '../SupervisorConfigSection';

afterEach(() => {
    cleanup();
});

/** Open the Customize disclosure so the inner fields render. */
function expandCustomize() {
    fireEvent.click(screen.getByRole('button', { name: /customize/i }));
}

/** Open the nested Advanced disclosure (must already be inside Customize). */
function expandAdvanced() {
    fireEvent.click(screen.getByRole('button', { name: /advanced/i }));
}

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
            // Expand everything so all nested copy is in the DOM for the scan.
            expandCustomize();
            expandAdvanced();
            // data-testids carry "supervisor" by design; the rendered TEXT must not.
            expect(container.textContent).not.toMatch(/supervisor/i);
        });
    });

    describe('Customize disclosure (smart defaults, collapsed)', () => {
        it('is collapsed by default — config fields are not rendered', () => {
            render(<SupervisorConfigSection value={undefined} onChange={vi.fn()} applicable />);
            expect(screen.queryByTestId('supervisor-config-max-fanout')).not.toBeInTheDocument();
            expect(screen.queryByTestId('supervisor-config-writer-style')).not.toBeInTheDocument();
        });

        it('shows the smart-defaults summary line when collapsed', () => {
            render(<SupervisorConfigSection value={undefined} onChange={vi.fn()} applicable />);
            expect(screen.getByText(/using smart defaults/i)).toBeInTheDocument();
        });

        it('reveals the config fields after clicking Customize, and hides them again on toggle', () => {
            render(<SupervisorConfigSection value={undefined} onChange={vi.fn()} applicable />);
            expandCustomize();
            expect(screen.getByTestId('supervisor-config-max-fanout')).toBeInTheDocument();
            // Toggle closed again.
            fireEvent.click(screen.getByRole('button', { name: /customize/i }));
            expect(screen.queryByTestId('supervisor-config-max-fanout')).not.toBeInTheDocument();
        });

        it('renders expanded (no Customize toggle) when disabled — read-only detail view', () => {
            render(
                <SupervisorConfigSection
                    value={{ max_fanout_per_iteration: 7 }}
                    onChange={vi.fn()}
                    applicable
                    disabled
                />
            );
            // No disclosure button — fields are directly visible.
            expect(screen.queryByRole('button', { name: /customize/i })).not.toBeInTheDocument();
            expect(screen.getByTestId('supervisor-config-max-fanout')).toBeInTheDocument();
        });
    });

    describe('field rendering + testids', () => {
        it('renders all five fields with their data-testids (Customize + Advanced expanded)', () => {
            render(<SupervisorConfigSection value={undefined} onChange={vi.fn()} applicable />);
            expandCustomize();
            expandAdvanced();
            expect(screen.getByTestId('supervisor-config-max-fanout')).toBeInTheDocument();
            expect(screen.getByTestId('supervisor-config-max-iterations')).toBeInTheDocument();
            expect(screen.getByTestId('supervisor-config-source-allowlist')).toBeInTheDocument();
            expect(screen.getByTestId('supervisor-config-writer-style')).toBeInTheDocument();
            expect(screen.getByTestId('supervisor-config-scope-clarification')).toBeInTheDocument();
        });

        it('max-fanout and max-iterations are number inputs with placeholder defaults (not set values)', () => {
            render(<SupervisorConfigSection value={undefined} onChange={vi.fn()} applicable />);
            expandCustomize();
            const fanout = screen.getByTestId('supervisor-config-max-fanout') as HTMLInputElement;
            const iterations = screen.getByTestId('supervisor-config-max-iterations') as HTMLInputElement;
            expect(fanout).toHaveAttribute('type', 'number');
            expect(iterations).toHaveAttribute('type', 'number');
            // Untouched: empty value, default surfaced via placeholder only.
            expect(fanout.value).toBe('');
            expect(iterations.value).toBe('');
            expect(fanout).toHaveAttribute('placeholder', '5 (default)');
            expect(iterations).toHaveAttribute('placeholder', '3 (default)');
        });

        it('renders existing values when set', () => {
            const value: SupervisorConfig = {
                max_fanout_per_iteration: 7,
                max_iterations: 4,
                source_allowlist: ['web_search'],
                writer_style: 'annotated_bullets',
                scope_clarification_enabled: true,
            };
            render(<SupervisorConfigSection value={value} onChange={vi.fn()} applicable />);
            expandCustomize();
            expandAdvanced();
            expect((screen.getByTestId('supervisor-config-max-fanout') as HTMLInputElement).value).toBe('7');
            expect((screen.getByTestId('supervisor-config-max-iterations') as HTMLInputElement).value).toBe('4');
            expect((screen.getByTestId('supervisor-config-writer-style') as HTMLSelectElement).value).toBe('annotated_bullets');
            expect(screen.getByTestId('supervisor-config-scope-clarification')).toBeChecked();
        });
    });

    describe('report format (writer_style)', () => {
        it('offers exactly the two labeled enum values, no platform-default option, defaulting to formal_report', () => {
            render(<SupervisorConfigSection value={undefined} onChange={vi.fn()} applicable />);
            expandCustomize();
            const select = screen.getByTestId('supervisor-config-writer-style') as HTMLSelectElement;
            const optionValues = Array.from(select.options).map((o) => o.value);
            expect(optionValues).toEqual(['formal_report', 'annotated_bullets']);
            expect(select.value).toBe('formal_report');
            expect(screen.getByRole('option', { name: 'Formal report' })).toBeInTheDocument();
            expect(screen.getByRole('option', { name: 'Annotated bullets' })).toBeInTheDocument();
        });

        it('shows a description for the selected report format', () => {
            render(<SupervisorConfigSection value={undefined} onChange={vi.fn()} applicable />);
            expandCustomize();
            expect(screen.getByText(/reads like a written briefing/i)).toBeInTheDocument();
        });

        it('fires onChange with the enum value when writer_style changes', () => {
            const onChange = vi.fn();
            render(<SupervisorConfigSection value={undefined} onChange={onChange} applicable />);
            expandCustomize();
            fireEvent.change(screen.getByTestId('supervisor-config-writer-style'), {
                target: { value: 'annotated_bullets' },
            });
            expect(onChange).toHaveBeenCalledWith(
                expect.objectContaining({ writer_style: 'annotated_bullets' })
            );
        });
    });

    describe('Allowed Sources checklist (under Advanced)', () => {
        it('renders base web sources + passed tool servers as checkboxes, with "All" default-checked', () => {
            render(
                <SupervisorConfigSection
                    value={undefined}
                    onChange={vi.fn()}
                    applicable
                    toolServers={[{ name: 'internal_docs' }, { name: 'sales_db' }]}
                />
            );
            expandCustomize();
            expandAdvanced();
            const container = screen.getByTestId('supervisor-config-source-allowlist');
            expect(screen.getByLabelText('All available sources')).toBeChecked();
            expect(screen.getByLabelText('Web search')).toBeInTheDocument();
            expect(screen.getByLabelText('Read web page')).toBeInTheDocument();
            expect(screen.getByLabelText('internal_docs')).toBeInTheDocument();
            expect(screen.getByLabelText('sales_db')).toBeInTheDocument();
            expect(container).toBeInTheDocument();
        });

        it('checking a specific source unchecks "All" and emits source_allowlist with that name', () => {
            const onChange = vi.fn();
            render(<SupervisorConfigSection value={undefined} onChange={onChange} applicable />);
            expandCustomize();
            expandAdvanced();
            fireEvent.click(screen.getByLabelText('Web search'));
            expect(onChange).toHaveBeenCalledWith(
                expect.objectContaining({ source_allowlist: ['web_search'] })
            );
        });

        it('unchecking the last specific source reverts to "All" (clears source_allowlist)', () => {
            const onChange = vi.fn();
            render(
                <SupervisorConfigSection
                    value={{ source_allowlist: ['web_search'] }}
                    onChange={onChange}
                    applicable
                />
            );
            expandCustomize();
            expandAdvanced();
            expect(screen.getByLabelText('All available sources')).not.toBeChecked();
            expect(screen.getByLabelText('Web search')).toBeChecked();
            // Uncheck the only selected source.
            fireEvent.click(screen.getByLabelText('Web search'));
            const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1][0];
            expect(lastCall.source_allowlist).toBeUndefined();
        });

        it('clicking "All available sources" while specific sources are checked clears them', () => {
            const onChange = vi.fn();
            render(
                <SupervisorConfigSection
                    value={{ source_allowlist: ['web_search', 'read_url'] }}
                    onChange={onChange}
                    applicable
                />
            );
            expandCustomize();
            expandAdvanced();
            fireEvent.click(screen.getByLabelText('All available sources'));
            const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1][0];
            expect(lastCall.source_allowlist).toBeUndefined();
        });
    });

    describe('onChange callbacks', () => {
        it('fires onChange when max_fanout changes', () => {
            const onChange = vi.fn();
            render(<SupervisorConfigSection value={undefined} onChange={onChange} applicable />);
            expandCustomize();
            fireEvent.change(screen.getByTestId('supervisor-config-max-fanout'), { target: { value: '5' } });
            expect(onChange).toHaveBeenCalledWith(
                expect.objectContaining({ max_fanout_per_iteration: 5 })
            );
        });

        it('fires onChange when max_iterations changes', () => {
            const onChange = vi.fn();
            render(<SupervisorConfigSection value={undefined} onChange={onChange} applicable />);
            expandCustomize();
            fireEvent.change(screen.getByTestId('supervisor-config-max-iterations'), { target: { value: '3' } });
            expect(onChange).toHaveBeenCalledWith(
                expect.objectContaining({ max_iterations: 3 })
            );
        });

        it('fires onChange when scope_clarification is toggled', () => {
            const onChange = vi.fn();
            render(<SupervisorConfigSection value={undefined} onChange={onChange} applicable />);
            expandCustomize();
            fireEvent.click(screen.getByTestId('supervisor-config-scope-clarification'));
            expect(onChange).toHaveBeenCalledWith(
                expect.objectContaining({ scope_clarification_enabled: true })
            );
        });

        it('does not fire onChange on initial mount or on expand', () => {
            const onChange = vi.fn();
            render(<SupervisorConfigSection value={undefined} onChange={onChange} applicable />);
            expandCustomize();
            expandAdvanced();
            expect(onChange).not.toHaveBeenCalled();
        });
    });

    describe('disabled (read-only-on-edit posture)', () => {
        it('disables every control and renders expanded', () => {
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
            // Source checklist is read-only: the persisted source is shown checked + disabled.
            expect(screen.getByLabelText('Web search')).toBeChecked();
            expect(screen.getByLabelText('Web search')).toBeDisabled();
        });
    });
});
