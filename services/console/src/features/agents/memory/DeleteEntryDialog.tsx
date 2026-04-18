import { Button } from '@/components/ui/button';
import {
    Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';

interface DeleteEntryDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    entryTitle: string;
    isPending: boolean;
    onConfirm: () => void;
}

/**
 * Confirmation modal for hard-deleting a memory entry.
 *
 * The task spec explicitly requires: entry title in the copy, a
 * "cannot be undone" notice, and a single-click confirm action.
 */
export function DeleteEntryDialog({
    open,
    onOpenChange,
    entryTitle,
    isPending,
    onConfirm,
}: DeleteEntryDialogProps) {
    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="sm:max-w-[440px] console-surface border-white/10 rounded-2xl">
                <DialogHeader>
                    <DialogTitle className="text-lg font-display uppercase tracking-widest text-destructive">
                        Delete Memory Entry
                    </DialogTitle>
                </DialogHeader>
                <p className="text-sm text-muted-foreground">
                    Are you sure you want to delete{' '}
                    <span className="font-mono text-foreground">{entryTitle || '(untitled entry)'}</span>?
                </p>
                <p className="text-xs text-amber-400">This action cannot be undone.</p>
                <DialogFooter>
                    <Button
                        type="button"
                        variant="ghost"
                        onClick={() => onOpenChange(false)}
                        className="uppercase tracking-widest text-xs"
                    >
                        Cancel
                    </Button>
                    <Button
                        type="button"
                        onClick={onConfirm}
                        disabled={isPending}
                        className="font-bold uppercase tracking-widest px-6 bg-destructive hover:bg-destructive/90 text-destructive-foreground"
                    >
                        {isPending ? 'Deleting...' : 'Delete'}
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}
