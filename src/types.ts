export type Direction = "利多" | "利空" | "中性";

export type Horizon = "1d" | "7d" | "30d";

export interface Factor {
  name: string;
  symbol: string;
  direction: Direction;
  change: string;
  strength: "强" | "中强" | "中" | "弱";
  contribution: number;
  route: string;
  reason: string;
}

export interface EventImpact {
  id: string;
  title: string;
  type: string;
  nature: string;
  affected: string[];
  horizon: string;
  evidence: "A" | "B" | "C" | "D";
  confidence: number;
  chain: string[];
  judgement: string;
  stakeholders: string[];
  beneficiaries: string[];
  harmed: string[];
  counter: string;
}

export interface Prediction {
  id: string;
  createdAt: string;
  target: string;
  horizon: Horizon;
  direction: string;
  indexRange: [number, number];
  confidence: number;
  status: "待复盘" | "已复盘";
  result?: string;
  deviation?: string;
}

export interface SourceItem {
  name: string;
  tier: "A" | "B" | "C" | "D";
  category: string;
  crawlType: string;
  auth: string;
  frequency: string;
  health: "正常" | "延迟" | "需授权" | "观察";
  focus: string;
}

export interface GraphNode {
  id: string;
  label: string;
  kind: "commodity" | "event" | "indicator" | "organization" | "output";
  x: number;
  y: number;
}

export interface GraphEdge {
  from: string;
  to: string;
  label: string;
  sentiment: "up" | "down" | "neutral";
}
