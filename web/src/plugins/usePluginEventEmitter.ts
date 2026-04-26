/**
 * usePluginEventEmitter — translates raw WireEvents into semantic PluginEvents
 * and emits them on the plugin event bus.
 *
 * Returns a `translateWireEvent` function that should be called from
 * useSessionStream's onWireEventForPlugin callback.
 */
import { useCallback, useRef } from "react";
import type { WireEvent, ContentPartEvent, SubagentEventWire } from "@/hooks/wireTypes";
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

export function usePluginEventEmitter() {
  const bus = usePluginBus();
  const thinkingRef = useRef<ThinkingState | null>(null);
  // Track active subagents for cluster detection
  const activeSubagentsRef = useRef<Map<string, { agentType: string | null; parentToolCallId: string }>>(new Map());
  const clusterTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const clusterCounterRef = useRef(0);

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
            activeSubagentsRef.current.set(agentId, {
              agentType: sub.payload.subagent_type ?? null,
              parentToolCallId,
            });
            emit({
              type: "subagent:start",
              sessionId,
              agentId,
              agentType: sub.payload.subagent_type ?? null,
              parentToolCallId,
            });

            // Cluster detection: if multiple subagents start within a short window,
            // emit a cluster event
            if (clusterTimerRef.current) {
              clearTimeout(clusterTimerRef.current);
            }
            clusterTimerRef.current = setTimeout(() => {
              const active = activeSubagentsRef.current;
              if (active.size >= 2) {
                emit({
                  type: "subagent:cluster",
                  sessionId,
                  clusterId: `cluster_${++clusterCounterRef.current}`,
                  agentCount: active.size,
                  agents: Array.from(active.entries()).map(([id, info]) => ({
                    agentId: id,
                    agentType: info.agentType,
                  })),
                });
              }
              clusterTimerRef.current = null;
            }, 500);
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
          emit({ type: "turn:begin", sessionId, turnIndex: 0 });
          break;
        }

        case "StepInterrupted": {
          activeSubagentsRef.current.clear();
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
