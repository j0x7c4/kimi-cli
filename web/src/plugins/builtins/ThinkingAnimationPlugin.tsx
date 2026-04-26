/**
 * Built-in thinking animation plugin.
 * Shows a pulsing "Thinking..." badge overlay while the agent is reasoning.
 */
import type { UIPlugin, PluginRenderProps } from "../types";

const ThinkingAnimationPlugin: UIPlugin = {
  id: "builtin:thinking-animation",
  name: "Thinking Animation",
  description: "Shows an animated overlay badge while the agent is thinking.",
  version: "1.0.0",
  author: "kimi-cli",
  events: ["thinking:start", "thinking:chunk", "thinking:end"],

  overlayConfig: {
    position: "top-right",
    zIndex: 9500,
  },

  render({ event }: PluginRenderProps) {
    if (event.type === "thinking:end") {
      // Return null → PluginOverlaySlot auto-dismisses
      return null;
    }

    if (event.type === "thinking:start" || event.type === "thinking:chunk") {
      const dots =
        event.type === "thinking:chunk"
          ? ".".repeat((event.accumulated.length % 3) + 1)
          : "...";

      return (
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "8px",
            background: "linear-gradient(135deg, #1e40af 0%, #3b82f6 100%)",
            color: "white",
            padding: "8px 16px",
            borderRadius: "9999px",
            fontSize: "13px",
            fontWeight: 500,
            boxShadow: "0 4px 12px rgba(0,0,0,0.15)",
            animation: "pluginPulse 2s ease-in-out infinite",
          }}
        >
          <span
            style={{
              display: "inline-block",
              width: "8px",
              height: "8px",
              borderRadius: "50%",
              background: "#93c5fd",
              animation: "pluginBlink 1s ease-in-out infinite",
            }}
          />
          Thinking{dots}
          <style>{`
            @keyframes pluginPulse {
              0%, 100% { transform: scale(1); opacity: 1; }
              50% { transform: scale(1.03); opacity: 0.9; }
            }
            @keyframes pluginBlink {
              0%, 100% { opacity: 1; }
              50% { opacity: 0.3; }
            }
          `}</style>
        </div>
      );
    }

    return null;
  },
};

export default ThinkingAnimationPlugin;
