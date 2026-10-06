// Resource aggregation is separate from the frozen eighteen backend nodes.
// A capability or resource count never supplies execution success.
export function pipelineMemoryObservation(graph: unknown) {
  const root = graph && typeof graph === "object" ? graph as Record<string, unknown> : undefined;
  const raw = root?.memory;
  const memory = raw && typeof raw === "object" ? raw as Record<string, unknown> : undefined;
  const count = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
  const clock = (value: unknown) => typeof value === "string" && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value)) ? Date.parse(value) : undefined;
  const issued = clock(root?.generated_at);
  const observed = clock(memory?.data_as_of);
  const valid = memory?.schema_version === "pipeline-memory.v1" && memory.capability_source === "AGENT_MEMORY_RECALL_ENABLED"
    && typeof memory.recall_enabled === "boolean" && observed !== undefined && issued !== undefined && observed <= issued;
  const planning = !memory || (valid && memory.recall_enabled === false);
  const enabled = valid && memory.recall_enabled === true;
  const indexDocs = valid && memory.index_docs_source === "semantic_index.active_index.document_count" ? count(memory.index_docs) : undefined;
  const lessonsActive = valid && memory.lessons_active_source === "agent_lessons:active.validity_window" ? count(memory.lessons_active) : undefined;
  const evidence = memory?.status_evidence && typeof memory.status_evidence === "object" ? memory.status_evidence as Record<string, unknown> : undefined;
  const evidenceAt = clock(evidence?.observed_at);
  const hasEvidence = enabled && evidence?.source === "event-agent-chain-latest.json:memory.recalls"
    && evidence.business_date === root?.business_date && typeof evidence.run_id === "string" && evidence.run_id.length > 0
    && evidenceAt !== undefined && observed !== undefined && evidenceAt <= observed
    && new Date(evidenceAt + 8 * 60 * 60 * 1000).toISOString().slice(0, 10) === root?.business_date
    && (count(evidence.recall_count) ?? 0) > 0 && count(evidence.fragment_count) !== undefined;
  const status: "ok" | "degraded" | undefined = hasEvidence && memory?.status === "ok" ? "ok"
    : hasEvidence && memory?.status === "degraded" ? "degraded" : undefined;
  const capability = enabled ? "召回已启用" : planning ? "召回规划中" : "启用状态未知";
  const votingKnown = valid && memory?.voting_source === "AGENT_MEMORY_VOTING_ENABLED"
    && typeof memory.voting_enabled === "boolean";
  const votingSummary = votingKnown ? memory!.voting_enabled ? "召回计票已启用" : "新增召回不参与当前计票" : "新增计票状态未知";
  return { planning, enabled, status, indexDocs, lessonsActive,
    timestamp: valid ? memory!.data_as_of as string : "",
    capability,
    summary: `${capability} · 索引 ${indexDocs ?? "—"} 文档 · 活跃教训 ${lessonsActive ?? "—"} 条。${status === "ok" ? "检索运行已核验" : status === "degraded" ? "检索运行降级" : "检索运行未核验"}；${votingSummary}。`,
    evidenceSummary: hasEvidence ? `本次 ${evidence!.recall_count} 次召回 · ${evidence!.fragment_count} 个片段` : "暂无可核验的当日召回记录"
  };
}
