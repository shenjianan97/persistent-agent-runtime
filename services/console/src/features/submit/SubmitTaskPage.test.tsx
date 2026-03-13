import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SubmitTaskPage } from './SubmitTaskPage';

const navigateMock = vi.fn();

vi.mock('react-router', async () => {
    const actual = await vi.importActual<typeof import('react-router')>('react-router');
    return {
        ...actual,
        useNavigate: () => navigateMock,
    };
});

vi.mock('./useSubmitTask', () => ({
    useSubmitTask: () => ({
        mutate: vi.fn(),
        isPending: false,
    }),
}));

vi.mock('./useModels', () => ({
    useModels: () => ({
        data: [
            { provider: 'openai', model_id: 'gpt-4o', display_name: 'GPT-4o' },
            { provider: 'anthropic', model_id: 'claude-3-5-sonnet-latest', display_name: 'Claude 3.5 Sonnet' },
            { provider: 'openai', model_id: 'gpt-4o-mini', display_name: 'GPT-4o Mini' },
            { provider: 'anthropic', model_id: 'claude-3-5-haiku-latest', display_name: 'Claude 3.5 Haiku' },
        ],
        isLoading: false,
    }),
}));

vi.mock('sonner', () => ({
    toast: {
        success: vi.fn(),
        error: vi.fn(),
    },
}));

afterEach(() => {
    cleanup();
    navigateMock.mockReset();
});

describe('SubmitTaskPage', () => {
    it('renders model choices sectioned by provider', () => {
        const { container } = render(
            <MemoryRouter>
                <SubmitTaskPage />
            </MemoryRouter>,
        );

        expect(screen.getByRole('heading', { name: 'Submit Task' })).toBeInTheDocument();

        const modelSelect = screen.getByLabelText('Model');
        expect(modelSelect).toBeInTheDocument();

        const optgroups = Array.from(container.querySelectorAll('optgroup'));
        expect(optgroups).toHaveLength(2);
        expect(optgroups.map((group) => group.getAttribute('label'))).toEqual(['OpenAI', 'Anthropic']);

        const openAiOptions = Array.from(optgroups[0].querySelectorAll('option')).map((option) => option.textContent);
        const anthropicOptions = Array.from(optgroups[1].querySelectorAll('option')).map((option) => option.textContent);

        expect(openAiOptions).toEqual(['GPT-4o', 'GPT-4o Mini']);
        expect(anthropicOptions).toEqual(['Claude 3.5 Sonnet', 'Claude 3.5 Haiku']);
    });
});
