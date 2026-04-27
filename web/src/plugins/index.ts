// biome-ignore lint/performance/noBarrelFile: intentional public API barrel
export { PluginSystemProvider, usePluginBus, usePluginRegistry, usePluginSystem } from "./PluginSystemProvider";
export { usePluginEventEmitter } from "./usePluginEventEmitter";
export type { WireEventContext } from "./usePluginEventEmitter";
export type { UIPlugin, PluginEvent, PluginEventType, PluginRegistry } from "./types";
