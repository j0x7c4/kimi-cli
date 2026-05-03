import {
  type ReactElement,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Trans, useTranslation } from "react-i18next";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Command,
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Switch } from "@/components/ui/switch";
import { ChevronDown, FolderOpen, Home, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

const HOME_DIR_REGEX = /^(\/Users\/[^/]+|\/home\/[^/]+)/;
const TRAILING_SLASH_REGEX = /\/$/;

type CreateSessionDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: (workDir: string, createDir?: boolean, thinking?: boolean) => Promise<void>;
  fetchWorkDirs: () => Promise<string[]>;
  fetchStartupDir: () => Promise<string>;
};

/**
 * Format a path for display:
 * - Replace home directory with ~
 * - For long paths, show ~/.../<last-two-segments>
 */
function formatPathForDisplay(path: string, maxSegments = 3): string {
  const homeMatch = path.match(HOME_DIR_REGEX);
  let displayPath = path;

  if (homeMatch) {
    displayPath = `~${path.slice(homeMatch[1].length)}`;
  }

  const segments = displayPath.split("/").filter(Boolean);

  if (segments.length <= maxSegments) {
    return displayPath.startsWith("~")
      ? displayPath
      : `/${segments.join("/")}`;
  }

  const prefix = displayPath.startsWith("~") ? "~" : "";
  const lastSegments = segments.slice(-2).join("/");
  return `${prefix}/.../${lastSegments}`;
}

// Module-level cache for work dirs (stale-while-revalidate)
let cachedWorkDirs: string[] | null = null;

export function CreateSessionDialog({
  open,
  onOpenChange,
  onConfirm,
  fetchWorkDirs,
  fetchStartupDir,
}: CreateSessionDialogProps): ReactElement {
  const [workDirs, setWorkDirs] = useState<string[]>(
    () => cachedWorkDirs ?? [],
  );
  const [inputValue, setInputValue] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [showConfirmCreate, setShowConfirmCreate] = useState(false);
  const [pendingPath, setPendingPath] = useState("");
  const [startupDir, setStartupDir] = useState("");
  const [commandValue, setCommandValue] = useState("");
  const [thinking, setThinking] = useState(true);
  const [isAdvancedOpen, setIsAdvancedOpen] = useState(false);
  const isCreatingRef = useRef(false);
  const commandListRef = useRef<HTMLDivElement>(null);
  const { t } = useTranslation(["sessions", "common"]);

  // Fetch startup dir and work dirs independently for progressive loading
  useEffect(() => {
    if (!open) {
      return;
    }

    // Initialize from cache if available, still refresh in background
    if (cachedWorkDirs) {
      setWorkDirs(cachedWorkDirs);
    } else {
      setIsLoading(true);
    }

    // Startup dir resolves fast — show it immediately and highlight it
    fetchStartupDir()
      .then((startup) => {
        if (startup) {
          setStartupDir(startup);
          setCommandValue(startup);
        }
      })
      .catch(() => {
        // Startup dir is optional for this dialog; ignore failures.
      });

    // Work dirs may take longer — update cache when done
    fetchWorkDirs()
      .then((dirs) => {
        cachedWorkDirs = dirs;
        setWorkDirs(dirs);
      })
      .catch((error) => {
        console.error("Failed to fetch directories:", error);
      })
      .finally(() => {
        setIsLoading(false);
      });
  }, [open, fetchWorkDirs, fetchStartupDir]);

  // Reset component state when dialog closes (cache persists at module level)
  useEffect(() => {
    if (!open) {
      setInputValue("");
      setCommandValue("");
      setWorkDirs(cachedWorkDirs ?? []);
      setIsCreating(false);
      setShowConfirmCreate(false);
      setPendingPath("");
      setStartupDir("");
      isCreatingRef.current = false;
      setThinking(true);
      setIsAdvancedOpen(false);
    }
  }, [open]);

  const handleSelect = useCallback(
    async (dir: string) => {
      if (isCreatingRef.current) return;
      isCreatingRef.current = true;
      setIsCreating(true);
      try {
        await onConfirm(dir, undefined, thinking);
        onOpenChange(false);
      } catch (err) {
        if (
          err instanceof Error &&
          "isDirectoryNotFound" in err &&
          (err as Error & { isDirectoryNotFound: boolean }).isDirectoryNotFound
        ) {
          setPendingPath(dir);
          setShowConfirmCreate(true);
        }
      } finally {
        setIsCreating(false);
        isCreatingRef.current = false;
      }
    },
    [onConfirm, onOpenChange, thinking],
  );

  const handleInputSubmit = useCallback(() => {
    const trimmed = inputValue.trim();
    if (!trimmed || isCreatingRef.current) return;
    handleSelect(trimmed);
  }, [inputValue, handleSelect]);

  const handleConfirmCreateDir = useCallback(async () => {
    if (!pendingPath) {
      return;
    }

    setShowConfirmCreate(false);
    setIsCreating(true);
    isCreatingRef.current = true;
    try {
      await onConfirm(pendingPath, true, thinking);
      onOpenChange(false);
    } catch (err) {
      console.error("Failed to create directory:", err);
    } finally {
      setIsCreating(false);
      isCreatingRef.current = false;
      setPendingPath("");
    }
  }, [pendingPath, onConfirm, onOpenChange, thinking]);

  const handleCancelCreateDir = useCallback(() => {
    setShowConfirmCreate(false);
    setPendingPath("");
  }, []);

  // Tab completion: fill input with first matching item's value
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key !== "Tab" || !commandListRef.current) return;

      // Find the currently selected (highlighted) item
      const selectedItem = commandListRef.current.querySelector<HTMLElement>(
        "[cmdk-item][data-selected=true]",
      );
      if (!selectedItem) return;

      const value = selectedItem.getAttribute("data-value");
      if (!value || value.startsWith("__custom__")) return;

      e.preventDefault();
      setInputValue(value);
    },
    [],
  );

  // Check if the current input matches any existing work dir
  const trimmedInput = inputValue.trim();
  const inputMatchesExisting =
    trimmedInput !== "" &&
    workDirs.some(
      (dir) =>
        dir === trimmedInput ||
        dir === trimmedInput.replace(TRAILING_SLASH_REGEX, ""),
    );

  const showCustomPathOption = trimmedInput !== "" && !inputMatchesExisting;

  // Recent dirs = workDirs excluding startupDir
  const recentDirs = useMemo(
    () => (startupDir ? workDirs.filter((d) => d !== startupDir) : workDirs),
    [workDirs, startupDir],
  );

  return (
    <>
      <CommandDialog
        open={open}
        onOpenChange={onOpenChange}
        title={t("sessions:create.title")}
        description={t("sessions:create.description")}
        showCloseButton={false}
      >
        <Command value={commandValue} onValueChange={setCommandValue}>
          <CommandInput
            placeholder={t("sessions:create.placeholder")}
            value={inputValue}
            onValueChange={setInputValue}
            onKeyDown={handleKeyDown}
          />
          <CommandList ref={commandListRef}>
            <CommandEmpty>
              {trimmedInput
                ? t("sessions:create.emptyNoMatch")
                : isLoading
                  ? t("sessions:create.emptyLoading")
                  : t("sessions:create.emptyTypePath")}
            </CommandEmpty>

            {showCustomPathOption && (
              <>
                <CommandGroup heading={t("sessions:create.groupCustom")}>
                  <CommandItem
                    className="group"
                    value={`__custom__${trimmedInput}`}
                    onSelect={handleInputSubmit}
                    disabled={isCreating}
                  >
                    {isCreating ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <FolderOpen />
                    )}
                    <span className="flex-1 truncate">{trimmedInput}</span>
                    <kbd className="pointer-events-none ml-auto hidden select-none rounded border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground group-data-[selected=true]:inline-flex">
                      ↵
                    </kbd>
                  </CommandItem>
                </CommandGroup>
                {(startupDir || recentDirs.length > 0 || isLoading) && (
                  <CommandSeparator />
                )}
              </>
            )}

            {startupDir && (
              <>
                <CommandGroup heading={t("sessions:create.groupCurrent")}>
                  <CommandItem
                    className="group"
                    value={startupDir}
                    onSelect={() => handleSelect(startupDir)}
                    disabled={isCreating}
                  >
                    <Home />
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span className="truncate">
                          {formatPathForDisplay(startupDir, 3)}
                        </span>
                      </TooltipTrigger>
                      <TooltipContent side="right">
                        {startupDir}
                      </TooltipContent>
                    </Tooltip>
                    <kbd className="pointer-events-none ml-auto hidden select-none rounded border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground group-data-[selected=true]:inline-flex">
                      ↵
                    </kbd>
                  </CommandItem>
                </CommandGroup>
                {(recentDirs.length > 0 || isLoading) && <CommandSeparator />}
              </>
            )}

            {recentDirs.length > 0 && (
              <CommandGroup heading={t("sessions:create.groupRecent")}>
                {recentDirs.map((dir) => (
                  <CommandItem
                    className="group"
                    key={dir}
                    value={dir}
                    onSelect={() => handleSelect(dir)}
                    disabled={isCreating}
                  >
                    <FolderOpen />
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span className="truncate">
                          {formatPathForDisplay(dir, 3)}
                        </span>
                      </TooltipTrigger>
                      <TooltipContent side="right">{dir}</TooltipContent>
                    </Tooltip>
                    <kbd className="pointer-events-none ml-auto hidden select-none rounded border bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground group-data-[selected=true]:inline-flex">
                      ↵
                    </kbd>
                  </CommandItem>
                ))}
              </CommandGroup>
            )}

            {isLoading && (
              <div className="flex items-center justify-center py-4">
                <Loader2 className="size-4 animate-spin text-muted-foreground" />
              </div>
            )}
          </CommandList>
          <div className="border-t px-2 py-1.5">
            <Collapsible open={isAdvancedOpen} onOpenChange={setIsAdvancedOpen}>
              <CollapsibleTrigger className="flex w-full items-center justify-between rounded-sm px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-accent-foreground">
                <span>{t("sessions:advanced.title")}</span>
                <ChevronDown className={cn("size-3 transition-transform", isAdvancedOpen && "rotate-180")} />
              </CollapsibleTrigger>
              <CollapsibleContent>
                <div className="flex items-center justify-between px-2 py-2">
                  <span className="text-xs text-muted-foreground">{t("sessions:advanced.thinkingMode")}</span>
                  <Switch
                    checked={thinking}
                    onCheckedChange={setThinking}
                    aria-label={t("sessions:advanced.enableThinking")}
                  />
                </div>
              </CollapsibleContent>
            </Collapsible>
          </div>
        </Command>
      </CommandDialog>

      <AlertDialog open={showConfirmCreate} onOpenChange={setShowConfirmCreate}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("sessions:createDir.title")}</AlertDialogTitle>
            <AlertDialogDescription>
              <Trans
                i18nKey="sessions:createDir.body"
                values={{ path: pendingPath }}
                components={{
                  pathCode: (
                    <code className="bg-muted px-1 py-0.5 rounded text-foreground break-all" />
                  ),
                }}
              />
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={handleCancelCreateDir}>
              {t("common:actions.cancel")}
            </AlertDialogCancel>
            <AlertDialogAction onClick={handleConfirmCreateDir}>
              {t("sessions:createDir.create")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
