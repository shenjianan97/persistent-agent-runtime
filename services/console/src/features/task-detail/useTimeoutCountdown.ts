import { useState, useEffect } from 'react';

function formatRemaining(ms: number): string {
    if (ms <= 0) return 'expired';
    const totalSeconds = Math.ceil(ms / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    if (minutes > 0) {
        return `${minutes}m ${seconds}s`;
    }
    return `${seconds}s`;
}

export function useTimeoutCountdown(timeoutAt?: string): string | null {
    const [remaining, setRemaining] = useState<string | null>(null);

    useEffect(() => {
        if (!timeoutAt) {
            setRemaining(null);
            return;
        }

        const update = () => {
            const diff = new Date(timeoutAt).getTime() - Date.now();
            setRemaining(formatRemaining(diff));
        };

        update();
        const interval = setInterval(update, 1000);
        return () => clearInterval(interval);
    }, [timeoutAt]);

    return remaining;
}
