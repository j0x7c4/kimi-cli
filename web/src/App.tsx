import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChatStatus } from "ai";
import { useTranslation } from "react-i18next";
import { PromptInputProvider } from "@ai-elements";
import { toast } from "sonner";
import { PanelLeftOpen, PanelLeftClose, LogOut, ShieldCheck } from "lucide-react";
import { cn } from "./lib/utils";
import { ResizablePanel, ResizablePanelGroup } from "./components/ui/resizable";
import { ChatWorkspaceContainer } from "./features/chat/chat-workspace-container";
import { SessionsSidebar } from "./features/sessions/sessions";
import type { ArchiveState } from "./features/sessions/sessions";
import { CreateSessionDialog } from "./features/sessions/create-session-dialog";
import { Toaster } from "./components/ui/sonner";
import { formatRelativeTime } from "./hooks/utils";
import { useSessions } from "./hooks/useSessions";
import {
  archiveSessionMemory,
  useMemoryEvents,
  useRecentSummaries,
} from "./hooks/useMemory";
import type { MemoryEvent } from "./hooks/useMemory";
import { useTheme } from "./hooks/use-theme";
import { ThemeToggle } from "./components/ui/theme-toggle";
import { LanguageToggle } from "./components/ui/language-toggle";
import type { SessionStatus } from "./lib/api/models";
import type { PanelSize, PanelImperativeHandle } from "react-resizable-panels";
import { consumeAuthTokenFromUrl, setAuthToken } from "./lib/auth";
import { useAuth } from "./hooks/useAuth";
import { LoginPage } from "./features/auth/login-page";
import { AdminPage } from "./features/admin/admin-page";
import { BrandingProvider, useBranding } from "./hooks/useBranding";

/**
 * Get session ID from URL search params
 */
function getSessionIdFromUrl(): string | null {
  const params = new URLSearchParams(window.location.search);
  return params.get("session");
}

/**
 * Update URL with session ID without triggering page reload
 */
function updateUrlWithSession(sessionId: string | null): void {
  const url = new URL(window.location.href);
  if (sessionId) {
    url.searchParams.set("session", sessionId);
  } else {
    url.searchParams.delete("session");
  }
  window.history.replaceState({}, "", url.toString());
}

const SIDEBAR_COLLAPSED_SIZE = 48;
const SIDEBAR_MIN_SIZE = 200;
const SIDEBAR_DEFAULT_SIZE = 260;
const SIDEBAR_ANIMATION_MS = 250;

function App() {
  // Initialize theme on app startup
  useTheme();
  const { t } = useTranslation(["toasts", "common"]);

  // Branding context
  const { config: brandingConfig } = useBranding();
  const collapsedLogoSrc = brandingConfig?.logo ?? "/logo.png";
  const collapsedLogoUrl = brandingConfig?.logo_url ?? "https://www.kimi.com/code";

  // Auth state
  const { currentUser, isLoading: isAuthLoading, isAdmin, login, logout } = useAuth();

  // Route: /admin or /admin/ -> render admin panel
  // biome-ignore lint/performance/useTopLevelRegex: inline regex is fine here; this hook runs rarely
  const isAdminRoute = window.location.pathname.replace(/\/$/, "") === "/admin";

  // Handle logout
  const handleLogout = useCallback(async () => {
    try {
      await logout();
    } catch {
      // ignore errors, state is cleared anyway
    }
  }, [logout]);

  const sidebarElementRef = useRef<HTMLDivElement | null>(null);
  const sidebarPanelRef = useRef<PanelImperativeHandle | null>(null);
  const sessionsHook = useSessions();
  const [isMobileSidebarOpen, setIsMobileSidebarOpen] = useState(false);
  const [isDesktop, setIsDesktop] = useState(() => {
    if (typeof window === "undefined") {
      return true;
    }
    return window.matchMedia("(min-width: 1024px)").matches;
  });

  const {
    sessions,
    archivedSessions,
    selectedSessionId,
    createSession,
    deleteSession,
    selectSession,
    uploadSessionFile,
    getSessionFile,
    getSessionFileUrl,
    listSessionDirectory,
    refreshSession,
    refreshSessions,
    refreshArchivedSessions,
    loadMoreSessions,
    loadMoreArchivedSessions,
    hasMoreSessions,
    hasMoreArchivedSessions,
    isLoadingMore,
    isLoadingMoreArchived,
    isLoadingArchived,
    searchQuery,
    setSearchQuery,
    applySessionStatus,
    fetchWorkDirs,
    fetchStartupDir,
    renameSession,
    generateTitle,
    archiveSession,
    unarchiveSession,
    bulkArchiveSessions,
    bulkUnarchiveSessions,
    bulkDeleteSessions,
    forkSession,
    error: sessionsError,
  } = sessionsHook;

  const currentSession = useMemo(
    () => sessions.find((session) => session.sessionId === selectedSessionId),
    [sessions, selectedSessionId],
  );

  const [streamStatus, setStreamStatus] = useState<ChatStatus>("ready");

  useEffect(() => {
    const token = consumeAuthTokenFromUrl();
    if (token) {
      setAuthToken(token);
    }
  }, []);

  // Create session dialog state (lifted to App for unified access)
  const [showCreateDialog, setShowCreateDialog] = useState(false);

  // Auto-open create dialog or create session directly from URL params
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const action = params.get("action");
    if (action === "create") {
      setShowCreateDialog(true);
    } else if (action === "create-in-dir") {
      const workDir = params.get("workDir");
      if (!workDir) return; // invalid params, ignore silently
      createSession(workDir).catch(() => {
        // Errors are already handled globally via sessionsError → toast
      });
    } else {
      return;
    }
    params.delete("action");
    params.delete("workDir");
    const url = new URL(window.location.href);
    url.search = params.toString();
    window.history.replaceState({}, "", url.toString());
  }, [createSession]);

  const handleOpenCreateDialog = useCallback(() => {
    setShowCreateDialog(true);
    setIsMobileSidebarOpen(false);
  }, []);

  const handleOpenMobileSidebar = useCallback(() => {
    setIsMobileSidebarOpen(true);
  }, []);

  const handleCloseMobileSidebar = useCallback(() => {
    setIsMobileSidebarOpen(false);
  }, []);

  // Sidebar state
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [isSidebarAnimating, setIsSidebarAnimating] = useState(false);
  const handleCollapseSidebar = useCallback(() => {
    setIsSidebarAnimating(true);
    sidebarPanelRef.current?.collapse();
  }, []);
  const handleExpandSidebar = useCallback(() => {
    setIsSidebarAnimating(true);
    sidebarPanelRef.current?.expand();
  }, []);
  const handleSidebarResize = useCallback((panelSize: PanelSize) => {
    const collapsed = panelSize.inPixels <= SIDEBAR_COLLAPSED_SIZE + 1;
    setIsSidebarCollapsed((prev) => (prev === collapsed ? prev : collapsed));
  }, []);

  useEffect(() => {
    if (!isSidebarAnimating) {
      return;
    }
    const timer = window.setTimeout(() => {
      setIsSidebarAnimating(false);
    }, SIDEBAR_ANIMATION_MS);
    return () => window.clearTimeout(timer);
  }, [isSidebarAnimating]);

  useEffect(() => {
    const current = sidebarPanelRef.current;
    if (!current) {
      return;
    }
    setIsSidebarCollapsed(current.isCollapsed());
  }, []);

  useEffect(() => {
    const element = sidebarElementRef.current;
    if (!element) {
      return;
    }
    if (isSidebarAnimating) {
      element.style.transition = `flex-basis ${SIDEBAR_ANIMATION_MS}ms ease-in-out`;
      return;
    }
    element.style.transition = "";
  }, [isSidebarAnimating]);

  // Track layout breakpoint and close mobile sidebar when switching to desktop
  useEffect(() => {
    const mediaQuery = window.matchMedia("(min-width: 1024px)");
    const handleChange = () => {
      const matches = mediaQuery.matches;
      setIsDesktop(matches);
      if (matches) setIsMobileSidebarOpen(false);
    };
    handleChange();
    mediaQuery.addEventListener("change", handleChange);
    return () => mediaQuery.removeEventListener("change", handleChange);
  }, []);

  // Track if we've restored session from URL
  const hasRestoredFromUrlRef = useRef(false);

  // Eagerly restore session from URL - don't wait for session list to load
  // This allows session content to load in parallel with the session list
  useEffect(() => {
    if (hasRestoredFromUrlRef.current) {
      return;
    }

    const urlSessionId = getSessionIdFromUrl();
    if (urlSessionId) {
      console.log("[App] Eagerly restoring session from URL:", urlSessionId);
      selectSession(urlSessionId);
    }
    hasRestoredFromUrlRef.current = true;
  }, [selectSession]);

  // Validate session exists once session list loads, clear URL if not found
  useEffect(() => {
    if (sessions.length === 0 || !selectedSessionId) {
      return;
    }

    if (searchQuery.trim() || hasMoreSessions) {
      return;
    }

    const sessionExists = sessions.some(
      (s) => s.sessionId === selectedSessionId,
    );
    if (!sessionExists) {
      console.log("[App] Session from URL not found, clearing selection");
      updateUrlWithSession(null);
      selectSession("");
    }
  }, [sessions, selectedSessionId, selectSession, hasMoreSessions, searchQuery]);

  // Update URL when selected session changes
  useEffect(() => {
    // Skip the initial render before URL restoration
    if (!hasRestoredFromUrlRef.current) {
      return;
    }
    updateUrlWithSession(selectedSessionId || null);
  }, [selectedSessionId]);

  // Show toast notifications for errors
  useEffect(() => {
    if (sessionsError) {
      toast.error(t("toasts:session.errorTitle"), {
        description: sessionsError,
      });
    }
  }, [sessionsError, t]);

  const handleStreamStatusChange = useCallback((nextStatus: ChatStatus) => {
    setStreamStatus(nextStatus);
  }, []);

  const handleSessionStatus = useCallback(
    (status: SessionStatus) => {
      applySessionStatus(status);

      if (status.state !== "idle") {
        return;
      }

      const reason = status.reason ?? "";

      if (reason === "config_update") {
        console.log("[App] Config update detected, refreshing global config");
        window.dispatchEvent(new Event("kimi:config-update"));
      }

      if (!reason.startsWith("prompt_")) {
        return;
      }

      console.log(
        "[App] Prompt complete, refreshing session info:",
        status.sessionId,
      );
      refreshSession(status.sessionId);
    },
    [applySessionStatus, refreshSession],
  );

  const handleCreateSession = useCallback(
    async (workDir: string, createDir?: boolean, thinking?: boolean) => {
      await createSession(workDir, createDir, thinking);
    },
    [createSession],
  );

  const handleCreateSessionInDir = useCallback(
    async (workDir: string) => {
      await createSession(workDir);
    },
    [createSession],
  );

  const handleDeleteSession = useCallback(
    async (sessionId: string) => {
      await deleteSession(sessionId);
    },
    [deleteSession],
  );

  const handleSelectSession = useCallback(
    (sessionId: string) => {
      selectSession(sessionId);
      setIsMobileSidebarOpen(false);
    },
    [selectSession],
  );

  const handleRefreshSessions = useCallback(async () => {
    await refreshSessions();
  }, [refreshSessions]);

  const handleSearchQueryChange = useCallback(
    (query: string) => {
      setSearchQuery(query);
    },
    [setSearchQuery],
  );

  // ----- memory archive state -----

  const { items: recentSummaries, refresh: refreshRecentSummaries } =
    useRecentSummaries(200);

  // session_id -> max(created_at) (unix seconds)
  const lastArchivedBySession = useMemo(() => {
    const map = new Map<string, number>();
    for (const s of recentSummaries) {
      const prev = map.get(s.session_id);
      if (prev === undefined || s.created_at > prev) {
        map.set(s.session_id, s.created_at);
      }
    }
    return map;
  }, [recentSummaries]);

  const [archiveInFlight, setArchiveInFlight] = useState<Set<string>>(
    () => new Set(),
  );
  const [archiveErrors, setArchiveErrors] = useState<Map<string, string>>(
    () => new Map(),
  );
  // session_id -> startedAt (ms epoch); for the 90s safety timeout
  const archiveStartedAtRef = useRef<Map<string, number>>(new Map());

  // SSE: completion / failure events from background archive jobs
  const memoryEventHandler = useCallback(
    (event: MemoryEvent) => {
      const sessionId = event.session_id;
      setArchiveInFlight((prev) => {
        if (!prev.has(sessionId)) return prev;
        const next = new Set(prev);
        next.delete(sessionId);
        return next;
      });
      archiveStartedAtRef.current.delete(sessionId);
      if (event.type === "archive.completed") {
        setArchiveErrors((prev) => {
          if (!prev.has(sessionId)) return prev;
          const next = new Map(prev);
          next.delete(sessionId);
          return next;
        });
        refreshRecentSummaries();
        toast.success(t("toasts:memory.savedTitle"));
      } else {
        const message = event.error || t("toasts:memory.fallbackError");
        setArchiveErrors((prev) => new Map(prev).set(sessionId, message));
        toast.error(message);
      }
    },
    [refreshRecentSummaries, t],
  );
  useMemoryEvents(memoryEventHandler);

  // Periodic refresh so auto-archives (compaction / session_end, which run in
  // the worker process and don't push to the gateway's SSE bus) surface in
  // the sidebar dot without a page reload.
  useEffect(() => {
    const interval = window.setInterval(() => {
      refreshRecentSummaries();
    }, 60_000);
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        refreshRecentSummaries();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [refreshRecentSummaries]);

  // Safety: if an SSE event never arrives (server crash, lost stream), drop
  // the session out of in-flight after 90s and mark it failed.
  useEffect(() => {
    const interval = window.setInterval(() => {
      const now = Date.now();
      const stuck: string[] = [];
      for (const [sid, startedAt] of archiveStartedAtRef.current) {
        if (now - startedAt > 90_000) {
          stuck.push(sid);
        }
      }
      if (stuck.length === 0) return;
      for (const sid of stuck) archiveStartedAtRef.current.delete(sid);
      setArchiveInFlight((prev) => {
        let changed = false;
        const next = new Set(prev);
        for (const sid of stuck) {
          if (next.delete(sid)) changed = true;
        }
        return changed ? next : prev;
      });
      setArchiveErrors((prev) => {
        const next = new Map(prev);
        for (const sid of stuck) {
          next.set(sid, "Timed out waiting for memory recording to complete");
        }
        return next;
      });
    }, 15_000);
    return () => window.clearInterval(interval);
  }, []);

  const deriveArchiveState = useCallback(
    (sessionId: string, lastUpdated: Date): ArchiveState => {
      if (archiveInFlight.has(sessionId)) return "in_progress";
      if (archiveErrors.has(sessionId)) return "red";
      const archived = lastArchivedBySession.get(sessionId);
      if (archived === undefined) return "gray";
      if (lastUpdated.getTime() / 1000 > archived + 2) return "yellow";
      return "green";
    },
    [archiveInFlight, archiveErrors, lastArchivedBySession],
  );

  // Transform Session[] to SessionSummary[] for sidebar
  const sessionSummaries = useMemo(
    () =>
      sessions.map((session) => ({
        id: session.sessionId,
        title: session.title ?? "Untitled",
        updatedAt: formatRelativeTime(session.lastUpdated),
        workDir: session.workDir,
        lastUpdated: session.lastUpdated,
        archiveState: deriveArchiveState(session.sessionId, session.lastUpdated),
        archiveError: archiveErrors.get(session.sessionId),
      })),
    [sessions, deriveArchiveState, archiveErrors],
  );

  // Transform archived Session[] to SessionSummary[] for sidebar
  const archivedSessionSummaries = useMemo(
    () =>
      archivedSessions.map((session) => ({
        id: session.sessionId,
        title: session.title ?? "Untitled",
        updatedAt: formatRelativeTime(session.lastUpdated),
        workDir: session.workDir,
        lastUpdated: session.lastUpdated,
        archiveState: deriveArchiveState(session.sessionId, session.lastUpdated),
        archiveError: archiveErrors.get(session.sessionId),
      })),
    [archivedSessions, deriveArchiveState, archiveErrors],
  );

  const handleForkSession = useCallback(
    async (sessionId: string, turnIndex: number) => {
      await forkSession(sessionId, turnIndex);
    },
    [forkSession],
  );

  const handleRecordSessionMemory = useCallback(async (sessionId: string) => {
    setArchiveErrors((prev) => {
      if (!prev.has(sessionId)) return prev;
      const next = new Map(prev);
      next.delete(sessionId);
      return next;
    });
    setArchiveInFlight((prev) => new Set(prev).add(sessionId));
    archiveStartedAtRef.current.set(sessionId, Date.now());
    try {
      await archiveSessionMemory(sessionId);
      toast(t("toasts:memory.recordingInBackground"));
    } catch (e) {
      const message =
        e instanceof Error ? e.message : t("toasts:memory.startFallbackError");
      setArchiveInFlight((prev) => {
        if (!prev.has(sessionId)) return prev;
        const next = new Set(prev);
        next.delete(sessionId);
        return next;
      });
      archiveStartedAtRef.current.delete(sessionId);
      setArchiveErrors((prev) => new Map(prev).set(sessionId, message));
      toast.error(message);
    }
  }, [t]);

  // Auth gates: loading, unauthenticated, admin route
  if (isAuthLoading && !currentUser) {
    return (
      <div className="flex h-[100dvh] items-center justify-center bg-background text-muted-foreground text-sm">
        Loading...
      </div>
    );
  }

  if (!currentUser) {
    return (
      <>
        <LoginPage onLogin={login} />
        <Toaster position="top-right" richColors />
      </>
    );
  }

  if (isAdminRoute) {
    if (!isAdmin) {
      // Non-admin users attempting to access /admin are redirected to home.
      window.location.replace("/");
      return null;
    }
    return (
      <>
        <AdminPage currentUser={currentUser} />
        <Toaster position="top-right" richColors />
      </>
    );
  }

  const renderChatPanel = () => (
    <ChatWorkspaceContainer
      selectedSessionId={selectedSessionId}
      currentSession={currentSession}
      sessionDescription={currentSession?.title}
      onSessionStatus={handleSessionStatus}
      onStreamStatusChange={handleStreamStatusChange}
      uploadSessionFile={uploadSessionFile}
      onListSessionDirectory={listSessionDirectory}
      onGetSessionFileUrl={getSessionFileUrl}
      onGetSessionFile={getSessionFile}
      onOpenCreateDialog={handleOpenCreateDialog}
      onOpenSidebar={handleOpenMobileSidebar}
      generateTitle={generateTitle}
      onRenameSession={renameSession}
      onForkSession={handleForkSession}
    />
  );

  return (
    <PromptInputProvider>
      <div className="box-border flex h-[100dvh] flex-col bg-background text-foreground px-[calc(0.75rem+var(--safe-left))] pr-[calc(0.75rem+var(--safe-right))] pt-[calc(0.75rem+var(--safe-top))] pb-1 lg:pb-[calc(0.75rem+var(--safe-bottom))] max-lg:h-[100svh] max-lg:overflow-hidden">
        <div className="mx-auto flex h-full min-h-0 w-full flex-1 flex-col gap-2 max-w-none">
          {isDesktop ? (
            <ResizablePanelGroup
              orientation="horizontal"
              className="min-h-0 flex-1 overflow-hidden"
            >
              {/* Sidebar */}
              <ResizablePanel
                id="sessions"
                collapsible
                collapsedSize={SIDEBAR_COLLAPSED_SIZE}
                defaultSize={SIDEBAR_DEFAULT_SIZE}
                minSize={SIDEBAR_MIN_SIZE}
                elementRef={sidebarElementRef}
                panelRef={sidebarPanelRef}
                onResize={handleSidebarResize}
                className={cn("relative min-h-0 border-r pl-0.5 pr-2 overflow-hidden")}
              >
                {/* Collapsed sidebar - vertical strip with logo and expand button */}
                <div
                  className={cn(
                    "absolute inset-0 flex h-full flex-col items-center py-3 transition-all duration-200 ease-in-out",
                    isSidebarCollapsed
                      ? "opacity-100 translate-x-0"
                      : "opacity-0 -translate-x-2 pointer-events-none select-none",
                  )}
                >
                  <a
                    href={collapsedLogoUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="hover:opacity-80 transition-opacity"
                  >
                    <img
                      src={collapsedLogoSrc}
                      alt="Logo"
                      width={24}
                      height={24}
                      className="size-6"
                    />
                  </a>
                  <button
                    type="button"
                    aria-label={t("common:sidebar.expand")}
                    className="mt-auto mb-1 inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary/50 hover:text-foreground"
                    onClick={handleExpandSidebar}
                  >
                    <PanelLeftOpen className="size-4" />
                  </button>
                </div>
                {/* Expanded sidebar */}
                <div
                  className={cn(
                    "absolute inset-0 flex h-full min-h-0 flex-col gap-3 transition-all duration-200 ease-in-out",
                    isSidebarCollapsed
                      ? "opacity-0 translate-x-2 pointer-events-none select-none"
                      : "opacity-100 translate-x-0",
                  )}
                >
                  <SessionsSidebar
                    onDeleteSession={handleDeleteSession}
                    onSelectSession={handleSelectSession}
                    onRenameSession={renameSession}
                    onArchiveSession={archiveSession}
                    onUnarchiveSession={unarchiveSession}
                    onRecordSessionMemory={handleRecordSessionMemory}
                    onBulkArchiveSessions={bulkArchiveSessions}
                    onBulkUnarchiveSessions={bulkUnarchiveSessions}
                    onBulkDeleteSessions={bulkDeleteSessions}
                    onRefreshSessions={handleRefreshSessions}
                    onRefreshArchivedSessions={refreshArchivedSessions}
                    onLoadMoreSessions={loadMoreSessions}
                    onLoadMoreArchivedSessions={loadMoreArchivedSessions}
                    onOpenCreateDialog={handleOpenCreateDialog}
                    onCreateSessionInDir={handleCreateSessionInDir}
                    streamStatus={streamStatus}
                    selectedSessionId={selectedSessionId}
                    sessions={sessionSummaries}
                    archivedSessions={archivedSessionSummaries}
                    hasMoreSessions={hasMoreSessions}
                    hasMoreArchivedSessions={hasMoreArchivedSessions}
                    isLoadingMore={isLoadingMore}
                    isLoadingMoreArchived={isLoadingMoreArchived}
                    isLoadingArchived={isLoadingArchived}
                    searchQuery={searchQuery}
                    onSearchQueryChange={handleSearchQueryChange}
                  />
                  <div className="mt-auto flex flex-col gap-1 pl-2 pb-2 pr-2">
                    {/* User info row */}
                    <div className="flex items-center gap-1 min-w-0 px-1 py-1 rounded-md">
                      <span className="truncate text-xs text-muted-foreground flex-1 min-w-0" title={currentUser.username}>
                        {currentUser.username}
                      </span>
                      {isAdmin && (
                        <a
                          href="/admin"
                          title={t("common:user.adminPanel")}
                          className="inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary/50 hover:text-foreground shrink-0"
                        >
                          <ShieldCheck className="size-3.5" />
                        </a>
                      )}
                      <button
                        type="button"
                        title={t("common:user.signOut")}
                        aria-label={t("common:user.signOut")}
                        className="inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary/50 hover:text-foreground shrink-0"
                        onClick={handleLogout}
                      >
                        <LogOut className="size-3.5" />
                      </button>
                    </div>
                    {/* Theme toggle + language toggle + collapse */}
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <ThemeToggle />
                        <LanguageToggle />
                      </div>
                      <button
                        type="button"
                        aria-label={t("common:sidebar.collapse")}
                        className="inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary/50 hover:text-foreground"
                        onClick={handleCollapseSidebar}
                      >
                        <PanelLeftClose className="size-4" />
                      </button>
                    </div>
                  </div>
                </div>
              </ResizablePanel>

              {/* Main Chat Area */}
              <ResizablePanel id="chat" className="relative min-h-0 flex justify-center flex-1">
                {renderChatPanel()}
              </ResizablePanel>
            </ResizablePanelGroup>
          ) : (
            <div className="flex min-h-0 flex-1 flex-col">
              {renderChatPanel()}
            </div>
          )}
        </div>
      </div>

      {/* Toast notifications */}
      <Toaster position="top-right" richColors />

      {/* Create Session Dialog - unified for sidebar button and keyboard shortcut */}
      <CreateSessionDialog
        open={showCreateDialog}
        onOpenChange={setShowCreateDialog}
        onConfirm={handleCreateSession}
        fetchWorkDirs={fetchWorkDirs}
        fetchStartupDir={fetchStartupDir}
      />

      {/* Mobile Sessions Sidebar */}
      {isMobileSidebarOpen ? (
        <div className="fixed inset-0 z-50 flex lg:hidden" role="dialog" aria-modal="true">
          <button
            type="button"
            className="absolute inset-0 bg-black/40"
            aria-label={t("common:sidebar.close")}
            onClick={handleCloseMobileSidebar}
          />
          <div className="relative flex h-full w-[min(86vw,360px)] flex-col border-r border-border bg-background pt-[var(--safe-top)] shadow-2xl">
            <div className="min-h-0 flex-1">
              <SessionsSidebar
                onDeleteSession={handleDeleteSession}
                onSelectSession={handleSelectSession}
                onRenameSession={renameSession}
                onArchiveSession={archiveSession}
                onUnarchiveSession={unarchiveSession}
                onRecordSessionMemory={handleRecordSessionMemory}
                onBulkArchiveSessions={bulkArchiveSessions}
                onBulkUnarchiveSessions={bulkUnarchiveSessions}
                onBulkDeleteSessions={bulkDeleteSessions}
                onRefreshSessions={handleRefreshSessions}
                onRefreshArchivedSessions={refreshArchivedSessions}
                onLoadMoreSessions={loadMoreSessions}
                onLoadMoreArchivedSessions={loadMoreArchivedSessions}
                onOpenCreateDialog={handleOpenCreateDialog}
                onCreateSessionInDir={handleCreateSessionInDir}
                onClose={handleCloseMobileSidebar}
                streamStatus={streamStatus}
                selectedSessionId={selectedSessionId}
                sessions={sessionSummaries}
                archivedSessions={archivedSessionSummaries}
                hasMoreSessions={hasMoreSessions}
                hasMoreArchivedSessions={hasMoreArchivedSessions}
                isLoadingMore={isLoadingMore}
                isLoadingMoreArchived={isLoadingMoreArchived}
                isLoadingArchived={isLoadingArchived}
                searchQuery={searchQuery}
                onSearchQueryChange={handleSearchQueryChange}
              />
            </div>
            <div className="flex flex-col gap-1 border-t px-3 py-2">
              <div className="flex items-center gap-1 min-w-0">
                <span className="truncate text-xs text-muted-foreground flex-1 min-w-0" title={currentUser.username}>
                  {currentUser.username}
                </span>
                {isAdmin && (
                  <a
                    href="/admin"
                    title={t("common:user.adminPanel")}
                    className="inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary/50 hover:text-foreground shrink-0"
                  >
                    <ShieldCheck className="size-3.5" />
                  </a>
                )}
                <button
                  type="button"
                  title={t("common:user.signOut")}
                  aria-label={t("common:user.signOut")}
                  className="inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary/50 hover:text-foreground shrink-0"
                  onClick={handleLogout}
                >
                  <LogOut className="size-3.5" />
                </button>
              </div>
              <div className="flex flex-col items-center gap-2">
                <ThemeToggle />
                <LanguageToggle />
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </PromptInputProvider>
  );
}

function AppWithBranding() {
  return (
    <BrandingProvider>
      <App />
    </BrandingProvider>
  );
}

export default AppWithBranding;
