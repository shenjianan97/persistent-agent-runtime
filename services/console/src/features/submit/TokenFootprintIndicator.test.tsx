import { render, screen, cleanup } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import {
    TokenFootprintIndicator,
    computeAttachmentBytes,
    type TokenFootprintEntry,
} from './TokenFootprintIndicator';

afterEach(cleanup);

describe('computeAttachmentBytes', () => {
    it('returns 0 for empty selection', () => {
        expect(computeAttachmentBytes([])).toBe(0);
    });

    it('sums title + summary + observations + 50 per entry', () => {
        const entries: TokenFootprintEntry[] = [
            {
                memory_id: 'a',
                title: 'abc', // 3
                summary: 'de', // 2
                observations: ['x', 'yz'], // 3
            },
        ];
        // 3 + 2 + 3 + 50 = 58
        expect(computeAttachmentBytes(entries)).toBe(58);
    });

    it('handles missing fields as zero-length', () => {
        const entries: TokenFootprintEntry[] = [
            { memory_id: 'a', title: 'hi' }, // 2 + 0 + 0 + 50
            { memory_id: 'b' }, // 0 + 0 + 0 + 50
        ];
        expect(computeAttachmentBytes(entries)).toBe(102);
    });

    it('aggregates across multiple entries', () => {
        const entries: TokenFootprintEntry[] = [
            { memory_id: 'a', title: 'x'.repeat(100), summary: 'y'.repeat(500) },
            { memory_id: 'b', title: 'x'.repeat(100), summary: 'y'.repeat(500) },
        ];
        // 600 + 50 = 650 per entry, 1300 total
        expect(computeAttachmentBytes(entries)).toBe(1300);
    });
});

describe('TokenFootprintIndicator', () => {
    it('renders nothing when no entries are selected', () => {
        const { container } = render(
            <TokenFootprintIndicator entries={[]} selectionCount={0} />
        );
        expect(container.firstChild).toBeNull();
    });

    it('renders count + approximate size for a small selection', () => {
        const entries: TokenFootprintEntry[] = [
            { memory_id: 'a', title: 'Note', summary: 'short' }, // 4 + 5 + 50 = 59 B
        ];
        render(<TokenFootprintIndicator entries={entries} selectionCount={1} />);
        const el = screen.getByTestId('token-footprint-indicator');
        expect(el).toHaveTextContent(/Attached context:/i);
        expect(el).toHaveTextContent(/1 entry/);
        expect(el.getAttribute('data-large')).toBe('false');
    });

    it('turns amber at the 10 KB threshold', () => {
        const bigSummary = 'x'.repeat(10 * 1024); // 10 KB alone => total > 10 KB
        const entries: TokenFootprintEntry[] = [
            { memory_id: 'a', title: 'Large', summary: bigSummary },
        ];
        render(<TokenFootprintIndicator entries={entries} selectionCount={1} />);
        const el = screen.getByTestId('token-footprint-indicator');
        expect(el.getAttribute('data-large')).toBe('true');
        expect(el).toHaveTextContent(/1 entry/);
        // Exceeding bytes should include 'KB' label.
        expect(el).toHaveTextContent(/KB/);
    });

    it('pluralizes entry / entries correctly', () => {
        const entries: TokenFootprintEntry[] = [
            { memory_id: 'a' },
            { memory_id: 'b' },
        ];
        render(<TokenFootprintIndicator entries={entries} selectionCount={2} />);
        const el = screen.getByTestId('token-footprint-indicator');
        expect(el).toHaveTextContent(/2 entries/);
    });

    it('counts loading entries toward selectionCount but not bytes', () => {
        // Only one entry has resolved detail; selectionCount reflects two total.
        const entries: TokenFootprintEntry[] = [
            { memory_id: 'a', title: 'done', summary: 'x' },
        ];
        render(<TokenFootprintIndicator entries={entries} selectionCount={2} />);
        const el = screen.getByTestId('token-footprint-indicator');
        expect(el).toHaveTextContent(/2 entries/);
    });
});
