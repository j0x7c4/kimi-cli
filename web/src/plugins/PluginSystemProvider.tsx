/**
 * PluginSystemProvider — event bus, React context, and Portal-based overlay host.
 *
 * Wrap <App /> with this provider in bootstrap.tsx. It:
 * 1. Maintains a PluginEventBus (pub/sub for PluginEvents)
 * 2. Maintains a PluginRegistry (register/unregister UIPlugin instances)
 * 3. Renders active plugin overlays via a React Portal at document.body
 *
 * Overlay model: one slot per plugin. New events update the slot in place.
 * When a plugin calls dismiss() or its render returns null, the slot is removed.
 */
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  useCallback,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import type {
  PluginEvent,
  PluginEventType,
  UIPlugin,
  PluginRegistry,
} from "./types";

// ---------------------------------------------------------------------------
// PluginEventBus
// ---------------------------------------------------------------------------

type Listener = (event: PluginEvent) => void;

export class PluginEventBus {
  private listeners = new Map<PluginEventType, Set<Listener>>();

  on(eventType: PluginEventType, fn: Listener): () => void {
    if (!this.listeners.has(eventType)) {
      this.listeners.set(eventType, new Set());
    }
    this.listeners.get(eventType)!.add(fn);
    return () => this.listeners.get(eventType)?.delete(fn);
  }

  emit(event: PluginEvent): void {
    const fns = this.listeners.get(event.type);
    if (fns) {
      for (const fn of fns) {
        try {
          fn(event);
        } catch (err) {
          console.error(`[PluginBus] listener error for ${event.type}:`, err);
        }
      }
    }
  }

  clear(): void {
    this.listeners.clear();
  }
}

// ---------------------------------------------------------------------------
// Overlay slot tracking — one slot per plugin, updated in place
// ---------------------------------------------------------------------------

type OverlaySlot = {
  pluginId: string;
  plugin: UIPlugin;
  event: PluginEvent;
};

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

type PluginSystemContextValue = {
  bus: PluginEventBus;
  registry: PluginRegistry;
};

const PluginSystemContext = createContext<PluginSystemContextValue | null>(null);

export function usePluginSystem(): PluginSystemContextValue {
  const ctx = useContext(PluginSystemContext);
  if (!ctx) {
    throw new Error("usePluginSystem must be used within <PluginSystemProvider>");
  }
  return ctx;
}

export function usePluginBus(): PluginEventBus {
  return usePluginSystem().bus;
}

export function usePluginRegistry(): PluginRegistry {
  return usePluginSystem().registry;
}

// ---------------------------------------------------------------------------
// Provider + Portal host
// ---------------------------------------------------------------------------

export function PluginSystemProvider({ children }: { children: ReactNode }) {
  const busRef = useRef(new PluginEventBus());
  const pluginsRef = useRef<UIPlugin[]>([]);
  const [slots, setSlots] = useState<OverlaySlot[]>([]);

  const dismissPlugin = useCallback((pluginId: string) => {
    const t1 = autoDismissTimers.current.get(pluginId);
    if (t1) { clearTimeout(t1); autoDismissTimers.current.delete(pluginId); }
    const t2 = hardLimitTimers.current.get(pluginId);
    if (t2) { clearTimeout(t2); hardLimitTimers.current.delete(pluginId); }
    setSlots((prev) => prev.filter((s) => s.pluginId !== pluginId));
  }, []);

  // Registry implementation
  const registry = useRef<PluginRegistry>({
    register(plugin: UIPlugin) {
      pluginsRef.current = [...pluginsRef.current.filter((p) => p.id !== plugin.id), plugin];
      return () => {
        pluginsRef.current = pluginsRef.current.filter((p) => p.id !== plugin.id);
      };
    },
    unregister(pluginId: string) {
      pluginsRef.current = pluginsRef.current.filter((p) => p.id !== pluginId);
    },
    getPlugins() {
      return pluginsRef.current;
    },
  }).current;

  // Auto-dismiss timers keyed by pluginId (plugin-level + system hard limit)
  const autoDismissTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  const hardLimitTimers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  const SYSTEM_MAX_DISPLAY_MS = 10_000;

  // Connect bus → overlay rendering (upsert per plugin)
  useEffect(() => {
    const bus = busRef.current;
    const timers = autoDismissTimers.current;
    const hardTimers = hardLimitTimers.current;
    const allEventTypes: PluginEventType[] = [
      "thinking:start",
      "thinking:chunk",
      "thinking:end",
      "subagent:start",
      "subagent:stop",
      "subagent:cluster",
      "turn:begin",
      "turn:end",
    ];

    const unsubs = allEventTypes.map((eventType) =>
      bus.on(eventType, (event) => {
        const matchingPlugins = pluginsRef.current.filter((p) =>
          p.events.includes(event.type),
        );
        for (const plugin of matchingPlugins) {
          setSlots((prev) => {
            const existing = prev.find((s) => s.pluginId === plugin.id);
            if (existing) {
              return prev.map((s) =>
                s.pluginId === plugin.id ? { ...s, event } : s,
              );
            }
            return [...prev, { pluginId: plugin.id, plugin, event }];
          });

          const dismissSlot = (pid: string) => {
            timers.delete(pid);
            hardTimers.delete(pid);
            setSlots((s) => s.filter((slot) => slot.pluginId !== pid));
          };

          // Plugin-level auto-dismiss: resets on every new event
          const autoMs = plugin.overlayConfig?.autoDismissMs;
          if (autoMs && autoMs > 0) {
            const prev = timers.get(plugin.id);
            if (prev) clearTimeout(prev);
            timers.set(
              plugin.id,
              setTimeout(() => dismissSlot(plugin.id), Math.min(autoMs, SYSTEM_MAX_DISPLAY_MS)),
            );
          }

          // System hard limit: starts once per slot creation, never resets
          if (!hardTimers.has(plugin.id)) {
            hardTimers.set(
              plugin.id,
              setTimeout(() => dismissSlot(plugin.id), SYSTEM_MAX_DISPLAY_MS),
            );
          }
        }
      }),
    );

    return () => {
      unsubs.forEach((u) => u());
      for (const t of timers.values()) clearTimeout(t);
      for (const t of hardTimers.values()) clearTimeout(t);
      timers.clear();
      hardTimers.clear();
    };
  }, []);

  const contextValue = useRef<PluginSystemContextValue>({
    bus: busRef.current,
    registry,
  }).current;

  return (
    <PluginSystemContext.Provider value={contextValue}>
      {children}
      {createPortal(
        <PluginPortalHost slots={slots} dismiss={dismissPlugin} />,
        document.body,
      )}
    </PluginSystemContext.Provider>
  );
}

// ---------------------------------------------------------------------------
// Portal host — renders overlay slots
// ---------------------------------------------------------------------------

const POSITION_STYLES: Record<string, React.CSSProperties> = {
  center: {
    position: "fixed",
    top: "50%",
    left: "50%",
    transform: "translate(-50%, -50%)",
  },
  "top-right": {
    position: "fixed",
    top: "16px",
    right: "16px",
  },
  "bottom-right": {
    position: "fixed",
    bottom: "16px",
    right: "16px",
  },
  full: {
    position: "fixed",
    inset: 0,
  },
};

function PluginPortalHost({
  slots,
  dismiss,
}: {
  slots: OverlaySlot[];
  dismiss: (pluginId: string) => void;
}) {
  if (slots.length === 0) return null;

  return (
    <>
      {slots.map((slot) => {
        const config = slot.plugin.overlayConfig ?? {};
        const posStyle = POSITION_STYLES[config.position ?? "top-right"] ?? POSITION_STYLES["top-right"];

        return (
          <div
            key={slot.pluginId}
            style={{
              ...posStyle,
              zIndex: config.zIndex ?? 9999,
              pointerEvents: "auto",
            }}
          >
            <PluginOverlaySlot
              plugin={slot.plugin}
              event={slot.event}
              dismiss={() => dismiss(slot.pluginId)}
            />
          </div>
        );
      })}
    </>
  );
}

function PluginOverlaySlot({
  plugin,
  event,
  dismiss,
}: {
  plugin: UIPlugin;
  event: PluginEvent;
  dismiss: () => void;
}) {
  const pendingDismissRef = useRef(false);

  let content: ReactNode = null;
  try {
    content = plugin.render({ event, dismiss });
  } catch (err) {
    console.error(`[Plugin:${plugin.id}] render error:`, err);
  }

  // If render returned null, schedule dismiss in an effect (can't setState during render)
  if (content === null) {
    pendingDismissRef.current = true;
  } else {
    pendingDismissRef.current = false;
  }

  useEffect(() => {
    if (pendingDismissRef.current) {
      pendingDismissRef.current = false;
      dismiss();
    }
  });

  if (content === null) return null;
  return <>{content}</>;
}
