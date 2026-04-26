import { kimiCliVersion } from "@/lib/version";
import { cn } from "@/lib/utils";
import { useBranding, BRANDING_DEFAULTS } from "@/hooks/useBranding";

type KimiCliBrandProps = {
  className?: string;
  size?: "sm" | "md";
  showVersion?: boolean;
};

export function KimiCliBrand({
  className,
  size = "md",
  showVersion = true,
}: KimiCliBrandProps) {
  const { config } = useBranding();

  const brandName = config?.brand_name ?? BRANDING_DEFAULTS.brand_name;
  const logoSrc = config?.logo ?? BRANDING_DEFAULTS.logo;
  const logoUrl = config?.logo_url ?? BRANDING_DEFAULTS.logo_url;
  const versionText = config?.version || kimiCliVersion;

  const textSizeClass = size === "sm" ? "text-base" : "text-lg";
  const versionPadding = size === "sm" ? "text-xs" : "text-sm";
  const logoSize = size === "sm" ? "size-6" : "size-7";
  const logoPx = size === "sm" ? 24 : 28;

  return (
    <div className={cn("flex items-center gap-2", className)}>
      <a
        href={logoUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="flex items-center gap-2 hover:opacity-80 transition-opacity"
      >
        <img
          src={logoSrc}
          alt={brandName}
          width={logoPx}
          height={logoPx}
          className={logoSize}
        />
        <span className={cn(textSizeClass, "font-semibold text-foreground")}>
          {brandName}
        </span>
      </a>
      {showVersion && (
        <span
          className={cn("text-muted-foreground font-medium", versionPadding)}
        >
          v{versionText}
        </span>
      )}
    </div>
  );
}
