import { useCallback } from "react";
import { useTranslation } from "react-i18next";

import {
  LANGUAGE_STORAGE_KEY,
  normalizeLanguage,
  type SupportedLang,
} from "@/i18n";

type UseLanguageResult = {
  language: SupportedLang;
  setLanguage: (next: SupportedLang) => void;
  toggleLanguage: () => void;
};

export function useLanguage(): UseLanguageResult {
  // Subscribe to react-i18next so consumers re-render on language change.
  const { i18n } = useTranslation();
  const language = normalizeLanguage(i18n.resolvedLanguage ?? i18n.language);

  const setLanguage = useCallback(
    (next: SupportedLang) => {
      if (typeof window !== "undefined") {
        window.localStorage.setItem(LANGUAGE_STORAGE_KEY, next);
      }
      i18n.changeLanguage(next).catch(() => undefined);
    },
    [i18n],
  );

  const toggleLanguage = useCallback(() => {
    setLanguage(language === "zh-CN" ? "en" : "zh-CN");
  }, [language, setLanguage]);

  return { language, setLanguage, toggleLanguage };
}
