/**
 * Built-in subagent cluster animation plugin — Pixel Studio style.
 *
 * Phases:
 *   0.0s ~ 0.5s  Hero robot appears center, signal waves expand
 *   0.6s ~ 2.4s  Minion bots fly out from center to circular positions
 *   2.4s ~ 4.4s  All bots float, "All units online!"
 *   5.0s         Auto-dismiss
 */
import { useState, useEffect, useMemo } from "react";
import type {
  UIPlugin,
  PluginRenderProps,
  SubagentClusterEvent,
} from "../types";

// ---------------------------------------------------------------------------
// Palette & Sprites
// ---------------------------------------------------------------------------

const PALETTE: Record<string, string | null> = {
  " ": null,
  "#": "#0f172a",
  B: "#3b82f6",
  L: "#93c5fd",
  D: "#1e3a8a",
  Y: "#facc15",
  W: "#ffffff",
  P: "#f472b6",
  R: "#ef4444",
  G: "#22c55e",
  O: "#fb923c",
  C: "#06b6d4",
};

const HERO_WAVE = [
  " ....######.... ",
  " ...#BBBBBB#... ",
  " ..#BBBBBBBB#.. ",
  " ..#BYBBBBGB#.. ",
  " ..#BBBBBBBB#.. ",
  " .#.#BBBBBB#.#. ",
  " .#..#BBBB#..#. ",
  " ....#BBBB#.... ",
  " ...#BBBBBB#... ",
  " ..#BBBBBBBB#.. ",
  " ..#BBBBBBBB#.. ",
  " ..#BBBBBBBB#.. ",
  " ...########... ",
];

const MINION_IDLE = [
  " ..####.. ",
  ".#BBBBBB#.",
  "#BYBBBBGB#",
  "#BBBBBBBB#",
  ".#BBBBBB#.",
  "..#BBBB#..",
  "..#BBBB#..",
  ".#BBBBBB#.",
  "..######..",
];

const MINION_THEMES = [
  { B: "#3b82f6", L: "#93c5fd", D: "#1e3a8a" },
  { B: "#ef4444", L: "#fca5a5", D: "#991b1b" },
  { B: "#22c55e", L: "#86efac", D: "#166534" },
  { B: "#f59e0b", L: "#fcd34d", D: "#92400e" },
  { B: "#a855f7", L: "#d8b4fe", D: "#6b21a8" },
  { B: "#06b6d4", L: "#67e8f9", D: "#155e75" },
  { B: "#ec4899", L: "#fbcfe8", D: "#9d174d" },
  { B: "#84cc16", L: "#d9f99d", D: "#3f6212" },
];

const SPAWN_DURATION = 2400;
const HOLD_DURATION = 2000;
const FLY_RADIUS = 150;

// ---------------------------------------------------------------------------
// Pixel rendering components
// ---------------------------------------------------------------------------

type Pixel = { key: string; color: string | null };

function PixelSprite({
  grid,
  palette,
  pixelSize = 4,
  style = {},
}: {
  grid: string[];
  palette: Record<string, string | null>;
  pixelSize?: number;
  style?: React.CSSProperties;
}) {
  const rows = grid.length;
  const cols = grid[0]?.length || 0;
  const pixels: Pixel[] = [];
  for (let y = 0; y < rows; y++) {
    const chars = grid[y] ?? "";
    for (let x = 0; x < chars.length; x++) {
      pixels.push({ key: `${y}-${x}`, color: palette[chars[x] ?? " "] ?? null });
    }
  }
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: `repeat(${cols}, ${pixelSize}px)`,
        gridTemplateRows: `repeat(${rows}, ${pixelSize}px)`,
        gap: 0,
        lineHeight: 0,
        ...style,
      }}
    >
      {pixels.map((px) => (
        <div
          key={px.key}
          style={{
            width: pixelSize,
            height: pixelSize,
            backgroundColor: px.color ?? "transparent",
            imageRendering: "pixelated",
          }}
        />
      ))}
    </div>
  );
}

function PixelText({
  text,
  size = 16,
  color = "#e2e8f0",
  style = {},
}: {
  text: string;
  size?: number;
  color?: string;
  style?: React.CSSProperties;
}) {
  return (
    <div
      style={{
        fontFamily: '"Courier New", Courier, "SF Mono", Monaco, monospace',
        fontSize: size,
        fontWeight: 800,
        color,
        letterSpacing: "0.08em",
        lineHeight: 1.3,
        textAlign: "center",
        textShadow: "2px 2px 0px rgba(0,0,0,0.5)",
        ...style,
      }}
    >
      {text}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Signal waves
// ---------------------------------------------------------------------------

function SignalWaves() {
  const [waves, setWaves] = useState<{ id: number; born: number }[]>([]);

  useEffect(() => {
    const timers = [0, 500, 1000].map((delay, i) =>
      setTimeout(() => {
        setWaves((prev) => [...prev, { id: Date.now() + i, born: Date.now() }]);
      }, delay),
    );
    return () => timers.forEach(clearTimeout);
  }, []);

  useEffect(() => {
    const interval = setInterval(() => {
      const now = Date.now();
      setWaves((prev) => prev.filter((w) => now - w.born < 1500));
    }, 300);
    return () => clearInterval(interval);
  }, []);

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        pointerEvents: "none",
      }}
    >
      {waves.map((w) => (
        <div
          key={w.id}
          style={{
            position: "absolute",
            width: "80px",
            height: "80px",
            border: "3px solid rgba(59,130,246,0.9)",
            borderRadius: "4px",
            animation: "psSignal 1.2s ease-out forwards",
            imageRendering: "pixelated",
          }}
        />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Minion bot
// ---------------------------------------------------------------------------

function MinionBot({
  index,
  total,
  agentType,
  delay,
}: {
  index: number;
  total: number;
  agentType: string;
  delay: number;
}) {
  const [landed, setLanded] = useState(false);

  const pos = useMemo(() => {
    const angle = (index / total) * Math.PI * 2 - Math.PI / 2;
    return {
      x: Math.cos(angle) * FLY_RADIUS,
      y: Math.sin(angle) * FLY_RADIUS,
    };
  }, [index, total]);

  const theme = MINION_THEMES[index % MINION_THEMES.length];
  const palette = { ...PALETTE, ...theme };

  useEffect(() => {
    const t = setTimeout(() => setLanded(true), delay);
    return () => clearTimeout(t);
  }, [delay]);

  return (
    <div
      style={{
        position: "absolute",
        left: "50%",
        top: "50%",
        transform: landed
          ? `translate(calc(-50% + ${pos.x}px), calc(-50% + ${pos.y}px))`
          : "translate(-50%, -50%) scale(0.2)",
        opacity: landed ? 1 : 0,
        transition: "all 0.6s cubic-bezier(0.34, 1.56, 0.64, 1)",
      }}
    >
      {/* Inner wrapper for float animation — keeps it separate from positioning transform */}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: "6px",
          animation: landed ? "psFloat 2s ease-in-out infinite" : "none",
          animationDelay: `${index * 0.15}s`,
        }}
      >
        <PixelSprite
          grid={MINION_IDLE}
          palette={palette}
          pixelSize={5}
          style={{ filter: "drop-shadow(0 4px 0 rgba(0,0,0,0.25))" }}
        />
        {agentType && (
          <PixelText text={agentType} size={10} color={theme.L} style={{ marginTop: "2px" }} />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Scene
// ---------------------------------------------------------------------------

function ClusterScene({
  agents,
  dismiss,
}: {
  agents: Array<{ agentId: string; agentType: string | null }>;
  dismiss: () => void;
}) {
  const [phase, setPhase] = useState(0);
  const count = Math.min(agents.length, 8);

  useEffect(() => {
    const t1 = setTimeout(() => setPhase(1), SPAWN_DURATION);
    const t2 = setTimeout(() => {
      setPhase(2);
      dismiss();
    }, SPAWN_DURATION + HOLD_DURATION + 600);

    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
    };
  }, [dismiss]);

  return (
    <button
      type="button"
      style={{
        width: "100vw",
        height: "100vh",
        backgroundColor: "rgba(15,23,42,0.88)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        animation: "psFadeIn 0.2s ease-out",
        cursor: "pointer",
        border: "none",
        padding: 0,
      }}
      onClick={dismiss}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: "24px",
          cursor: "default",
          pointerEvents: "none",
        }}
      >
        <PixelText
          text={`${agents.length} AGENTS DEPLOYED`}
          size={18}
          color="#60a5fa"
          style={{ marginBottom: "4px" }}
        />
        <PixelText
          text="CLUSTER MODE ACTIVATED"
          size={11}
          color="#475569"
          style={{ marginBottom: "16px" }}
        />

        {/* Stage */}
        <div
          style={{
            position: "relative",
            width: `${FLY_RADIUS * 2 + 120}px`,
            height: `${FLY_RADIUS * 2 + 80}px`,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <SignalWaves />

          {/* Hero bot center */}
          <div style={{ zIndex: 10, animation: "psPulse 2s ease-in-out infinite" }}>
            <PixelSprite
              grid={HERO_WAVE}
              palette={PALETTE}
              pixelSize={8}
              style={{ filter: "drop-shadow(0 10px 0 rgba(0,0,0,0.3))" }}
            />
          </div>

          {/* Minion bots */}
          {agents.slice(0, count).map((agent, i) => (
            <MinionBot
              key={agent.agentId}
              index={i}
              total={count}
              agentType={agent.agentType?.slice(0, 8) || `BOT-${i + 1}`}
              delay={400 + i * 220}
            />
          ))}
        </div>

        <PixelText
          text={phase === 0 ? "Summoning units..." : "All units online!"}
          size={12}
          color={phase === 0 ? "#64748b" : "#4ade80"}
          style={{ marginTop: "20px" }}
        />
      </div>

      <style>{`
        @keyframes psFadeIn {
          from { opacity: 0; }
          to { opacity: 1; }
        }
        @keyframes psFloat {
          0%, 100% { transform: translateY(0); }
          50% { transform: translateY(-8px); }
        }
        @keyframes psPulse {
          0% { transform: scale(1); opacity: 1; }
          50% { transform: scale(1.05); opacity: 0.9; }
          100% { transform: scale(1); opacity: 1; }
        }
        @keyframes psSignal {
          0% { transform: scale(0.5); opacity: 1; border-color: rgba(59,130,246,0.8); }
          100% { transform: scale(2.5); opacity: 0; border-color: rgba(59,130,246,0); }
        }
      `}</style>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Plugin export
// ---------------------------------------------------------------------------

const SubagentClusterPlugin: UIPlugin = {
  id: "builtin:subagent-cluster",
  name: "Subagent Cluster",
  description:
    "Pixel-art summoning animation when subagents spawn in cluster. Auto-dismiss within 5s.",
  version: "2.0.0",
  author: "Pixel Studio",
  events: ["subagent:cluster"],

  overlayConfig: {
    position: "full",
    dismissible: true,
    zIndex: 9600,
  },

  render({ event, dismiss }: PluginRenderProps) {
    if (event.type !== "subagent:cluster") return null;

    const cluster = event as SubagentClusterEvent;
    return (
      <ClusterScene
        agents={cluster.agents}
        dismiss={dismiss}
        key={cluster.clusterId}
      />
    );
  },
};

export default SubagentClusterPlugin;
