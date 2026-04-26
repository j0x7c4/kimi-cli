import { getAuthHeader } from "../../auth";
import { getApiBaseUrl } from "../../../hooks/utils";

export interface BrandingConfig {
  brand_name: string | null;
  version: string | null;
  page_title: string | null;
  logo_url: string | null;
  logo: string | null;
  favicon: string | null;
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

export async function getBranding(): Promise<BrandingConfig> {
  const resp = await fetch(apiUrl("/api/branding"), {
    method: "GET",
    headers: { ...getAuthHeader() },
    credentials: "include",
  });
  return handleResponse<BrandingConfig>(resp);
}

export async function getAdminBranding(): Promise<BrandingConfig> {
  const resp = await fetch(apiUrl("/api/admin/branding"), {
    method: "GET",
    headers: { ...getAuthHeader() },
    credentials: "include",
  });
  return handleResponse<BrandingConfig>(resp);
}

export async function updateBranding(
  data: Partial<BrandingConfig>,
): Promise<BrandingConfig> {
  const resp = await fetch(apiUrl("/api/admin/branding"), {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      ...getAuthHeader(),
    },
    credentials: "include",
    body: JSON.stringify(data),
  });
  return handleResponse<BrandingConfig>(resp);
}

export async function resetBranding(): Promise<void> {
  const resp = await fetch(apiUrl("/api/admin/branding"), {
    method: "DELETE",
    headers: { ...getAuthHeader() },
    credentials: "include",
  });
  if (!resp.ok) {
    let message = `Reset failed (${resp.status})`;
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
