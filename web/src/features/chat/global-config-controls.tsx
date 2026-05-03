import { useCallback, useState, type ReactElement } from "react";
import { useTranslation } from "react-i18next";
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
  const { t } = useTranslation(["toasts", "config", "chat"]);

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
          toast.success(t("toasts:globalModel.successTitle"), {
            description: t("toasts:globalModel.successDesc", {
              count: restarted.length,
            }),
          });
        } else {
          toast.success(t("toasts:globalModel.successTitle"));
        }

        if (skippedBusy.length > 0) {
          setLastBusySkip(skippedBusy);
          toast.message(t("toasts:globalModel.busyTitle"), {
            description: t("toasts:globalModel.busyDesc", {
              count: skippedBusy.length,
            }),
          });
        } else {
          setLastBusySkip(null);
        }
      } catch (err) {
        const message =
          err instanceof Error ? err.message : t("toasts:globalModel.fallbackError");
        toast.error(t("toasts:globalModel.errorTitle"), { description: message });
      }
    },
    [config, update, t],
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

      toast.success(t("toasts:restartBusy.successTitle"), {
        description:
          restarted.length > 0
            ? t("toasts:restartBusy.successDesc", { count: restarted.length })
            : t("toasts:restartBusy.successDescNone"),
      });
    } catch (err) {
      const message =
        err instanceof Error ? err.message : t("toasts:restartBusy.fallbackError");
      toast.error(t("toasts:restartBusy.errorTitle"), { description: message });
    }
  }, [lastBusySkip, update, t]);

  const attachments = usePromptInputAttachments();

  return (
    <div className={cn("flex items-center gap-1", className)}>
      <Button
        variant="ghost"
        size="icon"
        className="size-9 border-0"
        aria-label={t("chat:attachFiles")}
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
            aria-label={t("config:model.changeAria")}
            type="button"
            disabled={isLoading || isUpdating || !config}
          >
            <Cpu className="size-4 shrink-0" />
            <span className="truncate">
              {config ? config.defaultModel : t("config:model.fallback")}
            </span>
            {(isLoading || isUpdating) && (
              <Loader className="ml-auto shrink-0" size={14} />
            )}
          </Button>
        </ModelSelectorTrigger>
        <ModelSelectorContent title={t("config:model.title")}>
          <ModelSelectorInput placeholder={t("config:model.placeholder")} />
          <ModelSelectorList>
            <ModelSelectorEmpty>{t("config:model.empty")}</ModelSelectorEmpty>
            <ModelSelectorGroup heading={t("config:model.heading")}>
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
                  {t("chat:planMode.label")}
                </span>
                <Switch
                  aria-label={t("chat:planMode.toggle")}
                  checked={planMode}
                  onCheckedChange={onPlanModeChange}
                />
              </div>
            </TooltipTrigger>
            <TooltipContent sideOffset={8}>
              {planMode
                ? t("chat:planMode.active")
                : t("chat:planMode.enable")}
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
          aria-label={t("config:model.forceRestart")}
          title={t("config:model.forceRestart")}
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
          aria-label={t("config:model.reload")}
          title={t("config:model.reload")}
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
