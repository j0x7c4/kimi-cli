/**
 * Built-in subagent spawn animation plugin.
 * Plays a 3-second entrance animation when a sub-agent is created, then auto-dismisses.
 */
import type {
  UIPlugin,
  PluginRenderProps,
  SubagentStartEvent,
} from "../types";

const AGENT_COLORS: Record<string, string> = {
  researcher: "#8b5cf6",
  coder: "#10b981",
  browser: "#f59e0b",
  planner: "#3b82f6",
  reviewer: "#ef4444",
  writer: "#ec4899",
};

function getColor(agentType: string | null): string {
  if (!agentType) return "#6366f1";
  const lower = agentType.toLowerCase();
  for (const [key, color] of Object.entries(AGENT_COLORS)) {
    if (lower.includes(key)) return color;
  }
  return "#6366f1";
}

const SubagentAnimationPlugin: UIPlugin = {
  id: "builtin:subagent-animation",
  name: "Subagent Animation",
  description: "Plays a 3-second animation when a sub-agent is spawned.",
  version: "1.0.0",
  author: "kimi-cli",
  events: ["subagent:start"],

  overlayConfig: {
    position: "top-right",
    zIndex: 9400,
    autoDismissMs: 3000,
  },

  render({ event }: PluginRenderProps) {
    if (event.type !== "subagent:start") return null;

    const e = event as SubagentStartEvent;
    const label = e.agentType ?? "Agent";
    const color = getColor(e.agentType);

    return (
      <div
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "10px",
          background: "#0f172a",
          color: "#e2e8f0",
          padding: "10px 16px",
          borderRadius: "12px",
          fontSize: "13px",
          fontWeight: 500,
          boxShadow: `0 4px 20px rgba(0,0,0,0.35), inset 0 0 0 1px ${color}40`,
          animation: "saEnter 0.4s cubic-bezier(0.16,1,0.3,1), saExit 0.4s ease-in 2.6s forwards",
          overflow: "hidden",
          position: "relative",
        }}
      >
        {/* Sweep light effect */}
        <div
          style={{
            position: "absolute",
            top: 0,
            left: "-100%",
            width: "100%",
            height: "100%",
            background: `linear-gradient(90deg, transparent 0%, ${color}15 50%, transparent 100%)`,
            animation: "saSweep 1.5s ease-in-out 0.3s",
            pointerEvents: "none",
          }}
        />

        {/* Spinning ring */}
        <span style={{ position: "relative", width: "22px", height: "22px", flexShrink: 0 }}>
          <svg width="22" height="22" viewBox="0 0 22 22" style={{ animation: "saSpin 1s linear infinite" }}><title>Loading</title>
            <circle cx="11" cy="11" r="9" fill="none" stroke={`${color}25`} strokeWidth="2" />
            <circle
              cx="11" cy="11" r="9" fill="none"
              stroke={color} strokeWidth="2"
              strokeLinecap="round" strokeDasharray="18 38"
            />
          </svg>
          <span
            style={{
              position: "absolute",
              top: "50%", left: "50%",
              transform: "translate(-50%, -50%)",
              width: "6px", height: "6px",
              borderRadius: "50%",
              background: color,
              boxShadow: `0 0 8px ${color}80`,
            }}
          />
        </span>

        <div style={{ display: "flex", flexDirection: "column", gap: "1px" }}>
          <span style={{ color, fontWeight: 600, fontSize: "13px" }}>{label}</span>
          <span style={{ color: "#64748b", fontSize: "11px" }}>spawning...</span>
        </div>

        {/* Progress bar */}
        <div
          style={{
            position: "absolute",
            bottom: 0, left: 0,
            height: "2px",
            background: `linear-gradient(90deg, ${color}, ${color}60)`,
            animation: "saProgress 3s linear forwards",
            borderRadius: "0 0 12px 12px",
          }}
        />

        <style>{`
          @keyframes saEnter {
            from { transform: translateX(30px) scale(0.9); opacity: 0; }
            to { transform: translateX(0) scale(1); opacity: 1; }
          }
          @keyframes saExit {
            from { transform: translateX(0); opacity: 1; }
            to { transform: translateX(30px); opacity: 0; }
          }
          @keyframes saSpin {
            to { transform: rotate(360deg); }
          }
          @keyframes saSweep {
            from { left: -100%; }
            to { left: 200%; }
          }
          @keyframes saProgress {
            from { width: 0%; }
            to { width: 100%; }
          }
        `}</style>
      </div>
    );
  },
};

export default SubagentAnimationPlugin;
