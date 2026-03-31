import { cleanup, render, screen, fireEvent } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LangfuseEndpointList } from './LangfuseEndpointList';
import type { LangfuseEndpoint } from '@/types';

// Mock the hooks module
vi.mock('./useLangfuseEndpoints', () => ({
    useLangfuseEndpoints: vi.fn(),
    useCreateLangfuseEndpoint: vi.fn(),
    useUpdateLangfuseEndpoint: vi.fn(),
    useDeleteLangfuseEndpoint: vi.fn(),
    useTestLangfuseEndpoint: vi.fn(),
}));

// Mock the dialog so it doesn't render complex internals
vi.mock('./LangfuseEndpointDialog', () => ({
    LangfuseEndpointDialog: () => null,
}));

// Mock sonner toast
vi.mock('sonner', () => ({
    toast: {
        success: vi.fn(),
        error: vi.fn(),
    },
}));

import {
    useLangfuseEndpoints,
    useCreateLangfuseEndpoint,
    useUpdateLangfuseEndpoint,
    useDeleteLangfuseEndpoint,
    useTestLangfuseEndpoint,
} from './useLangfuseEndpoints';

const mockUseLangfuseEndpoints = vi.mocked(useLangfuseEndpoints);
const mockUseCreateLangfuseEndpoint = vi.mocked(useCreateLangfuseEndpoint);
const mockUseUpdateLangfuseEndpoint = vi.mocked(useUpdateLangfuseEndpoint);
const mockUseDeleteLangfuseEndpoint = vi.mocked(useDeleteLangfuseEndpoint);
const mockUseTestLangfuseEndpoint = vi.mocked(useTestLangfuseEndpoint);

const noopMutation = {
    mutate: vi.fn(),
    isPending: false,
} as unknown as ReturnType<typeof useDeleteLangfuseEndpoint>;

beforeEach(() => {
    mockUseCreateLangfuseEndpoint.mockReturnValue(noopMutation as ReturnType<typeof useCreateLangfuseEndpoint>);
    mockUseUpdateLangfuseEndpoint.mockReturnValue(noopMutation as ReturnType<typeof useUpdateLangfuseEndpoint>);
    mockUseDeleteLangfuseEndpoint.mockReturnValue(noopMutation as ReturnType<typeof useDeleteLangfuseEndpoint>);
    mockUseTestLangfuseEndpoint.mockReturnValue(noopMutation as ReturnType<typeof useTestLangfuseEndpoint>);
});

afterEach(() => {
    cleanup();
    vi.clearAllMocks();
});

const sampleEndpoints: LangfuseEndpoint[] = [
    {
        endpoint_id: 'ep-1',
        tenant_id: 'tenant-1',
        name: 'Production Langfuse',
        host: 'https://langfuse.example.com',
        created_at: '2026-01-15T00:00:00Z',
        updated_at: '2026-01-15T00:00:00Z',
    },
    {
        endpoint_id: 'ep-2',
        tenant_id: 'tenant-1',
        name: 'Staging Langfuse',
        host: 'https://staging.langfuse.example.com',
        created_at: '2026-02-20T00:00:00Z',
        updated_at: '2026-02-20T00:00:00Z',
    },
];

describe('LangfuseEndpointList', () => {
    it('renders empty state when no endpoints', () => {
        mockUseLangfuseEndpoints.mockReturnValue({
            data: [],
            isLoading: false,
        } as unknown as ReturnType<typeof useLangfuseEndpoints>);

        render(<LangfuseEndpointList />);

        expect(screen.getByText('No Langfuse endpoints configured')).toBeInTheDocument();
    });

    it('renders table with endpoints', () => {
        mockUseLangfuseEndpoints.mockReturnValue({
            data: sampleEndpoints,
            isLoading: false,
        } as unknown as ReturnType<typeof useLangfuseEndpoints>);

        render(<LangfuseEndpointList />);

        expect(screen.getByText('Production Langfuse')).toBeInTheDocument();
        expect(screen.getByText('https://langfuse.example.com')).toBeInTheDocument();
        expect(screen.getByText('Staging Langfuse')).toBeInTheDocument();
        expect(screen.getByText('https://staging.langfuse.example.com')).toBeInTheDocument();

        // Created dates
        expect(screen.getByText(new Date('2026-01-15T00:00:00Z').toLocaleDateString())).toBeInTheDocument();
        expect(screen.getByText(new Date('2026-02-20T00:00:00Z').toLocaleDateString())).toBeInTheDocument();
    });

    it('shows add endpoint button', () => {
        mockUseLangfuseEndpoints.mockReturnValue({
            data: [],
            isLoading: false,
        } as unknown as ReturnType<typeof useLangfuseEndpoints>);

        render(<LangfuseEndpointList />);

        expect(screen.getByText('Add Endpoint')).toBeInTheDocument();
    });

    it('calls delete handler when delete button is clicked', () => {
        const deleteMutate = vi.fn();
        mockUseDeleteLangfuseEndpoint.mockReturnValue({
            mutate: deleteMutate,
            isPending: false,
        } as unknown as ReturnType<typeof useDeleteLangfuseEndpoint>);

        mockUseLangfuseEndpoints.mockReturnValue({
            data: sampleEndpoints,
            isLoading: false,
        } as unknown as ReturnType<typeof useLangfuseEndpoints>);

        // Mock window.confirm to return true
        vi.spyOn(window, 'confirm').mockReturnValue(true);

        render(<LangfuseEndpointList />);

        // Find all delete buttons (by title attribute)
        const deleteButtons = screen.getAllByTitle('Delete');
        expect(deleteButtons).toHaveLength(2);

        // Click the first delete button
        fireEvent.click(deleteButtons[0]);

        expect(window.confirm).toHaveBeenCalled();
        expect(deleteMutate).toHaveBeenCalledWith('ep-1', expect.any(Object));
    });
});
