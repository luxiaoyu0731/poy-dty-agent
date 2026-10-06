import { navItems } from "../data/intelligence";

export function pageFromHash() {
  if (typeof window === "undefined") return navItems[0];
  const hash = decodeURIComponent(window.location.hash.replace(/^#\/?/, ""));
  return navItems.includes(hash) ? hash : navItems[0];
}

export function hashForPage(page: string) {
  return `#/${encodeURIComponent(page)}`;
}
