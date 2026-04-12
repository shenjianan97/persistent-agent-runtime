import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FileAttachment, validateFiles } from './FileAttachment';

afterEach(() => {
    cleanup();
});

describe('validateFiles', () => {
    it('returns null for empty file list', () => {
        expect(validateFiles([])).toBeNull();
    });

    it('returns null for files within limits', () => {
        const files = [
            new File(['x'.repeat(1000)], 'small.txt'),
        ];
        expect(validateFiles(files)).toBeNull();
    });

    it('returns error for file exceeding 50 MB', () => {
        const largeContent = new Uint8Array(50 * 1024 * 1024 + 1);
        const files = [new File([largeContent], 'large.bin')];
        const error = validateFiles(files);
        expect(error).not.toBeNull();
        expect(error).toContain('50 MB');
    });

    it('returns error for total size exceeding 200 MB', () => {
        const files = [
            new File([new Uint8Array(49 * 1024 * 1024)], 'file1.bin'),
            new File([new Uint8Array(49 * 1024 * 1024)], 'file2.bin'),
            new File([new Uint8Array(49 * 1024 * 1024)], 'file3.bin'),
            new File([new Uint8Array(49 * 1024 * 1024)], 'file4.bin'),
            new File([new Uint8Array(49 * 1024 * 1024)], 'file5.bin'),
        ];
        const error = validateFiles(files);
        expect(error).not.toBeNull();
        expect(error).toContain('200 MB');
    });

    it('returns null for exactly 50 MB file', () => {
        const files = [
            new File([new Uint8Array(50 * 1024 * 1024)], 'exact.bin'),
        ];
        expect(validateFiles(files)).toBeNull();
    });
});

describe('FileAttachment', () => {
    it('opens the file picker when the browse surface is clicked', () => {
        const handleFilesChange = vi.fn();

        render(<FileAttachment files={[]} onFilesChange={handleFilesChange} />);

        const browseButton = screen.getByRole('button', { name: /drop files here or click to browse/i });
        const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
        const clickSpy = vi.spyOn(fileInput, 'click');

        fireEvent.click(browseButton);

        expect(clickSpy).toHaveBeenCalledTimes(1);
    });

    it('does not open the file picker when disabled', () => {
        const handleFilesChange = vi.fn();

        render(
            <FileAttachment
                files={[]}
                onFilesChange={handleFilesChange}
                disabled
                disabledReason="File upload requires sandbox"
            />
        );

        const browseButton = screen.getByRole('button', { name: /file upload not available/i });
        const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
        const clickSpy = vi.spyOn(fileInput, 'click');

        fireEvent.click(browseButton);

        expect(clickSpy).not.toHaveBeenCalled();
    });

    it('adds files from drag and drop', () => {
        const handleFilesChange = vi.fn();
        const droppedFile = new File(['hello'], 'notes.txt', { type: 'text/plain' });

        render(<FileAttachment files={[]} onFilesChange={handleFilesChange} />);

        const dropZone = screen.getByRole('button', { name: /drop files here or click to browse/i });

        fireEvent.drop(dropZone, {
            dataTransfer: {
                files: [droppedFile],
                items: [],
                types: ['Files'],
            },
        });

        expect(handleFilesChange).toHaveBeenCalledWith([droppedFile]);
    });
});
