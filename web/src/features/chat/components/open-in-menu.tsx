import { useCallback, useMemo, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  ChevronDownIcon,
  CopyIcon,
  FolderOpenIcon,
  CodeIcon,
  SquareTerminalIcon,
  TerminalIcon,
  AppWindowIcon,
  ChevronUpIcon,
} from "lucide-react";
import { toast } from "sonner";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { ButtonGroup, ButtonGroupText } from "@/components/ui/button-group";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { isMacOS } from "@/hooks/utils";
import { cn } from "@/lib/utils";
import {
  type OpenTargetDef,
  ALL_OPEN_TARGETS,
  openViaBackend,
  setLastOpenTargetId,
  useLastOpenTargetId,
} from "@/features/chat/open-in-shared";

type OpenInMenuProps = {
  workDir?: string | null;
  className?: string;
};

type OpenTarget = OpenTargetDef & {
  icon: ReactNode;
};

const TRAILING_SLASH_REGEX = /\/+$/;

function normalizePath(path: string): string {
  const trimmed = path.trim().replace(/\\/g, "/");
  if (trimmed === "") {
    return "/";
  }
  const cleaned = trimmed.replace(TRAILING_SLASH_REGEX, "");
  return cleaned === "" ? "/" : cleaned;
}

function compactPath(path: string, maxLength = 22): string {
  const normalized = normalizePath(path);
  if (normalized.length <= maxLength) {
    return normalized;
  }
  const parts = normalized.split("/").filter(Boolean);
  if (parts.length === 0) {
    return normalized.slice(0, maxLength - 1) + "…";
  }
  const tail = parts.slice(-2).join("/");
  if (tail.length + 2 <= maxLength) {
    return `…/${tail}`;
  }
  return `…/${tail.slice(-maxLength + 2)}`;
}

const ICON_MAP: Record<string, ReactNode> = {
  finder: <FolderOpenIcon className="size-4" />,
  cursor: <AppWindowIcon className="size-4" />,
  vscode: <CodeIcon className="size-4" />,
  antigravity: <ChevronUpIcon className="size-4" />,
  iterm: <TerminalIcon className="size-4" />,
  terminal: <SquareTerminalIcon className="size-4" />,
};

export function OpenInMenu({ workDir, className }: OpenInMenuProps) {
  const { t } = useTranslation("chat");
  const isMac = isMacOS();
  const hasWorkDir = Boolean(workDir && workDir.trim().length > 0);
  const displayPath = workDir ? compactPath(workDir) : t("openIn.noDirectory");

  const menuTargets = useMemo<OpenTarget[]>(
    () =>
      ALL_OPEN_TARGETS.filter((t) => !t.macOnly || isMac).map((t) => ({
        ...t,
        icon: ICON_MAP[t.id],
      })),
    [isMac],
  );

  const lastTargetId = useLastOpenTargetId();

  const lastTarget = useMemo(
    () => menuTargets.find((t) => t.id === lastTargetId) ?? null,
    [menuTargets, lastTargetId],
  );

  const handleCopyPath = useCallback(async () => {
    if (!workDir) {
      return;
    }
    try {
      await navigator.clipboard.writeText(workDir);
      toast.success(t("openIn.pathCopied"), { description: workDir });
    } catch (error) {
      console.error("Failed to copy path:", error);
      toast.error(t("openIn.copyFailed"));
    }
  }, [workDir, t]);

  const handleOpenTarget = useCallback(
    async (target: OpenTarget) => {
      if (!workDir) {
        toast.message(t("openIn.noWorkDir"), {
          description: t("openIn.noWorkDirDesc"),
        });
        return;
      }
      try {
        await openViaBackend(target.backendApp, workDir);
        setLastOpenTargetId(target.id);
      } catch (error) {
        console.error("Failed to open external URL:", error);
        toast.error(t("openIn.openFailed"), {
          description:
            error instanceof Error
              ? error.message
              : t("openIn.unexpectedError"),
        });
      }
    },
    [workDir, t],
  );

  if (!isMac) {
    return null;
  }

  return (
    <ButtonGroup
      className={cn("h-8 items-center", className)}
      aria-label={t("openIn.openWorkDir")}
    >
      <Tooltip>
        <TooltipTrigger asChild>
          <ButtonGroupText
            className={cn(
              "h-8 max-w-[220px] px-3 text-xs font-semibold",
              "bg-secondary/40 text-foreground",
              !hasWorkDir && "text-muted-foreground",
            )}
          >
            <TerminalIcon className="size-3.5" />
            <span className="truncate">{displayPath}</span>
          </ButtonGroupText>
        </TooltipTrigger>
        {workDir ? (
          <TooltipContent side="bottom" className="max-w-md break-all">
            {workDir}
          </TooltipContent>
        ) : null}
      </Tooltip>
      {lastTarget ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!hasWorkDir}
              className="h-8 px-2 text-xs"
              aria-label={t("openIn.openInLabel", { label: lastTarget.label })}
              onClick={() => handleOpenTarget(lastTarget)}
            >
              {t("openIn.open")}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">
            {t("openIn.openInLabel", { label: lastTarget.label })}
          </TooltipContent>
        </Tooltip>
      ) : null}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!hasWorkDir}
            className="h-8 px-2 text-xs"
            aria-label={t("openIn.chooseApp")}
          >
            {!lastTarget && t("openIn.open")}
            <ChevronDownIcon className="size-3" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-56">
          {menuTargets.map((target) => (
            <DropdownMenuItem
              key={target.id}
              onSelect={() => handleOpenTarget(target)}
              aria-label={
                target.id === lastTargetId
                  ? t("openIn.openInLabelLastUsed", { label: target.label })
                  : target.label
              }
            >
              {target.icon}
              <span>{target.label}</span>
              {target.id === lastTargetId && (
                <span className="ml-auto text-[10px] text-muted-foreground">
                  {t("openIn.lastUsed")}
                </span>
              )}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={handleCopyPath}>
            <CopyIcon className="size-4" />
            <span>{t("openIn.copyPath")}</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </ButtonGroup>
  );
}
