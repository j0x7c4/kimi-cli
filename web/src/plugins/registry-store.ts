/**
 * Plugin registry store — persists installed plugin metadata in localStorage.
 *
 * The store only tracks metadata (id, name, enabled state, source URL, etc.).
 * Actual plugin code is loaded at runtime via dynamic import from the stored URL
 * or by reference for builtins. The PluginSystemProvider reads this store on
 * mount to determine which plugins to activate.
 */

const STORAGE_KEY = "kimi_ui_plugins_v1";

export type PluginSource = "builtin" | "url";

export interface InstalledPlugin {
  id: string;
  name: string;
  description: string;
  version: string;
  author?: string;
  /** Events this plugin handles (e.g. "thinking:start", "subagent:cluster") */
  events: string[];
  enabled: boolean;
  source: PluginSource;
  /** For source="url": the ES module URL that exports a default UIPlugin */
  importUrl?: string;
  installedAt: number; // Unix ms
}

// ---------------------------------------------------------------------------
// Read
// ---------------------------------------------------------------------------

export function getInstalledPlugins(): InstalledPlugin[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as InstalledPlugin[]) : [];
  } catch {
    return [];
  }
}

export function getPlugin(id: string): InstalledPlugin | undefined {
  return getInstalledPlugins().find((p) => p.id === id);
}

// ---------------------------------------------------------------------------
// Write helpers
// ---------------------------------------------------------------------------

function saveAll(plugins: InstalledPlugin[]): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(plugins));
}

export function addPlugin(plugin: InstalledPlugin): InstalledPlugin[] {
  const existing = getInstalledPlugins().filter((p) => p.id !== plugin.id);
  const updated = [...existing, plugin];
  saveAll(updated);
  return updated;
}

export function setPluginEnabled(id: string, enabled: boolean): InstalledPlugin[] {
  const updated = getInstalledPlugins().map((p) =>
    p.id === id ? { ...p, enabled } : p,
  );
  saveAll(updated);
  return updated;
}

export function removePlugin(id: string): InstalledPlugin[] {
  const updated = getInstalledPlugins().filter((p) => p.id !== id);
  saveAll(updated);
  return updated;
}

// ---------------------------------------------------------------------------
// Seed builtins — call on app startup to ensure builtins are present.
// If the user already has a preference (enabled=false) it is preserved.
// ---------------------------------------------------------------------------

export interface BuiltinPluginDef {
  id: string;
  name: string;
  description: string;
  version: string;
  author: string;
  events: string[];
}

const BUILTINS: BuiltinPluginDef[] = [
  {
    id: "builtin:thinking-animation",
    name: "Thinking Animation",
    description: "Shows an animated overlay badge while the agent is thinking.",
    version: "1.0.0",
    author: "kimi-cli",
    events: ["thinking:start", "thinking:end"],
  },
  {
    id: "builtin:subagent-cluster",
    name: "Subagent Cluster Visualization",
    description:
      "Displays an animated panel when multiple sub-agents are launched concurrently.",
    version: "1.0.0",
    author: "kimi-cli",
    events: ["subagent:cluster"],
  },
];

export function seedBuiltins(): InstalledPlugin[] {
  const current = getInstalledPlugins();
  const currentIds = new Set(current.map((p) => p.id));

  const toAdd: InstalledPlugin[] = BUILTINS.filter(
    (b) => !currentIds.has(b.id),
  ).map((b) => ({
    ...b,
    source: "builtin" as PluginSource,
    enabled: true,
    installedAt: Date.now(),
  }));

  if (toAdd.length === 0) return current;

  const updated = [...current, ...toAdd];
  saveAll(updated);
  return updated;
}

// ---------------------------------------------------------------------------
// Import from URL
// ---------------------------------------------------------------------------

export interface ImportResult {
  plugin: InstalledPlugin;
  warning?: string;
}

/**
 * Attempts to dynamically import a plugin from a URL.
 * The module must export a default object with at minimum: id, name, version, events.
 */
export async function importPluginFromUrl(url: string): Promise<ImportResult> {
  let mod: unknown;
  try {
    mod = await import(/* @vite-ignore */ url);
  } catch (err) {
    throw new Error(
      `Failed to load module from "${url}": ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  // Support both default export and named "plugin" export
  const raw =
    (mod as { default?: unknown }).default ??
    (mod as { plugin?: unknown }).plugin;

  if (!raw || typeof raw !== "object") {
    throw new Error(
      `Module at "${url}" does not export a plugin object as default or named "plugin" export.`,
    );
  }

  const def = raw as Record<string, unknown>;

  if (typeof def.id !== "string" || !def.id) {
    throw new Error('Plugin is missing required field "id".');
  }
  if (typeof def.name !== "string" || !def.name) {
    throw new Error('Plugin is missing required field "name".');
  }
  if (!Array.isArray(def.events)) {
    throw new Error('Plugin is missing required field "events" (array of event type strings).');
  }

  const plugin: InstalledPlugin = {
    id: def.id as string,
    name: def.name as string,
    description: typeof def.description === "string" ? def.description : "",
    version: typeof def.version === "string" ? def.version : "0.0.0",
    author: typeof def.author === "string" ? def.author : undefined,
    events: (def.events as unknown[])
      .filter((e) => typeof e === "string")
      .map((e) => e as string),
    enabled: true,
    source: "url",
    importUrl: url,
    installedAt: Date.now(),
  };

  const all = addPlugin(plugin);
  const warning =
    all.find((p) => p.id === plugin.id)?.importUrl !== url
      ? undefined
      : undefined;

  return { plugin, warning };
}
