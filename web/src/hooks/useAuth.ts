import { useState, useEffect, useCallback } from "react";
import { login as apiLogin, logout as apiLogout, getMe } from "../lib/api/apis/AuthApi";
import type { UserInfo } from "../lib/api/apis/AuthApi";

const CURRENT_USER_KEY = "kimi_current_user";

function loadCachedUser(): UserInfo | null {
  try {
    const raw = localStorage.getItem(CURRENT_USER_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as UserInfo;
  } catch {
    return null;
  }
}

function saveUser(user: UserInfo | null): void {
  if (user) {
    localStorage.setItem(CURRENT_USER_KEY, JSON.stringify(user));
  } else {
    localStorage.removeItem(CURRENT_USER_KEY);
  }
}

type UseAuthReturn = {
  currentUser: UserInfo | null;
  isLoading: boolean;
  isAdmin: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
};

export function useAuth(): UseAuthReturn {
  const [currentUser, setCurrentUser] = useState<UserInfo | null>(() => loadCachedUser());
  const [isLoading, setIsLoading] = useState(true);

  // On mount, verify session with backend (use cache optimistically while verifying)
  useEffect(() => {
    let cancelled = false;

    getMe()
      .then((user) => {
        if (cancelled) return;
        setCurrentUser(user);
        saveUser(user);
      })
      .catch(() => {
        if (cancelled) return;
        // Network error - keep cached user to avoid unnecessary logout
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (username: string, password: string): Promise<void> => {
    const user = await apiLogin(username, password);
    setCurrentUser(user);
    saveUser(user);
  }, []);

  const logout = useCallback(async (): Promise<void> => {
    try {
      await apiLogout();
    } finally {
      setCurrentUser(null);
      saveUser(null);
    }
  }, []);

  const isAdmin = currentUser?.role === "admin";

  return { currentUser, isLoading, isAdmin, login, logout };
}
