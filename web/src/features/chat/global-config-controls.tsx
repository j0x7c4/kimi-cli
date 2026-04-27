import { useCallback, useState, type ReactElement } from "react";
import { toast } from "sonner";
import { Check, Cpu, Paperclip, RefreshCcw } from "lucide-react";
import { usePromptInputAttachments } from "@ai-elements";
import { useGlobalConfig } from "@/hooks/useGlobalConfig";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Loader } from "@/components/ai-elements/loader";
import {
  ModelSelector,
  ModelSelectorContent,
  ModelSelectorEmpty,
  ModelSelectorGroup,
  ModelSelectorInput,
  ModelSelectorItem,
  ModelSelectorList,
  ModelSelectorName,
  ModelSelectorTrigger,
} from "@/components/ai-elements/model-selector";
import { cn } from "@/lib/utils";

export type GlobalConfigControlsProps = {
  className?: string;
  planMode?: boolean;
  onPlanModeChange?: (enabled: boolean) => void;
};

export function GlobalConfigControls({
  className,
  planMode = false,
  onPlanModeChange,
}: GlobalConfigControlsProps): ReactElement {
  const { config, isLoading, isUpdating, error, refresh, update } =
    useGlobalConfig();

  const [isSelectorOpen, setIsSelectorOpen] = useState(false);
  const [lastBusySkip, setLastBusySkip] = useState<string[] | null>(null);

  const handleSelectModel = useCallback(
    async (modelKey: string) => {
      setIsSelectorOpen(false);
      if (!config || modelKey === config.defaultModel) {
        return;
      }

      try {
        const resp = await update({ defaultModel: modelKey });
        const restarted = resp.restartedSessionIds ?? [];
        const skippedBusy = resp.skippedBusySessionIds ?? [];

        if (restarted.length > 0) {
          toast.success("Global model updated", {
            description: `Restarted ${restarted.length} running session(s).`,
          });
        } else {
          toast.success("Global model updated");
        }

        if (skippedBusy.length > 0) {
          setLastBusySkip(skippedBusy);
          toast.message("Some sessions were skipped (busy)", {
            description: `Skipped ${skippedBusy.length} busy session(s). You can retry when they are idle, or force restart.`,
          });
        } else {
          setLastBusySkip(null);
        }
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Failed to update global model";
        toast.error("Failed to update global model", { description: message });
      }
    },
    [config, update],
  );

  const handleForceRestartBusy = useCallback(async () => {
    if (!lastBusySkip || lastBusySkip.length === 0) {
      return;
    }
    try {
      const resp = await update({ forceRestartBusySessions: true });
      const restarted = resp.restartedSessionIds ?? [];
      const skippedBusy = resp.skippedBusySessionIds ?? [];

      if (skippedBusy.length === 0) {
        setLastBusySkip(null);
      } else {
        setLastBusySkip(skippedBusy);
      }

      toast.success("Restarted running sessions", {
        description:
          restarted.length > 0
            ? `Restarted ${restarted.length} session(s).`
            : "No running sessions to restart.",
      });
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "Failed to restart busy sessions";
      toast.error("Failed to restart busy sessions", { description: message });
    }
  }, [lastBusySkip, update]);

  const attachments = usePromptInputAttachments();

  return (
    <div className={cn("flex items-center gap-1", className)}>
      <Button
        variant="ghost"
        size="icon"
        className="size-9 border-0"
        aria-label="Attach files"
        type="button"
        onClick={() => attachments.openFileDialog()}
      >
        <Paperclip className="size-4" />
      </Button>

      <div className="mx-0 h-4 w-px bg-border/70" />

      <ModelSelector open={isSelectorOpen} onOpenChange={setIsSelectorOpen}>
        <ModelSelectorTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            className="h-9 max-w-[160px] justify-start gap-2 border-0"
            aria-label="Change global model"
            type="button"
            disabled={isLoading || isUpdating || !config}
          >
            <Cpu className="size-4 shrink-0" />
            <span className="truncate">
              {config ? config.defaultModel : "Model"}
            </span>
            {(isLoading || isUpdating) && (
              <Loader className="ml-auto shrink-0" size={14} />
            )}
          </Button>
        </ModelSelectorTrigger>
        <ModelSelectorContent title="Select global model">
          <ModelSelectorInput placeholder="Search models..." />
          <ModelSelectorList>
            <ModelSelectorEmpty>No models found.</ModelSelectorEmpty>
            <ModelSelectorGroup heading="Models">
              {(config?.models ?? []).map((m) => {
                const isSelected = m.name === config?.defaultModel;
                const label = `${m.name} (${m.provider})`;
                return (
                  <ModelSelectorItem
                    key={m.name}
                    value={`${m.name} ${m.model} ${m.provider}`}
                    onSelect={(_value) => handleSelectModel(m.name)}
                    className="flex items-center gap-2"
                  >
                    {isSelected ? (
                      <Check className="size-4 text-foreground" />
                    ) : (
                      <span className="size-4" />
                    )}
                    <ModelSelectorName title={label}>
                      {m.name}
                    </ModelSelectorName>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {m.provider}
                    </span>
                  </ModelSelectorItem>
                );
              })}
            </ModelSelectorGroup>
          </ModelSelectorList>
        </ModelSelectorContent>
      </ModelSelector>

      {onPlanModeChange && (
        <>
          <div className="mx-0 h-4 w-px bg-border/70" />
          <Tooltip>
            <TooltipTrigger asChild>
              <div className="flex h-9 items-center gap-2 rounded-md px-2">
                <span className="text-xs text-muted-foreground">
                  Plan
                </span>
                <Switch
                  aria-label="Toggle plan mode"
                  checked={planMode}
                  onCheckedChange={onPlanModeChange}
                />
              </div>
            </TooltipTrigger>
            <TooltipContent sideOffset={8}>
              {planMode
                ? "Plan mode is active. The model will only read and plan, not modify files."
                : "Enable plan mode for read-only research and planning."}
            </TooltipContent>
          </Tooltip>
        </>
      )}

      {(lastBusySkip && lastBusySkip.length > 0) || error ? (
        <div className="mx-1.5 h-4 w-px bg-border/70" />
      ) : null}

      {lastBusySkip && lastBusySkip.length > 0 ? (
        <Button
          variant="outline"
          size="icon"
          className="size-9"
          aria-label="Force restart busy sessions"
          title="Force restart busy sessions"
          type="button"
          onClick={handleForceRestartBusy}
          disabled={isUpdating}
        >
          <RefreshCcw className="size-4" />
        </Button>
      ) : null}

      {error ? (
        <Button
          variant="outline"
          size="icon"
          className="size-9"
          aria-label="Reload global config"
          title="Reload global config"
          type="button"
          onClick={() => {
            refresh();
          }}
        >
          <RefreshCcw className="size-4" />
        </Button>
      ) : null}
    </div>
  );
}
