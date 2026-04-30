import { useCallback, useEffect, useRef, useState } from "react";

import { getAuthHeader, getAuthToken } from "../lib/auth";
import { getApiBaseUrl } from "./utils";

export type MemoryKind = "user" | "feedback" | "project" | "reference";

export type KnowledgeFile = {
  name: string;
  size: number;
  mtime: number;
};

export type KnowledgeFileContent = {
  name: string;
  content: string;
};

export type PersistentEntry = {
  id: string;
  kind: MemoryKind;
  content: string;
  created_at: number;
  updated_at: number | null;
};

export type RecentSummary = {
  id: string;
  session_id: string;
  created_at: number;
  trigger: string;
  summary: string;
  work_dir: string | null;
};

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const url = `${getApiBaseUrl()}${path}`;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...getAuthHeader(),
    ...((init.headers as Record<string, string>) ?? {}),
  };
  const res = await fetch(url, { ...init, headers });
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const data = await res.json();
      if (typeof data?.detail === "string") {
        message = data.detail;
      }
    } catch {
      /* ignore */
    }
    throw new Error(message);
  }
  if (res.status === 204) {
    return undefined as unknown as T;
  }
  return (await res.json()) as T;
}

// ----------------------- knowledge base -----------------------

export function listKnowledge(sessionId: string): Promise<KnowledgeFile[]> {
  return api(`/api/memory/knowledge?session_id=${encodeURIComponent(sessionId)}`);
}

export function readKnowledge(
  sessionId: string,
  filename: string,
): Promise<KnowledgeFileContent> {
  return api(
    `/api/memory/knowledge/${encodeURIComponent(filename)}?session_id=${encodeURIComponent(sessionId)}`,
  );
}

export function writeKnowledge(
  sessionId: string,
  filename: string,
  content: string,
): Promise<KnowledgeFile> {
  return api(
    `/api/memory/knowledge/${encodeURIComponent(filename)}?session_id=${encodeURIComponent(sessionId)}`,
    {
      method: "PUT",
      body: JSON.stringify({ content }),
    },
  );
}

export function deleteKnowledge(sessionId: string, filename: string): Promise<void> {
  return api(
    `/api/memory/knowledge/${encodeURIComponent(filename)}?session_id=${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
  );
}

// ----------------------- persistent memory -----------------------

export function listPersistent(): Promise<PersistentEntry[]> {
  return api("/api/memory/persistent");
}

export function addPersistent(kind: MemoryKind, content: string): Promise<PersistentEntry> {
  return api("/api/memory/persistent", {
    method: "POST",
    body: JSON.stringify({ kind, content }),
  });
}

export function updatePersistent(id: string, content: string): Promise<PersistentEntry> {
  return api(`/api/memory/persistent/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify({ content }),
  });
}

export function deletePersistent(id: string): Promise<void> {
  return api(`/api/memory/persistent/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

// ----------------------- recent summaries -----------------------

export function listRecent(limit = 50): Promise<RecentSummary[]> {
  return api(`/api/memory/recent?limit=${limit}`);
}

export type ArchiveAccepted = {
  session_id: string;
  status: string;
};

export function archiveSessionMemory(sessionId: string): Promise<ArchiveAccepted> {
  return api(`/api/memory/sessions/${encodeURIComponent(sessionId)}/archive`, {
    method: "POST",
  });
}

// ----------------------- memory events (SSE) -----------------------

export type MemoryEvent =
  | { type: "archive.completed"; session_id: string; summary: RecentSummary }
  | { type: "archive.failed"; session_id: string; error: string };

/**
 * Open a single SSE connection to ``/api/memory/events`` for the current user
 * and dispatch parsed events to ``onEvent``. Browser ``EventSource``
 * automatically reconnects on disconnect.
 *
 * Auth: cookie via ``withCredentials``; falls back to ``?token=`` query param
 * for Bearer-token clients (``EventSource`` cannot send custom headers).
 */
export function useMemoryEvents(onEvent: (event: MemoryEvent) => void): void {
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    const token = getAuthToken();
    const base = `${getApiBaseUrl()}/api/memory/events`;
    const url = token ? `${base}?token=${encodeURIComponent(token)}` : base;
    const es = new EventSource(url, { withCredentials: true });
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as MemoryEvent;
        handlerRef.current(data);
      } catch {
        /* ignore malformed events */
      }
    };
    es.onerror = () => {
      /* browser auto-reconnects; don't close manually */
    };
    return () => {
      es.close();
    };
  }, []);
}

// ----------------------- React hooks -----------------------

export function usePersistentMemory() {
  const [entries, setEntries] = useState<PersistentEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setEntries(await listPersistent());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const add = useCallback(
    async (kind: MemoryKind, content: string) => {
      await addPersistent(kind, content);
      await refresh();
    },
    [refresh],
  );

  const update = useCallback(
    async (id: string, content: string) => {
      await updatePersistent(id, content);
      await refresh();
    },
    [refresh],
  );

  const remove = useCallback(
    async (id: string) => {
      await deletePersistent(id);
      await refresh();
    },
    [refresh],
  );

  return { entries, loading, error, refresh, add, update, remove };
}

export function useRecentSummaries(limit = 50) {
  const [items, setItems] = useState<RecentSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setItems(await listRecent(limit));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { items, loading, error, refresh };
}

export function useKnowledgeBase(sessionId: string | null) {
  const [files, setFiles] = useState<KnowledgeFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!sessionId) {
      setFiles([]);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setFiles(await listKnowledge(sessionId));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { files, loading, error, refresh };
}
