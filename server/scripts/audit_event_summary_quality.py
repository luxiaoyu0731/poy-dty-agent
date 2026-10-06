from __future__ import annotations

import argparse
import hashlib
import html
import importlib
import json
import re
import sys
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

connect = importlib.import_module("app.storage").connect

HTML_RE = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>|&(?:nbsp|amp|lt|gt|quot|#\d+);", re.I)
TEMPLATE_TERMS = ("影响DTY", "影响POY", "初步方向", "当前判断", "成本传导", "仅供观察", "待真实证据")
JUDGMENT_TERMS = ("利多", "利空", "偏强", "偏弱", "建议买入", "建议卖出", "风险等级", "置信度", "推翻条件", "核验信号")
DISCLAIMER_TERMS = ("AI生成可能有误", "仅供参考", "请核实", "无法确认", "信息可能不完整", "请通过原始来源核验")
PROJECT_TERMS = (
    "POY",
    "DTY",
    "PX",
    "PTA",
    "MEG",
    "原油",
    "石脑油",
    "聚酯",
    "长丝",
    "成本",
    "库存",
    "开工",
    "利润",
    "欧佩克",
    "OPEC",
    "减产",
)
ACTION_RE = re.compile(
    r"宣布|表示|称|发布|决定|完成|部署|打击|袭击|延长|削减|增加|暂停|恢复|启动|关闭|上涨|下跌|制裁|解除|将|计划"
)
ACTOR_RE = re.compile(
    r"(?:公司|集团|政府|部门|委员会|组织|司令部|部队|美军|士兵|港口|工厂|装置|欧佩克|OPEC|[A-Z][A-Za-z]{2,})"
)
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%/-]*|[\u4e00-\u9fff]{2,}")


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def tokens(value: str) -> set[str]:
    result: set[str] = set()
    for token in TOKEN_RE.findall(normalize(value).lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            result.update(token[index : index + 2] for index in range(max(0, len(token) - 1)))
        elif len(token) >= 3:
            result.add(token)
    return result


def language_family(value: str) -> str:
    text = normalize(value)
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if not chinese and not latin:
        return "unknown"
    if chinese >= max(2, latin * 0.2):
        return "zh"
    return "latin"


def failure_cluster(error: str) -> str:
    value = (error or "").lower()
    groups = {
        "timeout": ("timeout", "timed out"),
        "rate_limit": ("429", "rate limit", "too many requests"),
        "authentication": ("401", "403", "auth", "api key"),
        "network": ("connection", "network", "dns", "ssl"),
        "provider_5xx": ("500", "502", "503", "504", "server error"),
        "invalid_output": ("empty", "invalid", "parse", "json", "format"),
        "content_policy": ("content policy", "moderation", "safety"),
    }
    for label, needles in groups.items():
        if any(needle in value for needle in needles):
            return label
    return "other" if value else "missing_reason"


def pipeline_state(row: dict[str, Any]) -> str:
    """Return an aggregate-safe lifecycle state; private diagnostics stay local."""
    input_grade = str(row.get("input_quality") or "unknown")
    if input_grade in {"title_only", "partial_text"}:
        return "awaiting_source"
    status = str(row.get("summary_status") or "").strip().lower()
    quality_status = str(row.get("quality_status") or "").strip().lower()
    try:
        decoded_reasons = json.loads(str(row.get("quality_reasons") or "[]"))
    except json.JSONDecodeError:
        decoded_reasons = []
    reasons = {str(reason).lower() for reason in decoded_reasons if isinstance(reason, str)}
    if status in {"completed", "success", "ready"} and quality_status != "rejected":
        return "ready"
    if quality_status == "manual_review" or "manual_review" in reasons:
        return "manual_review"
    if status == "rejected" or quality_status == "rejected":
        if any(marker in reason for reason in reasons for marker in ("schema", "json", "format", "parse", "structure")):
            return "schema_review"
        return "grounding_review"
    if status in {"pending", "queued", "retrying"}:
        return "queued"
    if status in {"running", "processing"}:
        return "processing"
    if status in {"failed", "error", "exhausted"}:
        return "dead_letter" if status == "exhausted" or int(row.get("attempts") or 0) >= 3 else "provider_delayed"
    return "not_queued"


def audit_row(row: dict[str, Any]) -> dict[str, Any]:
    summary = normalize(str(row.get("factual_summary") or ""))
    source = normalize(" ".join(str(row.get(key) or "") for key in ("title", "raw_text", "source_summary")))
    status = str(row.get("summary_status") or "unknown")
    try:
        raw_metadata = json.loads(str(row.get("raw") or "{}"))
    except json.JSONDecodeError:
        raw_metadata = {}
    source_content = raw_metadata.get("source_content", {}) if isinstance(raw_metadata, dict) else {}
    summary_input = raw_metadata.get("summary_input_quality", {}) if isinstance(raw_metadata, dict) else {}
    input_grade = str(
        row.get("input_quality")
        or (source_content.get("status") if isinstance(source_content, dict) else "")
        or (summary_input.get("level") if isinstance(summary_input, dict) else "")
        or "unknown"
    )
    if input_grade not in {"full_text", "partial_text", "title_only"}:
        input_grade = "unknown"
    accepted_title_only = input_grade == "title_only"
    summary_tokens, source_tokens = tokens(summary), tokens(source)
    overlap = len(summary_tokens & source_tokens) / max(1, len(summary_tokens))
    issues: list[str] = []
    if status == "completed" and not summary:
        issues.append("completed_empty")
    if status == "completed" and input_grade in {"partial_text", "title_only"}:
        issues.append("input_not_full_text")
    if summary and len(summary) < 24 and not accepted_title_only:
        issues.append("too_short")
    if len(summary) > 600:
        issues.append("too_long")
    if HTML_RE.search(summary):
        issues.append("html_pollution")
    if any(term.lower() in summary.lower() for term in TEMPLATE_TERMS):
        issues.append("template_pollution")
    disclaimer_hits = [term for term in DISCLAIMER_TERMS if term.lower() in summary.lower()]
    if disclaimer_hits:
        issues.append("disclaimer_pollution")
    judgment_hits = [term for term in JUDGMENT_TERMS if term.lower() in summary.lower()]
    factual_direction_translation = ("偏弱" in judgment_hits and re.search(r"\bweak(?:er|ness)?\b", source, re.I)) or (
        "建议卖出" in judgment_hits and re.search(r"\bsell\b", source, re.I)
    )
    if judgment_hits and not factual_direction_translation:
        issues.append("judgment_pollution")
    # Lexical overlap is meaningful only within the same language family.
    # Chinese summaries of English sources are translations; low character/token
    # overlap is expected and must not be reported as a factual-quality failure.
    if (
        summary
        and source
        and not accepted_title_only
        and language_family(summary) == language_family(source)
        and overlap < 0.12
    ):
        issues.append("low_traceability")
    summary_language = language_family(summary)
    declared = str(row.get("language") or "").lower()
    if summary and declared.startswith("zh") and summary_language == "latin":
        issues.append("language_mismatch")
    if summary and declared.startswith("en") and summary_language == "unknown":
        issues.append("language_unknown")
    fact_structure = {
        "has_actor": bool(ACTOR_RE.search(summary)),
        "has_action": bool(ACTION_RE.search(summary)),
        "has_time": bool(re.search(r"\d{4}年|\d{1,2}月\d{1,2}日|\b\d{4}-\d{2}-\d{2}\b|今日|昨日|本周|年底", summary)),
        "has_number": bool(re.search(r"\d", summary)),
    }
    if (
        status == "completed"
        and input_grade == "full_text"
        and not (fact_structure["has_actor"] and fact_structure["has_action"])
    ):
        issues.append("missing_fact_structure")
    relevance_hits = [term for term in PROJECT_TERMS if term.lower() in f"{source} {summary}".lower()]
    project_relevance = {"relevant": bool(relevance_hits), "matched_terms": relevance_hits}
    if (
        status == "completed"
        and not relevance_hits
        and row.get("impact_analysis_status") not in {"irrelevant", "rejected"}
    ):
        issues.append("low_project_relevance")
    information_density = round(min(1.0, len(summary_tokens) / max(1.0, len(summary) / 8)), 4)
    return {
        "article_id": str(row.get("article_id") or ""),
        "status": status,
        "source_id": str(row.get("source_id") or "unknown"),
        "language": declared or "unknown",
        "title": str(row.get("title") or ""),
        "summary": summary,
        "summary_chars": len(summary),
        "traceability_overlap": round(overlap, 4),
        "information_density": information_density,
        "input_grade": input_grade,
        "disclaimer_hits": disclaimer_hits,
        "fact_structure": fact_structure,
        "project_relevance": project_relevance,
        "issues": list(dict.fromkeys(issues)),
        "source_disposition": "accepted_title_only" if accepted_title_only else input_grade,
        "failure_cluster": failure_cluster(str(row.get("error") or "")) if status == "failed" else None,
    }


def stratified_sample(records: list[dict[str, Any]], per_stratum: int) -> list[dict[str, Any]]:
    strata: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        quality = "flagged" if record["issues"] else "clean"
        strata[(record["status"], record["language"], quality)].append(record)
    sample: list[dict[str, Any]] = []
    for key in sorted(strata):
        ordered = sorted(strata[key], key=lambda item: hashlib.sha256(item["article_id"].encode()).hexdigest())
        sample.extend(ordered[:per_stratum])
    return sample


def build_report(per_stratum: int) -> dict[str, Any]:
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            """SELECT a.article_id, a.title, a.raw_text, a.summary AS source_summary,
                      a.language, a.source_id, a.url, a.published_at, a.raw,
                      COALESCE(s.factual_summary,'') AS factual_summary,
                      COALESCE(s.summary_status,'') AS summary_status,
                      COALESCE(s.quality_status,'') AS quality_status,
                      COALESCE(s.quality_reasons,'[]') AS quality_reasons,
                      COALESCE(s.input_quality,'') AS input_quality,
                      COALESCE(s.impact_analysis_status,'') AS impact_analysis_status,
                      COALESCE(s.attempts,0) AS attempts,
                      COALESCE(s.error,'') AS error
               FROM news_articles a LEFT JOIN event_ai_summaries s USING(article_id)
               ORDER BY a.article_id"""
        ).fetchall()
    audited = [audit_row(dict(row)) for row in rows]
    pipeline_counts = Counter(pipeline_state(dict(row)) for row in rows)
    issues = Counter(issue for record in audited for issue in record["issues"])
    statuses = Counter(record["status"] for record in audited)
    failures = Counter(record["failure_cluster"] for record in audited if record["failure_cluster"])
    input_grades = Counter(record["input_grade"] for record in audited)
    disclaimer_count = sum(bool(record["disclaimer_hits"]) for record in audited)
    relevant_count = sum(record["project_relevance"]["relevant"] for record in audited)
    structured_count = sum(
        record["fact_structure"]["has_actor"] and record["fact_structure"]["has_action"] for record in audited
    )
    completed = [record for record in audited if record["status"] == "completed"]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "read_only_no_provider_calls",
        "population": len(audited),
        "status_counts": dict(statuses),
        "completed_quality": {
            "count": len(completed),
            "clean": sum(not r["issues"] for r in completed),
            "flagged": sum(bool(r["issues"]) for r in completed),
        },
        "issue_counts": dict(issues),
        "failure_clusters": dict(failures),
        "pipeline_funnel": {
            "counts": dict(pipeline_counts),
            "coverage_ratio": round(pipeline_counts["ready"] / max(1, len(rows)), 4),
            "action_required": len(rows) - pipeline_counts["ready"],
        },
        "input_grade_counts": dict(input_grades),
        "quality_metrics": {
            "average_information_density": round(
                sum(record["information_density"] for record in audited) / max(1, len(audited)), 4
            ),
            "disclaimer_pollution_count": disclaimer_count,
            "structured_fact_count": structured_count,
            "project_relevant_count": relevant_count,
        },
        "thresholds": {"min_chars": 24, "max_chars": 600, "min_traceability_overlap": 0.12},
        "stratified_sample": stratified_sample(audited, per_stratum),
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 事件摘要质量审计",
        "",
        f"生成时间：{report['generated_at']}",
        "",
        "本报告只读数据库，未调用 DeepSeek 或其他外部模型。",
        "",
        "## 总览",
        "",
    ]
    lines += [
        f"- 审计记录：{report['population']}",
        f"- 状态：`{json.dumps(report['status_counts'], ensure_ascii=False)}`",
        f"- 已完成质量：`{json.dumps(report['completed_quality'], ensure_ascii=False)}`",
        f"- 问题计数：`{json.dumps(report['issue_counts'], ensure_ascii=False)}`",
        f"- 失败原因聚类：`{json.dumps(report['failure_clusters'], ensure_ascii=False)}`",
        "",
        "## 分层抽样",
        "",
    ]
    for item in report["stratified_sample"]:
        lines += [
            f"### {item['article_id']} · {item['status']} · {item['language']}",
            "",
            f"- 标题：{item['title']}",
            f"- 摘要：{item['summary'] or '（空）'}",
            f"- 字符数：{item['summary_chars']}",
            f"- 可追溯词元重合率：{item['traceability_overlap']:.2%}",
            f"- 问题：{', '.join(item['issues']) or '无'}",
            f"- 失败分类：{item['failure_cluster'] or '不适用'}",
            "",
        ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only quality audit for DeepSeek event summaries.")
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--sample-per-stratum", type=int, default=3)
    args = parser.parse_args()
    report = build_report(max(1, min(args.sample_per_stratum, 20)))
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output_md.write_text(markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("population", "status_counts", "completed_quality", "issue_counts", "failure_clusters")
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
