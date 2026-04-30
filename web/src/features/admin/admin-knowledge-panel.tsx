import { useCallback, useEffect, useState } from "react";

import { Loader2, RefreshCw, Save } from "lucide-react";
import { toast } from "sonner";

import {
  type KnowledgeIndex,
  getKnowledgeIndex,
  setKnowledgeIndex,
} from "@/lib/api/apis/AdminApi";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";

const PLACEHOLDER = `# Knowledge Index

Brief table of contents pointing to detailed pages under \`wiki/\`.

- Architecture overview — wiki/architecture.md
- Coding conventions — wiki/conventions.md
`;

export function AdminKnowledgePanel() {
  const [index, setIndex] = useState<KnowledgeIndex | null>(null);
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getKnowledgeIndex();
      setIndex(data);
      setContent(data.content);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load index.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const data = await setKnowledgeIndex(content);
      setIndex(data);
      setContent(data.content);
      toast.success("Knowledge index saved");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to save index.");
    } finally {
      setSaving(false);
    }
  }, [content]);

  const dirty = index !== null && content !== index.content;

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-2">
          <div>
            <CardTitle className="text-base">Shared Knowledge Index</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">
              Only <code>index.md</code> is injected into the system prompt. Use it as a table
              of contents pointing to detailed pages under <code>wiki/</code>; agents read those
              on demand via <code>ReadFile</code>.
            </p>
            {index && (
              <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                {index.path}
                {!index.exists && (
                  <span className="ml-2 italic">(will be created on save)</span>
                )}
              </p>
            )}
          </div>
          <Button
            size="sm"
            variant="outline"
            onClick={refresh}
            disabled={loading || saving}
            className="gap-2"
          >
            <RefreshCw className={loading ? "size-4 animate-spin" : "size-4"} />
            Refresh
          </Button>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {error && (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}
          <Textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            rows={20}
            placeholder={PLACEHOLDER}
            disabled={loading}
            className="font-mono text-sm"
          />
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>{content.length} chars</span>
            <Button
              size="sm"
              onClick={handleSave}
              disabled={!dirty || saving || loading}
              className="gap-2"
            >
              {saving ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Save className="size-4" />
              )}
              Save
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
