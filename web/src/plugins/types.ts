import type { ReactNode } from "react";

// ---------------------------------------------------------------------------
// Plugin Events — semantic events translated from wire protocol events
// ---------------------------------------------------------------------------

export type ThinkingStartEvent = {
  type: "thinking:start";
  sessionId: string;
  messageId: string;
};

export type ThinkingChunkEvent = {
  type: "thinking:chunk";
  sessionId: string;
  messageId: string;
  chunk: string;
  accumulated: string;
};

export type ThinkingEndEvent = {
  type: "thinking:end";
  sessionId: string;
  messageId: string;
  totalText: string;
  durationMs: number;
};

export type SubagentStartEvent = {
  type: "subagent:start";
  sessionId: string;
  agentId: string;
  agentType: string | null;
  parentToolCallId: string;
};

export type SubagentStopEvent = {
  type: "subagent:stop";
  sessionId: string;
  agentId: string;
};

export type SubagentClusterEvent = {
  type: "subagent:cluster";
  sessionId: string;
  clusterId: string;
  agentCount: number;
  agents: Array<{ agentId: string; agentType: string | null }>;
};

export type TurnBeginPluginEvent = {
  type: "turn:begin";
  sessionId: string;
  turnIndex: number;
};

export type TurnEndPluginEvent = {
  type: "turn:end";
  sessionId: string;
};

export type PluginEvent =
  | ThinkingStartEvent
  | ThinkingChunkEvent
  | ThinkingEndEvent
  | SubagentStartEvent
  | SubagentStopEvent
  | SubagentClusterEvent
  | TurnBeginPluginEvent
  | TurnEndPluginEvent;

export type PluginEventType = PluginEvent["type"];

// ---------------------------------------------------------------------------
// Plugin interface
// ---------------------------------------------------------------------------

export type PluginRenderProps = {
  event: PluginEvent;
  /** Call dismiss() to remove this overlay instance. */
  dismiss: () => void;
};

export type PluginOverlayConfig = {
  position?: "center" | "top-right" | "bottom-right" | "full";
  dismissible?: boolean;
  zIndex?: number;
  /** Auto-dismiss overlay after N milliseconds. Timer resets on each new event. */
  autoDismissMs?: number;
};

export interface UIPlugin {
  id: string;
  name: string;
  description?: string;
  version?: string;
  author?: string;
  events: PluginEventType[];
  render: (props: PluginRenderProps) => ReactNode;
  overlayConfig?: PluginOverlayConfig;
}

// ---------------------------------------------------------------------------
// Plugin Registry
// ---------------------------------------------------------------------------

export interface PluginRegistry {
  register(plugin: UIPlugin): () => void;
  unregister(pluginId: string): void;
  getPlugins(): UIPlugin[];
}
