import { Download, FileText } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { api } from '@/api/client';
import { ArtifactMetadata } from '@/types';

function formatFileSize(bytes: number): string {
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    const k = 1024;
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    const size = bytes / Math.pow(k, i);
    return `${size.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

interface ArtifactsTabProps {
    taskId: string;
    artifacts: ArtifactMetadata[];
}

export function ArtifactsTab({ taskId, artifacts }: ArtifactsTabProps) {
    if (artifacts.length === 0) {
        return null;
    }

    const handleDownload = (filename: string, direction: string) => {
        const url = api.getArtifactDownloadUrl(taskId, filename, direction);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    };

    return (
        <Card className="console-surface border-white/10">
            <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2 border-b border-white/8">
                <CardTitle className="text-sm font-display uppercase tracking-widest flex items-center gap-2 text-muted-foreground">
                    <FileText className="w-4 h-4" /> Artifacts ({artifacts.length})
                </CardTitle>
            </CardHeader>
            <CardContent className="pt-4">
                <div className="space-y-2">
                    {artifacts.map((artifact) => (
                        <div
                            key={artifact.artifactId}
                            className="flex items-center justify-between gap-4 px-4 py-3 rounded-lg bg-black/20 border border-white/5 hover:border-white/10 transition-colors"
                        >
                            <div className="flex items-center gap-3 min-w-0">
                                <FileText className="w-4 h-4 text-muted-foreground shrink-0" />
                                <div className="min-w-0">
                                    <p className="text-sm font-mono text-foreground truncate">
                                        {artifact.filename}
                                    </p>
                                    <div className="flex gap-3 text-xs text-muted-foreground font-mono uppercase tracking-wider">
                                        <span>{artifact.direction}</span>
                                        <span>{artifact.contentType}</span>
                                        <span>{formatFileSize(artifact.sizeBytes)}</span>
                                        <span>{new Date(artifact.createdAt).toLocaleString()}</span>
                                    </div>
                                </div>
                            </div>
                            <Button
                                variant="ghost"
                                size="sm"
                                className="shrink-0 uppercase tracking-widest text-xs"
                                onClick={() => handleDownload(artifact.filename, artifact.direction)}
                            >
                                <Download className="w-4 h-4 mr-1" /> Download
                            </Button>
                        </div>
                    ))}
                </div>
            </CardContent>
        </Card>
    );
}
