import { useEffect, useState } from "react";
import { Bot, Send } from "lucide-react";
import type { RagSearchResponse } from "../services/api";
import { api } from "../services/api";
import { evidenceLevelLabel } from "./displayLabels";
import { EvidenceList } from "./domain";
import { Button, EmptyState, Panel, StatusPill } from "./ui";

const defaultQuestion = "请根据当前证据链，说明 POY/DTY 上游成本压力的主要驱动、反证和数据缺口。";
const suggestedQuestions = [
  "今天 POY/DTY 上游成本压力是增强、减弱还是观望？请给出依据和反证。",
  "当前最可能误导判断的新闻或数据缺口是什么？",
  "如果只看未来 7 天，原油、PX、PTA、MEG 哪一段最值得关注？"
];

export function AssistantConsole({
  seed,
  onRetrieved
}: {
  seed?: string;
  onRetrieved: (retrieval: RagSearchResponse) => void;
}) {
  const [question, setQuestion] = useState(seed || defaultQuestion);
  const [answer, setAnswer] = useState("");
  const [status, setStatus] = useState("待提问");

  useEffect(() => {
    if (seed) setQuestion(seed);
  }, [seed]);

  const ask = async () => {
    if (!question.trim()) return;
    setStatus("检索证据中");
    setAnswer("");
    try {
      const evidence = await api.knowledgeRetrieval(question, 8);
      onRetrieved(evidence);
      setStatus("生成回答中");
      await api.chatStream(question, (chunk) => {
        setAnswer((current) => `${current}${chunk}`);
      });
      setStatus("已完成");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "请求失败");
    }
  };

  return (
    <div className="assistant-console">
      <label>
        <span>问题</span>
        <textarea
          aria-label="AI 流式问题"
          onChange={(event) => setQuestion(event.target.value)}
          value={question}
        />
      </label>
      <div className="assistant-question-templates">
        {suggestedQuestions.map((item) => (
          <button key={item} onClick={() => setQuestion(item)} type="button">{item}</button>
        ))}
      </div>
      <div className="form-actions">
        <Button icon={<Send size={16} />} onClick={() => void ask()} tone="accent">发送</Button>
        <StatusPill tone={status === "已完成" ? "good" : status.includes("失败") ? "bad" : "accent"}>{status}</StatusPill>
      </div>
      <div className="assistant-answer">
        {answer ? <p>{answer}</p> : <EmptyState title="等待回答">助手会先读取本地证据，再输出事实、推断和反证。</EmptyState>}
      </div>
    </div>
  );
}

export function RetrievalPanel({ retrieval }: { retrieval: RagSearchResponse | null }) {
  if (!retrieval) {
    return (
      <EmptyState title="尚未检索">
        点击发送后，这里会显示本次回答可引用的文档、来源等级和风险标记。
      </EmptyState>
    );
  }

  return (
    <>
      <div className="queue-summary">
        <StatusPill tone="accent">{evidenceLevelLabel(retrieval.evidence_level)}</StatusPill>
        <span>置信度 {Math.round(retrieval.confidence * 100)}%</span>
        <span>文档 {retrieval.documents.length} 条</span>
      </div>
      <EvidenceList items={retrieval.documents} />
      {retrieval.warnings.length ? (
        <div className="warning-stack">
          {retrieval.warnings.map((warning) => <span key={warning}>{warning}</span>)}
        </div>
      ) : null}
    </>
  );
}

export function AssistantRulesPanel() {
  return (
    <Panel eyebrow="回答边界" title="回答边界">
      <div className="assistant-rules">
        <Bot size={18} />
        <p>回答必须区分事实、推断和反证；C/D 级材料只能作为弱信号；缺失数据会明确保留为缺口。</p>
      </div>
    </Panel>
  );
}
