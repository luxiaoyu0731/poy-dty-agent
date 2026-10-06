import type { PipelineImplementationProfile } from "../services/api";

export function AgentImplementationProfile({ profile }: { profile?: PipelineImplementationProfile }) {
  return <div className="agent-implementation-profile" data-testid="agent-implementation-profile">
    <h3>Agent 组成与执行边界</h3>
    {!profile ? <p>当前接口未提供该节点的提示词与执行配置。职责说明不能替代真实配置，也不能还原当次调用。</p> : <>
      <p className="implementation-basis">{profile.basis}</p>
      <dl>
        <div><dt>模型配置</dt><dd>{profile.configured_model}<small>{profile.model_source}</small></dd></div>
        <div><dt>上下文与记忆</dt><dd>{profile.context}</dd></div>
        <div><dt>工具与调度</dt><dd>{profile.tools}</dd></div>
        <div><dt>输出与核验</dt><dd>{profile.output}</dd></div>
        <div><dt>预算与失败路径</dt><dd>阶段上限 {profile.stage_cap} 次。{profile.fallback}</dd></div>
      </dl>
      <details className="pipeline-contract-record"><summary>系统提示词 · 当前代码原文</summary>
        <p>版本 {profile.prompt_version}</p><pre>{profile.system_prompt}</pre>
        <small>来源 {profile.source}<br />SHA-256 {profile.prompt_sha256}</small>
      </details>
      <p className="implementation-boundary">{profile.historical_request}</p>
    </>}
  </div>;
}
