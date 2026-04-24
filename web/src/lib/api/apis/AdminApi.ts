import { getAuthHeader } from "../../auth";
import { getApiBaseUrl } from "../../../hooks/utils";

export interface AdminUser {
  id: string;
  username: string;
  role: string;
  is_active: boolean;
  created_at: number;
  session_count: number;
}

function apiUrl(path: string): string {
  return `${getApiBaseUrl()}${path}`;
}

async function handleResponse<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let message = `Request failed (${resp.status})`;
    try {
      const data = await resp.json();
      if (typeof data.detail === "string") {
        message = data.detail;
      } else if (typeof data.msg === "string") {
        message = data.msg;
      }
    } catch {
      // ignore JSON parse errors
    }
    throw new Error(message);
  }
  return resp.json() as Promise<T>;
}

export async function listUsers(): Promise<AdminUser[]> {
  const resp = await fetch(apiUrl("/api/admin/users"), {
    method: "GET",
    headers: {
      ...getAuthHeader(),
    },
    credentials: "include",
  });
  return handleResponse<AdminUser[]>(resp);
}

export async function createUser(
  username: string,
  password: string,
  role: string,
): Promise<AdminUser> {
  const resp = await fetch(apiUrl("/api/admin/users"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...getAuthHeader(),
    },
    credentials: "include",
    body: JSON.stringify({ username, password, role }),
  });
  return handleResponse<AdminUser>(resp);
}

export async function updateUser(
  id: string,
  data: { password?: string; role?: string; is_active?: boolean },
): Promise<AdminUser> {
  const resp = await fetch(apiUrl(`/api/admin/users/${encodeURIComponent(id)}`), {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      ...getAuthHeader(),
    },
    credentials: "include",
    body: JSON.stringify(data),
  });
  return handleResponse<AdminUser>(resp);
}

export async function deleteUser(id: string): Promise<void> {
  const resp = await fetch(apiUrl(`/api/admin/users/${encodeURIComponent(id)}`), {
    method: "DELETE",
    headers: {
      ...getAuthHeader(),
    },
    credentials: "include",
  });
  if (!resp.ok) {
    let message = `Delete failed (${resp.status})`;
    try {
      const data = await resp.json();
      if (typeof data.detail === "string") {
        message = data.detail;
      }
    } catch {
      // ignore
    }
    throw new Error(message);
  }
}
