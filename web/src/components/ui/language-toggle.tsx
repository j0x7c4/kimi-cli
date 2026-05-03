import { useTranslation } from "react-i18next";

import { useLanguage } from "@/hooks/use-language";
import { cn } from "@/lib/utils";

import { Button } from "./button";

type LanguageToggleProps = {
  className?: string;
};

export function LanguageToggle({ className }: LanguageToggleProps) {
  const { language, toggleLanguage } = useLanguage();
  const { t } = useTranslation("common");
  const isZh = language === "zh-CN";

  return (
    <Button
      aria-label={isZh ? t("lang.switchToEn") : t("lang.switchToZh")}
      title={isZh ? t("lang.switchToEn") : t("lang.switchToZh")}
      className={cn(
        "size-9 p-0 text-foreground hover:text-foreground dark:hover:text-foreground hover:bg-accent/20 dark:hover:bg-accent/20",
        "cursor-pointer text-sm font-medium",
        className,
      )}
      onClick={toggleLanguage}
      size="icon"
      variant="outline"
    >
      <span aria-hidden="true">{isZh ? "中" : "EN"}</span>
    </Button>
  );
}
