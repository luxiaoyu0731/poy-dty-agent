import type { EventDetail } from "./api";

const PRODUCTS: Record<string, string> = { crude: "原油", naphtha: "石脑油", px: "PX", pta: "PTA", meg: "MEG", poy: "POY", dty: "DTY" };
const HORIZONS: Record<string, string> = { D1: "1 天", D7: "7 天", D30: "30 天" };

/** Present only gaps already supplied by the event API; never invent counterclaims. */
export function eventEvidenceGaps(detail: Pick<EventDetail, "gaps" | "horizon_impact">) {
  const rows = new Map<string, Set<string>>();
  const add = (message: string, context?: string) => {
    const text = message.trim();
    if (!text) return;
    if (!rows.has(text)) rows.set(text, new Set());
    if (context) rows.get(text)!.add(context);
  };
  for (const gap of detail.gaps) add(gap.message_safe);
  for (const impact of detail.horizon_impact) {
    const context = `${PRODUCTS[impact.product_id] ?? impact.product_id.toUpperCase()} · ${HORIZONS[impact.horizon] ?? impact.horizon}`;
    for (const gap of impact.gaps) add(gap, context);
  }
  return [...rows].map(([message, contexts]) => ({ message, contexts: [...contexts] }));
}
