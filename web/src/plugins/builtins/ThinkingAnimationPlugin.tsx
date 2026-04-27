/**
 * Built-in thinking animation plugin — Pixel Studio style.
 *
 * Phases:
 *   0.0s ~ 1.2s  Robot floats, "THINKING..." dots animate
 *   1.2s ~ 2.8s  Bulb appears with blinking stars
 *   2.8s ~ 4.8s  Bulb lights up, "Eureka!"
 *   4.8s ~ 5.0s  Auto-dismiss
 */
import { useState, useEffect, useCallback } from "react";
import type { UIPlugin, PluginRenderProps } from "../types";

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

const HERO_IDLE = [
  " ....######.... ",
  " ...#BBBBBB#... ",
  " ..#BBBBBBBB#.. ",
  " ..#BYBBBBGB#.. ",
  " ..#BBBBBBBB#.. ",
  " ...#BBBBBB#... ",
  " ....#BBBB#.... ",
  " ....#BBBB#.... ",
  " ...#BBBBBB#... ",
  " ..#BBBBBBBB#.. ",
  " ..#BBBBBBBB#.. ",
  " ..#BBBBBBBB#.. ",
  " ...########... ",
];

const BULB_OFF = [
  " ..#.. ",
  ".###.",
  ".###.",
  ".###.",
  " ..#.. ",
  " ..#.. ",
  ".###.",
];

const BULB_ON = [
  " ..Y.. ",
  ".YYY.",
  ".YYY.",
  ".YYY.",
  " ..Y.. ",
  " ..Y.. ",
  ".YYY.",
];

const STAR = ["..#..", ".###.", "#####", ".###.", "..#.."];

// ---------------------------------------------------------------------------
// Pixel rendering components
// ---------------------------------------------------------------------------

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
      {grid.flatMap((row, y) =>
        row.split("").map((ch, x) => (
          <div
            key={`${x}-${y}`}
            style={{
              width: pixelSize,
              height: pixelSize,
              backgroundColor: palette[ch] || "transparent",
              imageRendering: "pixelated",
            }}
          />
        )),
      )}
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
// Scene
// ---------------------------------------------------------------------------

function ThinkingScene({ dismiss }: { dismiss: () => void }) {
  const [phase, setPhase] = useState(0);
  const [dots, setDots] = useState("");

  useEffect(() => {
    const dotInterval = setInterval(() => {
      setDots((prev) => (prev.length >= 3 ? "" : `${prev}.`));
    }, 400);

    const t1 = setTimeout(() => setPhase(1), 1200);
    const t2 = setTimeout(() => setPhase(2), 2800);
    const t3 = setTimeout(() => {
      setPhase(3);
      dismiss();
    }, 4800);

    return () => {
      clearInterval(dotInterval);
      clearTimeout(t1);
      clearTimeout(t2);
      clearTimeout(t3);
    };
  }, [dismiss]);

  return (
    <div
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
        onClick={(e) => e.stopPropagation()}
      >
        <PixelText
          text={`THINKING${dots}`}
          size={20}
          color="#93c5fd"
          style={{ marginBottom: "8px" }}
        />

        {/* Bot wrapper — floating */}
        <div
          style={{
            position: "relative",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            animation: "psFloat 2.5s ease-in-out infinite",
          }}
        >
          {/* Bulb area */}
          <div
            style={{
              marginBottom: "4px",
              opacity: phase >= 1 ? 1 : 0,
              transition: "opacity 0.4s",
              display: "flex",
              gap: "12px",
              alignItems: "flex-end",
              height: "42px",
            }}
          >
            {phase >= 1 && (
              <PixelSprite
                grid={STAR}
                palette={{ ...PALETTE, "#": "#facc15" }}
                pixelSize={2}
                style={{
                  animation: "psBlink 0.6s infinite alternate",
                  marginBottom: "4px",
                }}
              />
            )}
            <PixelSprite
              grid={phase >= 2 ? BULB_ON : BULB_OFF}
              palette={PALETTE}
              pixelSize={4}
              style={{
                filter:
                  phase >= 2
                    ? "drop-shadow(0 0 8px rgba(250,204,21,0.8))"
                    : "none",
                transition: "filter 0.5s",
              }}
            />
            {phase >= 1 && (
              <PixelSprite
                grid={STAR}
                palette={{ ...PALETTE, "#": "#facc15" }}
                pixelSize={2}
                style={{
                  animation: "psBlink 0.7s infinite alternate 0.3s",
                  marginBottom: "4px",
                }}
              />
            )}
          </div>

          {/* Hero robot */}
          <PixelSprite
            grid={HERO_IDLE}
            palette={PALETTE}
            pixelSize={6}
            style={{ filter: "drop-shadow(0 8px 0px rgba(0,0,0,0.3))" }}
          />
        </div>

        {/* Status text */}
        <PixelText
          text={
            phase === 0
              ? "Analyzing context..."
              : phase === 1
                ? "Reasoning through..."
                : "Eureka!"
          }
          size={12}
          color={phase === 2 ? "#facc15" : "#64748b"}
          style={{ marginTop: "16px", transition: "color 0.4s" }}
        />

        {/* Progress bar */}
        <div
          style={{
            marginTop: "12px",
            width: "120px",
            height: "4px",
            backgroundColor: "rgba(148,163,184,0.2)",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              height: "100%",
              width: "100%",
              backgroundColor: "#3b82f6",
              animation: "psThinkProgress 4.8s linear forwards",
            }}
          />
        </div>
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
        @keyframes psBlink {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
        @keyframes psThinkProgress {
          from { transform: scaleX(1); transform-origin: left; }
          to { transform: scaleX(0); transform-origin: left; }
        }
      `}</style>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Plugin export
// ---------------------------------------------------------------------------

const ThinkingAnimationPlugin: UIPlugin = {
  id: "builtin:thinking-animation",
  name: "Thinking Animation",
  description:
    "Pixel-art robot thinking animation with phased bulb/eureka sequence. Auto-dismiss within 5s.",
  version: "2.0.0",
  author: "Pixel Studio",
  events: ["thinking:start", "thinking:end"],

  overlayConfig: {
    position: "full",
    dismissible: true,
    zIndex: 9600,
  },

  render({ event, dismiss }: PluginRenderProps) {
    if (event.type === "thinking:start") {
      return <ThinkingScene dismiss={dismiss} key={event.messageId} />;
    }
    if (event.type === "thinking:end") {
      return null;
    }
    return null;
  },
};

export default ThinkingAnimationPlugin;
