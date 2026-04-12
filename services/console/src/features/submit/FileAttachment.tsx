import { useCallback, useRef, useState } from 'react';
import { X, Upload, FileIcon, AlertCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';

const MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024; // 50 MB
const MAX_TOTAL_SIZE_BYTES = 200 * 1024 * 1024; // 200 MB

interface FileAttachmentProps {
    files: File[];
    onFilesChange: (files: File[]) => void;
    disabled?: boolean;
    disabledReason?: string;
}

function formatFileSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function validateFiles(files: File[]): string | null {
    for (const file of files) {
        if (file.size > MAX_FILE_SIZE_BYTES) {
            return `File "${file.name}" exceeds the 50 MB limit (${formatFileSize(file.size)})`;
        }
    }
    const totalSize = files.reduce((sum, f) => sum + f.size, 0);
    if (totalSize > MAX_TOTAL_SIZE_BYTES) {
        return `Total file size exceeds the 200 MB limit (${formatFileSize(totalSize)})`;
    }
    return null;
}

export function FileAttachment({ files, onFilesChange, disabled = false, disabledReason }: FileAttachmentProps) {
    const inputRef = useRef<HTMLInputElement>(null);
    const [dragOver, setDragOver] = useState(false);
    const [validationError, setValidationError] = useState<string | null>(null);

    const addFiles = useCallback((newFiles: FileList | File[]) => {
        const fileArray = Array.from(newFiles);
        const combined = [...files, ...fileArray];

        const error = validateFiles(combined);
        if (error) {
            setValidationError(error);
            return;
        }
        setValidationError(null);
        onFilesChange(combined);
    }, [files, onFilesChange]);

    const removeFile = useCallback((index: number) => {
        const updated = files.filter((_, i) => i !== index);
        setValidationError(null);
        onFilesChange(updated);
    }, [files, onFilesChange]);

    const handleDrop = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        setDragOver(false);
        if (disabled) return;
        if (e.dataTransfer.files.length > 0) {
            addFiles(e.dataTransfer.files);
        }
    }, [addFiles, disabled]);

    const handleDragOver = useCallback((e: React.DragEvent) => {
        e.preventDefault();
        if (!disabled) setDragOver(true);
    }, [disabled]);

    const handleDragLeave = useCallback(() => {
        setDragOver(false);
    }, []);

    const handleFileInput = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
        if (e.target.files && e.target.files.length > 0) {
            addFiles(e.target.files);
            e.target.value = ''; // Reset input
        }
    }, [addFiles]);

    const handleBrowseClick = useCallback(() => {
        if (disabled) return;
        inputRef.current?.click();
    }, [disabled]);

    const totalSize = files.reduce((sum, f) => sum + f.size, 0);

    return (
        <div className="space-y-3">
            {disabled && disabledReason && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <AlertCircle className="w-3.5 h-3.5" />
                    <span>{disabledReason}</span>
                </div>
            )}

            <div className="relative">
                <button
                    type="button"
                    className={`block w-full border-2 border-dashed rounded-lg p-6 text-center transition-colors ${
                    disabled
                        ? 'border-border/30 bg-muted/5 cursor-not-allowed opacity-50'
                        : dragOver
                        ? 'border-primary bg-primary/5'
                        : 'border-border hover:border-primary/50 cursor-pointer'
                    }`}
                    onClick={handleBrowseClick}
                    onDrop={handleDrop}
                    onDragOver={handleDragOver}
                    onDragLeave={handleDragLeave}
                    disabled={disabled}
                >
                    <Upload className="w-8 h-8 mx-auto mb-2 text-muted-foreground" />
                    <span className="block text-sm text-muted-foreground">
                        {disabled
                            ? 'File upload not available'
                            : 'Drop files here or click to browse'}
                    </span>
                    <span className="mt-1 block text-xs text-muted-foreground/60">
                        Max 50 MB per file, 200 MB total
                    </span>
                </button>
                <input
                    ref={inputRef}
                    type="file"
                    multiple
                    className="absolute left-0 top-0 h-0 w-0 overflow-hidden opacity-0 pointer-events-none"
                    onChange={handleFileInput}
                    disabled={disabled}
                    tabIndex={-1}
                    aria-hidden="true"
                />
            </div>

            {validationError && (
                <div className="flex items-center gap-2 text-xs text-destructive">
                    <AlertCircle className="w-3.5 h-3.5 shrink-0" />
                    <span>{validationError}</span>
                </div>
            )}

            {files.length > 0 && (
                <div className="space-y-2">
                    {files.map((file, index) => (
                        <div
                            key={`${file.name}-${index}`}
                            className="flex items-center gap-3 p-2 rounded bg-muted/10 border border-white/5"
                        >
                            <FileIcon className="w-4 h-4 text-muted-foreground shrink-0" />
                            <div className="flex-1 min-w-0">
                                <p className="text-sm font-mono truncate">{file.name}</p>
                                <p className="text-xs text-muted-foreground">
                                    {formatFileSize(file.size)}
                                </p>
                            </div>
                            <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                className="h-6 w-6 p-0 hover:bg-destructive/20 hover:text-destructive"
                                onClick={(e) => {
                                    e.stopPropagation();
                                    removeFile(index);
                                }}
                            >
                                <X className="w-3.5 h-3.5" />
                            </Button>
                        </div>
                    ))}
                    <p className="text-xs text-muted-foreground">
                        {files.length} file{files.length !== 1 ? 's' : ''} ({formatFileSize(totalSize)})
                    </p>
                </div>
            )}
        </div>
    );
}
