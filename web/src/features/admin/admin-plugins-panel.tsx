import {
  useState,
  useCallback,
  useEffect,
  type FormEvent,
} from "react";
import {
  setPluginEnabled,
  removePlugin,
  importPluginFromUrl,
  seedBuiltins,
  type InstalledPlugin,
} from "@/plugins/registry-store";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Download,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
  ToggleLeft,
  ToggleRight,
  Puzzle,
  ExternalLink,
} from "lucide-react";
import { toast } from "sonner";

// ---------------------------------------------------------------------------
// Import Plugin Dialog
// ---------------------------------------------------------------------------

type ImportPluginDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onImported: () => void;
};

function ImportPluginDialog({ open, onOpenChange, onImported }: ImportPluginDialogProps) {
  const [url, setUrl] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleClose = useCallback(() => {
    setUrl("");
    setError(null);
    onOpenChange(false);
  }, [onOpenChange]);

  const handleSubmit = useCallback(
    async (e?: FormEvent) => {
      e?.preventDefault();
      const trimmed = url.trim();
      if (!trimmed) {
        setError("Plugin URL is required.");
        return;
      }
      if (!(trimmed.startsWith("http://") || trimmed.startsWith("https://"))) {
        setError("URL must start with http:// or https://");
        return;
      }
      setError(null);
      setIsLoading(true);
      try {
        const { plugin } = await importPluginFromUrl(trimmed);
        toast.success(`Plugin "${plugin.name}" imported successfully`);
        onImported();
        handleClose();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to import plugin.");
      } finally {
        setIsLoading(false);
      }
    },
    [url, onImported, handleClose],
  );

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Import Plugin</DialogTitle>
          <DialogDescription>
            Enter the URL of an ES module that exports a{" "}
            <code className="rounded bg-muted px-1 py-0.5 text-xs font-mono">UIPlugin</code>{" "}
            object as its default export. The module is loaded directly in the browser
            at runtime.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4 pt-2">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="plugin-url" className="text-sm font-medium text-foreground">
              Module URL
            </label>
            <Input
              id="plugin-url"
              type="url"
              placeholder="https://example.com/my-plugin.js"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              disabled={isLoading}
              autoFocus
            />
            <p className="text-xs text-muted-foreground">
              The module must export a plugin object with{" "}
              <span className="font-mono">id</span>,{" "}
              <span className="font-mono">name</span>,{" "}
              <span className="font-mono">events</span>, and{" "}
              <span className="font-mono">render</span> fields.{" "}
              <a
                href="/docs/plugin-development-guide.md"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-0.5 text-primary hover:underline"
              >
                Read the guide
                <ExternalLink className="size-3" />
              </a>
            </p>
          </div>

          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}

          <DialogFooter>
            <Button type="button" variant="outline" onClick={handleClose} disabled={isLoading}>
              Cancel
            </Button>
            <Button type="submit" disabled={isLoading} className="gap-2">
              {isLoading ? (
                <><Loader2 className="animate-spin size-4" />Importing...</>
              ) : (
                <><Download className="size-4" />Import</>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

export function AdminPluginsPanel() {
  const [plugins, setPlugins] = useState<InstalledPlugin[]>([]);
  const [showImportDialog, setShowImportDialog] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<InstalledPlugin | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [togglingId, setTogglingId] = useState<string | null>(null);

  const loadPlugins = useCallback(() => {
    const seeded = seedBuiltins();
    setPlugins(seeded);
  }, []);

  useEffect(() => {
    loadPlugins();
  }, [loadPlugins]);

  const handleToggle = useCallback((plugin: InstalledPlugin) => {
    setTogglingId(plugin.id);
    try {
      const updated = setPluginEnabled(plugin.id, !plugin.enabled);
      setPlugins(updated);
      toast.success(
        !plugin.enabled
          ? `"${plugin.name}" enabled`
          : `"${plugin.name}" disabled`,
      );
    } catch {
      toast.error("Failed to update plugin.");
    } finally {
      setTogglingId(null);
    }
  }, []);

  const handleDeleteConfirm = useCallback(async () => {
    if (!deleteTarget) return;
    setIsDeleting(true);
    try {
      const updated = removePlugin(deleteTarget.id);
      setPlugins(updated);
      toast.success(`"${deleteTarget.name}" removed`);
      setDeleteTarget(null);
    } catch {
      toast.error("Failed to remove plugin.");
    } finally {
      setIsDeleting(false);
    }
  }, [deleteTarget]);

  const totalPlugins = plugins.length;
  const enabledPlugins = plugins.filter((p) => p.enabled).length;

  function formatDate(ms: number): string {
    return new Date(ms).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Toolbar */}
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={loadPlugins}
          className="gap-2"
        >
          <RefreshCw className="size-4" />
          Refresh
        </Button>
        <Button
          size="sm"
          onClick={() => setShowImportDialog(true)}
          className="gap-2"
        >
          <Plus className="size-4" />
          Import Plugin
        </Button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader className="pb-1">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Total Plugins
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-3xl font-bold">{totalPlugins}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Enabled
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-3xl font-bold text-green-600 dark:text-green-400">
              {enabledPlugins}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1">
            <CardTitle className="text-sm font-medium text-muted-foreground">
              Disabled
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-3xl font-bold text-muted-foreground">
              {totalPlugins - enabledPlugins}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Plugin table */}
      <div className="rounded-xl border bg-card shadow-sm overflow-hidden">
        {plugins.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 gap-3 text-muted-foreground">
            <Puzzle className="size-8 opacity-30" />
            <p className="text-sm">No plugins installed.</p>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowImportDialog(true)}
              className="gap-2"
            >
              <Plus className="size-4" />
              Import your first plugin
            </Button>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/40 text-left text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  <th className="px-4 py-3">Plugin</th>
                  <th className="px-4 py-3">Version</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Events</th>
                  <th className="px-4 py-3">Source</th>
                  <th className="px-4 py-3">Installed</th>
                  <th className="px-4 py-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {plugins.map((plugin) => {
                  const isToggling = togglingId === plugin.id;
                  const isBuiltin = plugin.source === "builtin";

                  return (
                    <tr
                      key={plugin.id}
                      className="transition-colors hover:bg-muted/30"
                    >
                      <td className="px-4 py-3">
                        <div className="flex flex-col gap-0.5">
                          <span className="font-medium">{plugin.name}</span>
                          {plugin.description && (
                            <span className="text-xs text-muted-foreground line-clamp-1">
                              {plugin.description}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-muted-foreground font-mono text-xs">
                        {plugin.version}
                      </td>
                      <td className="px-4 py-3">
                        {plugin.enabled ? (
                          <Badge
                            variant="outline"
                            className="border-green-500/40 text-green-600 dark:text-green-400 bg-green-500/10"
                          >
                            Enabled
                          </Badge>
                        ) : (
                          <Badge variant="outline" className="text-muted-foreground">
                            Disabled
                          </Badge>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex flex-wrap gap-1">
                          {plugin.events.map((ev) => (
                            <Badge
                              key={ev}
                              variant="secondary"
                              className="font-mono text-xs px-1.5 py-0"
                            >
                              {ev}
                            </Badge>
                          ))}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        {isBuiltin ? (
                          <Badge variant="secondary">builtin</Badge>
                        ) : plugin.importUrl ? (
                          <a
                            href={plugin.importUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                          >
                            url
                            <ExternalLink className="size-3" />
                          </a>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-muted-foreground text-xs">
                        {formatDate(plugin.installedAt)}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center justify-end gap-1">
                          {/* Toggle enable/disable */}
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            title={plugin.enabled ? "Disable plugin" : "Enable plugin"}
                            disabled={isToggling}
                            onClick={() => handleToggle(plugin)}
                          >
                            {isToggling ? (
                              <Loader2 className="size-3.5 animate-spin" />
                            ) : plugin.enabled ? (
                              <ToggleRight className="size-3.5 text-green-600 dark:text-green-400" />
                            ) : (
                              <ToggleLeft className="size-3.5 text-muted-foreground" />
                            )}
                          </Button>

                          {/* Delete (disabled for builtins) */}
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            title={isBuiltin ? "Built-in plugins cannot be removed" : "Remove plugin"}
                            disabled={isBuiltin}
                            className="text-muted-foreground hover:text-destructive disabled:opacity-30"
                            onClick={() => setDeleteTarget(plugin)}
                          >
                            <Trash2 className="size-3.5" />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Import dialog */}
      <ImportPluginDialog
        open={showImportDialog}
        onOpenChange={setShowImportDialog}
        onImported={loadPlugins}
      />

      {/* Delete confirm */}
      <AlertDialog
        open={!!deleteTarget}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remove Plugin</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to remove{" "}
              <strong>{deleteTarget?.name}</strong>? The plugin will stop
              working immediately. You can re-import it later using the same URL.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-white hover:bg-destructive/90"
              disabled={isDeleting}
              onClick={handleDeleteConfirm}
            >
              {isDeleting ? (
                <><Loader2 className="animate-spin size-4" />Removing...</>
              ) : (
                "Remove"
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
