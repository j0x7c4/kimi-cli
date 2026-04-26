import {
  createContext,
  useContext,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { getBranding, type BrandingConfig } from "@/lib/api/apis/BrandingApi";

export const BRANDING_DEFAULTS = {
  brand_name: "Kimi Code",
  version: null,
  page_title: "Kimi Code Web UI",
  logo_url: "https://www.kimi.com/code",
  logo: "/logo.png",
  favicon: "/logo.png",
} as const;

export type BrandingState = {
  config: BrandingConfig | null;
  isLoading: boolean;
  refresh: () => Promise<void>;
};

const BrandingContext = createContext<BrandingState>({
  config: null,
  isLoading: true,
  refresh: async () => {},
});

export function useBranding(): BrandingState {
  return useContext(BrandingContext);
}

export function BrandingProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<BrandingConfig | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const isInitializedRef = useRef(false);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    try {
      const data = await getBranding();
      setConfig(data);
    } catch (err) {
      console.error("[useBranding] Failed to load branding:", err);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isInitializedRef.current) return;
    isInitializedRef.current = true;
    refresh();
  }, [refresh]);

  useEffect(() => {
    const handler = () => {
      refresh();
    };
    window.addEventListener("kimi:branding-update", handler);
    return () => window.removeEventListener("kimi:branding-update", handler);
  }, [refresh]);

  useEffect(() => {
    const channel = new BroadcastChannel("kimi:branding");
    channel.onmessage = () => {
      refresh();
    };
    return () => channel.close();
  }, [refresh]);

  useEffect(() => {
    if (!config) return;
    document.title = config.page_title ?? BRANDING_DEFAULTS.page_title;
  }, [config]);

  useEffect(() => {
    if (!config) return;
    const faviconUrl = config.favicon ?? BRANDING_DEFAULTS.favicon;
    let link = document.querySelector(
      'link[rel="icon"]',
    ) as HTMLLinkElement | null;
    if (!link) {
      link = document.createElement("link");
      link.rel = "icon";
      document.head.appendChild(link);
    }
    link.href = faviconUrl;
  }, [config]);

  return (
    <BrandingContext.Provider value={{ config, isLoading, refresh }}>
      {children}
    </BrandingContext.Provider>
  );
}
