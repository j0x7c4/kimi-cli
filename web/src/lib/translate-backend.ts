import type { TFunction } from "i18next";

/**
 * Translates a backend-emitted English string into the active UI language using
 * the `backend` namespace catalog. The backend code itself is not modified, so
 * this is a best-effort lookup:
 *   1. Exact-match lookup against `backend:exact.<raw>`.
 *   2. Pattern lookup against known templates (e.g. "Background task completed: …").
 *   3. Fallback: return the original string unchanged.
 *
 * Add new backend strings by extending `backend.json` (`exact.*`) and the
 * `BACKEND_TEMPLATES` table below.
 */

type BackendTemplate = {
  pattern: RegExp;
  key: string;
  capture: (match: RegExpMatchArray) => Record<string, string>;
};

const BACKEND_TEMPLATES: BackendTemplate[] = [
  {
    pattern: /^Background task completed:\s*(.*)$/,
    key: "backend:bgTask.completed",
    capture: (m) => ({ desc: m[1] ?? "" }),
  },
  {
    pattern: /^Background task failed:\s*(.*)$/,
    key: "backend:bgTask.failed",
    capture: (m) => ({ desc: m[1] ?? "" }),
  },
  {
    pattern: /^Background task timed out:\s*(.*)$/,
    key: "backend:bgTask.timedOut",
    capture: (m) => ({ desc: m[1] ?? "" }),
  },
  {
    pattern: /^Background task killed:\s*(.*)$/,
    key: "backend:bgTask.killed",
    capture: (m) => ({ desc: m[1] ?? "" }),
  },
  {
    pattern: /^Background task lost:\s*(.*)$/,
    key: "backend:bgTask.lost",
    capture: (m) => ({ desc: m[1] ?? "" }),
  },
  {
    pattern: /^Rejected:\s*(.*)$/,
    key: "backend:approval.rejectedWithFeedback",
    capture: (m) => ({ feedback: m[1] ?? "" }),
  },
];

export function translateBackendMessage(
  raw: string | null | undefined,
  t: TFunction,
): string {
  if (!raw) return "";

  const exactKey = `backend:exact.${raw}`;
  const exact = t(exactKey, { defaultValue: "" });
  if (exact && exact !== exactKey) return exact;

  for (const tpl of BACKEND_TEMPLATES) {
    const match = raw.match(tpl.pattern);
    if (match) return t(tpl.key, tpl.capture(match));
  }

  return raw;
}
