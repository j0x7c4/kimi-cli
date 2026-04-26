/**
 * Built-in subagent cluster animation plugin.
 * Plays a 3-second orbital formation animation when multiple sub-agents
 * are launched concurrently, then auto-dismisses.
 */
import type {
  UIPlugin,
  PluginRenderProps,
  SubagentClusterEvent,
} from "../types";

const NODE_COLORS = [
  "#3b82f6", "#8b5cf6", "#10b981", "#f59e0b",
  "#ef4444", "#ec4899", "#06b6d4", "#f97316",
];

const SubagentClusterPlugin: UIPlugin = {
  id: "builtin:subagent-cluster",
  name: "Subagent Cluster",
  description: "Plays an orbital formation animation when multiple sub-agents launch concurrently.",
  version: "2.0.0",
  author: "kimi-cli",
  events: ["subagent:cluster"],

  overlayConfig: {
    position: "bottom-right",
    zIndex: 9400,
    autoDismissMs: 3000,
  },

  render({ event }: PluginRenderProps) {
    if (event.type !== "subagent:cluster") return null;

    const cluster = event as SubagentClusterEvent;
    const count = cluster.agentCount;
    const size = Math.min(200, 120 + count * 16);
    const orbitR = size * 0.32;
    const cx = size / 2;
    const cy = size / 2;

    const nodes = cluster.agents.map((agent, i) => {
      const angle = (2 * Math.PI * i) / count - Math.PI / 2;
      return {
        x: cx + orbitR * Math.cos(angle),
        y: cy + orbitR * Math.sin(angle),
        lx: cx + (orbitR + 20) * Math.cos(angle),
        ly: cy + (orbitR + 20) * Math.sin(angle),
        color: NODE_COLORS[i % NODE_COLORS.length],
        label: agent.agentType ?? `#${i + 1}`,
        delay: i * 0.1,
      };
    });

    return (
      <div
        style={{
          background: "linear-gradient(145deg, #0c1222 0%, #1a1f3a 100%)",
          borderRadius: "16px",
          padding: "14px",
          boxShadow: "0 12px 40px rgba(0,0,0,0.4), inset 0 0 0 1px rgba(99,102,241,0.12)",
          animation: "scEnter 0.4s cubic-bezier(0.16,1,0.3,1), scExit 0.4s ease-in 2.6s forwards",
          position: "relative",
          overflow: "hidden",
        }}
      >
        {/* Background pulse */}
        <div
          style={{
            position: "absolute",
            top: "50%", left: "50%",
            width: `${size * 0.8}px`, height: `${size * 0.8}px`,
            transform: "translate(-50%, -50%)",
            borderRadius: "50%",
            background: "radial-gradient(circle, rgba(99,102,241,0.08) 0%, transparent 70%)",
            animation: "scBgPulse 2s ease-in-out infinite",
            pointerEvents: "none",
          }}
        />

        {/* Header */}
        <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px", position: "relative" }}>
          <span
            style={{
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              width: "22px", height: "22px", borderRadius: "6px",
              background: "rgba(99,102,241,0.15)", fontSize: "12px",
            }}
          >
            &#x26A1;
          </span>
          <div>
            <div style={{ color: "#e2e8f0", fontWeight: 600, fontSize: "13px", lineHeight: 1.2 }}>
              Cluster Formation
            </div>
            <div style={{ color: "#64748b", fontSize: "11px" }}>
              {count} agents launching
            </div>
          </div>
        </div>

        {/* Orbital SVG */}
        <div style={{ position: "relative", width: `${size}px`, height: `${size}px`, margin: "0 auto 6px" }}>
          <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={{ position: "absolute", top: 0, left: 0 }}>
            {/* Orbit ring — draws in */}
            <circle
              cx={cx} cy={cy} r={orbitR}
              fill="none" stroke="rgba(99,102,241,0.15)" strokeWidth="1"
              strokeDasharray={`${2 * Math.PI * orbitR}`}
              strokeDashoffset={`${2 * Math.PI * orbitR}`}
              style={{ animation: "scOrbitDraw 0.8s ease-out 0.1s forwards" }}
            />

            {/* Connections — fade in */}
            {nodes.map((n, i) =>
              nodes.slice(i + 1).map((m, j) => (
                <line
                  key={`${i}-${i + 1 + j}`}
                  x1={n.x} y1={n.y} x2={m.x} y2={m.y}
                  stroke="rgba(99,102,241,0.06)" strokeWidth="1"
                  style={{ animation: `scLine 0.4s ease-out ${Math.max(n.delay, m.delay) + 0.3}s both` }}
                />
              )),
            )}

            {/* Center hub */}
            <circle cx={cx} cy={cy} r="10" fill="rgba(99,102,241,0.12)" style={{ animation: "scHubPulse 2s ease-in-out infinite" }} />
            <circle cx={cx} cy={cy} r="3.5" fill="#6366f1" />

            {/* Agent nodes — pop in sequentially */}
            {nodes.map((n, i) => (
              <g key={i} style={{ animation: `scNodePop 0.35s cubic-bezier(0.34,1.56,0.64,1) ${n.delay + 0.2}s both` }}>
                <circle cx={n.x} cy={n.y} r="12" fill={`${n.color}10`} style={{ animation: `scGlow 2s ease-in-out ${n.delay}s infinite` }} />
                <circle cx={n.x} cy={n.y} r="6" fill={`${n.color}25`} stroke={n.color} strokeWidth="1.5" />
                <circle cx={n.x} cy={n.y} r="2" fill={n.color} />
              </g>
            ))}
          </svg>

          {/* Node labels */}
          {nodes.map((n, i) => (
            <div
              key={i}
              style={{
                position: "absolute",
                left: `${n.lx}px`, top: `${n.ly}px`,
                transform: "translate(-50%, -50%)",
                fontSize: "10px", fontWeight: 500,
                color: n.color,
                whiteSpace: "nowrap",
                textShadow: "0 1px 4px rgba(0,0,0,0.8)",
                animation: `scLabelIn 0.3s ease-out ${n.delay + 0.4}s both`,
              }}
            >
              {n.label}
            </div>
          ))}
        </div>

        {/* Bottom progress */}
        <div
          style={{
            position: "absolute",
            bottom: 0, left: 0,
            height: "2px",
            background: "linear-gradient(90deg, #6366f1, #6366f160)",
            animation: "scProgress 3s linear forwards",
            borderRadius: "0 0 16px 16px",
          }}
        />

        <style>{`
          @keyframes scEnter {
            from { transform: scale(0.8) translateY(20px); opacity: 0; }
            to { transform: scale(1) translateY(0); opacity: 1; }
          }
          @keyframes scExit {
            from { transform: scale(1); opacity: 1; }
            to { transform: scale(0.9) translateY(10px); opacity: 0; }
          }
          @keyframes scOrbitDraw {
            to { stroke-dashoffset: 0; }
          }
          @keyframes scNodePop {
            from { transform: scale(0); opacity: 0; }
            to { transform: scale(1); opacity: 1; }
          }
          @keyframes scLine {
            from { opacity: 0; }
            to { opacity: 1; }
          }
          @keyframes scHubPulse {
            0%, 100% { r: 10; opacity: 0.2; }
            50% { r: 14; opacity: 0.08; }
          }
          @keyframes scGlow {
            0%, 100% { r: 12; opacity: 0.25; }
            50% { r: 16; opacity: 0.08; }
          }
          @keyframes scLabelIn {
            from { opacity: 0; transform: translate(-50%, -50%) translateY(4px); }
            to { opacity: 1; transform: translate(-50%, -50%) translateY(0); }
          }
          @keyframes scBgPulse {
            0%, 100% { transform: translate(-50%, -50%) scale(1); opacity: 0.5; }
            50% { transform: translate(-50%, -50%) scale(1.15); opacity: 0.3; }
          }
          @keyframes scProgress {
            from { width: 0%; }
            to { width: 100%; }
          }
        `}</style>
      </div>
    );
  },
};

export default SubagentClusterPlugin;
