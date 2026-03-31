import { cleanup, render, screen, fireEvent } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LangfuseEndpointDialog } from './LangfuseEndpointDialog';
import type { LangfuseEndpoint, LangfuseEndpointRequest } from '@/types';
import { useTestLangfuseEndpoint } from './useLangfuseEndpoints';

vi.mock('./useLangfuseEndpoints', () => ({
    useTestLangfuseEndpoint: vi.fn(),
}));

const mockUseTestLangfuseEndpoint = vi.mocked(useTestLangfuseEndpoint);

const sampleEndpoint: LangfuseEndpoint = {
    endpoint_id: 'ep-1',
    tenant_id: 'tenant-1',
    name: 'Production Langfuse',
    host: 'https://langfuse.example.com',
    created_at: '2026-01-15T00:00:00Z',
    updated_at: '2026-01-15T00:00:00Z',
};

const defaultProps = {
    open: true,
    onClose: vi.fn(),
    onSubmit: vi.fn(),
    isPending: false,
    endpoint: null,
};

beforeEach(() => {
    mockUseTestLangfuseEndpoint.mockReturnValue({
        mutate: vi.fn(),
        isPending: false,
    } as unknown as ReturnType<typeof useTestLangfuseEndpoint>);
});

afterEach(() => {
    cleanup();
    vi.clearAllMocks();
});

describe('LangfuseEndpointDialog', () => {
    it('renders create dialog with empty fields', () => {
        render(<LangfuseEndpointDialog {...defaultProps} />);

        // Submit button says "Create" in create mode
        expect(screen.getByRole('button', { name: /create/i })).toBeInTheDocument();

        // Name and Host fields are present and empty
        const inputs = screen.getAllByRole('textbox');
        inputs.forEach((input) => {
            expect(input).toHaveValue('');
        });

        // No "Test Connection" button in create mode
        expect(screen.queryByText('Test Connection')).not.toBeInTheDocument();
    });

    it('renders edit dialog with pre-filled fields', () => {
        render(<LangfuseEndpointDialog {...defaultProps} endpoint={sampleEndpoint} />);

        // Submit button says "Update" in edit mode
        expect(screen.getByRole('button', { name: /update/i })).toBeInTheDocument();

        // Name and Host fields are pre-filled
        expect(screen.getByDisplayValue('Production Langfuse')).toBeInTheDocument();
        expect(screen.getByDisplayValue('https://langfuse.example.com')).toBeInTheDocument();

        // "Test Connection" button is shown in edit mode
        expect(screen.getByText('Test Connection')).toBeInTheDocument();
    });

    it('validates required fields - submit is disabled when isPending', () => {
        render(<LangfuseEndpointDialog {...defaultProps} isPending={true} />);

        const submitButton = screen.getByRole('button', { name: /saving/i });
        expect(submitButton).toBeDisabled();
    });

    it('calls onSubmit with form data when submitted', () => {
        const onSubmit = vi.fn();
        render(<LangfuseEndpointDialog {...defaultProps} onSubmit={onSubmit} />);

        // Fill in the required fields
        fireEvent.change(screen.getByPlaceholderText(/Production Langfuse/i), {
            target: { value: 'My Endpoint' },
        });
        fireEvent.change(screen.getByPlaceholderText(/langfuse\.example\.com/i), {
            target: { value: 'https://my.langfuse.com' },
        });

        // Fill in key fields (password inputs, identified by placeholder)
        fireEvent.change(screen.getByPlaceholderText('pk-lf-...'), {
            target: { value: 'pk-lf-test' },
        });
        fireEvent.change(screen.getByPlaceholderText('sk-lf-...'), {
            target: { value: 'sk-lf-test' },
        });

        const form = screen.getByRole('button', { name: /create/i }).closest('form')!;
        fireEvent.submit(form);

        expect(onSubmit).toHaveBeenCalledWith({
            name: 'My Endpoint',
            host: 'https://my.langfuse.com',
            public_key: 'pk-lf-test',
            secret_key: 'sk-lf-test',
        } satisfies LangfuseEndpointRequest);
    });

    it('test connection button calls the test mutation', () => {
        const testMutate = vi.fn();
        mockUseTestLangfuseEndpoint.mockReturnValue({
            mutate: testMutate,
            isPending: false,
        } as unknown as ReturnType<typeof useTestLangfuseEndpoint>);

        render(<LangfuseEndpointDialog {...defaultProps} endpoint={sampleEndpoint} />);

        const testButton = screen.getByText('Test Connection');
        fireEvent.click(testButton);

        expect(testMutate).toHaveBeenCalledWith('ep-1', expect.any(Object));
    });
});
