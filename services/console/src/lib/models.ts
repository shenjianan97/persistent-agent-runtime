import type { ModelResponse } from '@/types';

export const PROVIDER_LABELS: Record<string, string> = {
    anthropic: 'Anthropic',
    openai: 'OpenAI',
};

export function formatProviderLabel(provider: string) {
    return PROVIDER_LABELS[provider] ?? (provider.charAt(0).toUpperCase() + provider.slice(1));
}

export function groupModelsByProvider(models: ModelResponse[]) {
    const groups = new Map<string, { provider: string; label: string; models: ModelResponse[] }>();
    models.forEach((model) => {
        const existing = groups.get(model.provider);
        if (existing) {
            existing.models.push(model);
        } else {
            groups.set(model.provider, {
                provider: model.provider,
                label: formatProviderLabel(model.provider),
                models: [model],
            });
        }
    });
    return Array.from(groups.values());
}
