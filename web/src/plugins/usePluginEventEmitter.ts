/**
 * usePluginEventEmitter — translates raw WireEvents into semantic PluginEvents
 * and emits them on the plugin event bus.
 *
 * Returns a `translateWireEvent` function that should be called from
 * useSessionStream's onWireEventForPlugin callback.
 */
import { useCallback, useRef } from "react";
import type { WireEvent, ContentPartEvent, SubagentEventWire, ToolCallEvent } from "@/hooks/wireTypes";
import type { PluginEvent } from "./types";
import { usePluginBus } from "./PluginSystemProvider";

type ThinkingState = {
  messageId: string;
  accumulated: string;
  startedAt: number;
};

export type WireEventContext = {
  sessionId: string;
  isReplay: boolean;
};

/** Tool names that represent agent/subagent invocations */
const AGENT_TOOL_NAMES = new Set(["Agent", "agent", "dispatch_agent"]);

export function usePluginEventEmitter() {
  const bus = usePluginBus();
  const thinkingRef = useRef<ThinkingState | null>(null);
  // Track active subagents for cluster detection (from SubagentEvent wire events)
  const activeSubagentsRef = useRef<Map<string, { agentType: string | null; parentToolCallId: string }>>(new Map());
  // Track agent ToolCall events for subagent:stop cleanup
  const pendingAgentToolCallsRef = useRef<Map<string, { toolCallId: string; agentType: string | null }>>(new Map());
  // Accumulates ALL agents seen in the current turn (ToolCall + SubagentEvent paths)
  // NOT cleared by ToolResult, only by TurnBegin/StepInterrupted or after cluster fires
  const clusterWindowAgentsRef = useRef<Map<string, { agentId: string; agentType: string | null }>>(new Map());
  const clusterTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const clusterCounterRef = useRef(0);
  // Prevents emitting duplicate cluster events within the same turn
  const clusterEmittedRef = useRef(false);

  const emit = useCallback(
    (event: PluginEvent) => {
      bus.emit(event);
    },
    [bus],
  );

  const translateWireEvent = useCallback(
    (wireEvent: WireEvent, ctx: WireEventContext) => {
      // Skip replayed history — only fire plugins for live events
      if (ctx.isReplay) return;

      const { sessionId } = ctx;

      // End thinking state on any non-think event (e.g. ToolCall, text ContentPart, etc.)
      const isThinkChunk =
        wireEvent.type === "ContentPart" &&
        (wireEvent as ContentPartEvent).payload.type === "think" &&
        !!(wireEvent as ContentPartEvent).payload.think;

      if (!isThinkChunk && thinkingRef.current) {
        const state = thinkingRef.current;
        thinkingRef.current = null;
        emit({
          type: "thinking:end",
          sessionId,
          messageId: state.messageId,
          totalText: state.accumulated,
          durationMs: Date.now() - state.startedAt,
        });
      }

      switch (wireEvent.type) {
        case "ToolCall": {
          const tc = wireEvent as ToolCallEvent;
          const toolName = tc.payload.function?.name;
          if (AGENT_TOOL_NAMES.has(toolName)) {
            // Parse agent type from tool call arguments
            let agentType: string | null = null;
            try {
              const args = JSON.parse(tc.payload.function.arguments);
              agentType = args.subagent_type ?? args.description ?? null;
            } catch {
              // ignore parse errors
            }
            const toolCallId = tc.payload.id;
            pendingAgentToolCallsRef.current.set(toolCallId, { toolCallId, agentType });
            // Add to cluster window — persists for the whole turn so sequential
            // background agents (launched in separate LLM steps) are counted
            clusterWindowAgentsRef.current.set(toolCallId, { agentId: toolCallId, agentType });

            emit({
              type: "subagent:start",
              sessionId,
              agentId: toolCallId,
              agentType,
              parentToolCallId: toolCallId,
            });

            // Cluster detection: fire as soon as the 2nd agent is seen.
            // Use a short debounce (150ms) to batch agents launched in parallel
            // (same LLM step). Sequential agents (different steps) also accumulate
            // in clusterWindowAgentsRef and trigger on the 2nd one.
            if (clusterWindowAgentsRef.current.size >= 2 && !clusterEmittedRef.current) {
              if (clusterTimerRef.current) clearTimeout(clusterTimerRef.current);
              clusterTimerRef.current = setTimeout(() => {
                if (clusterEmittedRef.current) { clusterTimerRef.current = null; return; }
                const allAgents = new Map(clusterWindowAgentsRef.current);
                for (const [id, info] of activeSubagentsRef.current) {
                  if (!allAgents.has(id)) {
                    allAgents.set(id, { agentId: id, agentType: info.agentType });
                  }
                }
                if (allAgents.size >= 2) {
                  clusterEmittedRef.current = true;
                  emit({
                    type: "subagent:cluster",
                    sessionId,
                    clusterId: `cluster_${++clusterCounterRef.current}`,
                    agentCount: allAgents.size,
                    agents: Array.from(allAgents.values()),
                  });
                }
                clusterTimerRef.current = null;
              }, 150);
            }
          }
          break;
        }

        case "ToolResult": {
          // Clean up pending agent tool calls on result
          const tr = wireEvent as { type: "ToolResult"; payload: { tool_call_id: string } };
          if (pendingAgentToolCallsRef.current.has(tr.payload.tool_call_id)) {
            pendingAgentToolCallsRef.current.delete(tr.payload.tool_call_id);
            emit({
              type: "subagent:stop",
              sessionId,
              agentId: tr.payload.tool_call_id,
            });
          }
          break;
        }

        case "ContentPart": {
          const cp = wireEvent as ContentPartEvent;
          if (cp.payload.type === "think" && cp.payload.think) {
            if (!thinkingRef.current) {
              // First think chunk — emit thinking:start
              const messageId = `think_${Date.now()}`;
              thinkingRef.current = {
                messageId,
                accumulated: cp.payload.think,
                startedAt: Date.now(),
              };
              emit({
                type: "thinking:start",
                sessionId,
                messageId,
              });
            } else {
              thinkingRef.current.accumulated += cp.payload.think;
            }

            emit({
              type: "thinking:chunk",
              sessionId,
              messageId: thinkingRef.current.messageId,
              chunk: cp.payload.think,
              accumulated: thinkingRef.current.accumulated,
            });
          }
          break;
        }

        case "SubagentEvent": {
          const sub = wireEvent as SubagentEventWire;
          const agentId = sub.payload.agent_id ?? "unknown";
          const parentToolCallId = sub.payload.parent_tool_call_id ?? "";
          const innerType = sub.payload.event?.type;

          if (innerType === "TurnBegin") {
            const agentType = sub.payload.subagent_type ?? null;
            activeSubagentsRef.current.set(agentId, { agentType, parentToolCallId });
            // Also track in cluster window so sequential foreground agents accumulate
            clusterWindowAgentsRef.current.set(agentId, { agentId, agentType });
            emit({
              type: "subagent:start",
              sessionId,
              agentId,
              agentType,
              parentToolCallId,
            });

            // Cluster detection: fire as soon as 2nd agent is seen.
            // 150ms debounce batches agents launched in parallel.
            if (clusterWindowAgentsRef.current.size >= 2 && !clusterEmittedRef.current) {
              if (clusterTimerRef.current) clearTimeout(clusterTimerRef.current);
              clusterTimerRef.current = setTimeout(() => {
                if (clusterEmittedRef.current) { clusterTimerRef.current = null; return; }
                const allAgents = new Map(clusterWindowAgentsRef.current);
                for (const [id, info] of activeSubagentsRef.current) {
                  if (!allAgents.has(id)) {
                    allAgents.set(id, { agentId: id, agentType: info.agentType });
                  }
                }
                if (allAgents.size >= 2) {
                  clusterEmittedRef.current = true;
                  emit({
                    type: "subagent:cluster",
                    sessionId,
                    clusterId: `cluster_${++clusterCounterRef.current}`,
                    agentCount: allAgents.size,
                    agents: Array.from(allAgents.values()),
                  });
                }
                clusterTimerRef.current = null;
              }, 150);
            }
          }

          // Detect subagent completion (inner TurnEnd or step end)
          if (innerType === "TurnEnd" || innerType === "StepInterrupted") {
            activeSubagentsRef.current.delete(agentId);
            emit({
              type: "subagent:stop",
              sessionId,
              agentId,
            });
          }
          break;
        }

        case "TurnBegin": {
          activeSubagentsRef.current.clear();
          pendingAgentToolCallsRef.current.clear();
          clusterWindowAgentsRef.current.clear();
          clusterEmittedRef.current = false;
          if (clusterTimerRef.current) {
            clearTimeout(clusterTimerRef.current);
            clusterTimerRef.current = null;
          }
          emit({ type: "turn:begin", sessionId, turnIndex: 0 });
          break;
        }

        case "StepInterrupted": {
          activeSubagentsRef.current.clear();
          pendingAgentToolCallsRef.current.clear();
          clusterWindowAgentsRef.current.clear();
          clusterEmittedRef.current = false;
          if (clusterTimerRef.current) {
            clearTimeout(clusterTimerRef.current);
            clusterTimerRef.current = null;
          }
          emit({ type: "turn:end", sessionId });
          break;
        }

        default:
          break;
      }
    },
    [emit],
  );

  return { translateWireEvent };
}
