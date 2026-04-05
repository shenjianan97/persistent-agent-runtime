import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SettingsPage } from './SettingsPage';

vi.mock('./LangfuseEndpointList', () => ({
    LangfuseEndpointList: () => <div data-testid="langfuse-endpoint-list">Endpoint List</div>,
}));

afterEach(() => {
    cleanup();
});

describe('SettingsPage', () => {
    it('renders settings page with heading', () => {
        render(<SettingsPage />);

        expect(screen.getByText('Settings')).toBeInTheDocument();
        expect(screen.getByText(/manage integrations and platform configuration/i)).toBeInTheDocument();
    });

    it('renders endpoint list component', () => {
        render(<SettingsPage />);

        expect(screen.getByTestId('langfuse-endpoint-list')).toBeInTheDocument();
    });
});
