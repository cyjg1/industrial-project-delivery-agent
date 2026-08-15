import { Collapse, Descriptions, Drawer, Empty, Tag, Typography } from "antd";
import type { ChatMessage } from "../lib/ui";
import type { Workspace } from "../types";

type DetailDrawerProps = {
  open: boolean;
  workspace: Workspace;
  latestMessage?: ChatMessage;
  onClose: () => void;
};

export function DetailDrawer({ open, workspace, latestMessage, onClose }: DetailDrawerProps) {
  const latestAudit = workspace.context.audit_trail[workspace.context.audit_trail.length - 1];
  const latestModelIo = workspace.context.model_io_events[workspace.context.model_io_events.length - 1];
  const memoryRetrievalSteps = (latestMessage?.toolSteps || []).filter(
    (step) => step.tool_name === "search_memory" && step.status === "completed",
  );

  return (
    <Drawer title="调试与审计" width={560} open={open} onClose={onClose} className="detail-drawer">
      <Descriptions column={1} size="small" bordered>
        <Descriptions.Item label="运行状态">{workspace.run_status.status}</Descriptions.Item>
        <Descriptions.Item label="候选">{workspace.memory.candidate_count}</Descriptions.Item>
        <Descriptions.Item label="确认">{workspace.memory.confirmed_count}</Descriptions.Item>
        <Descriptions.Item label="存储">{workspace.storage.store_dir}</Descriptions.Item>
      </Descriptions>

      <Collapse
        className="drawer-collapse"
        items={[
          {
            key: "model-rounds",
            label: `逐轮模型轨迹${latestMessage?.modelRounds?.length ? `（${latestMessage.modelRounds.length} 轮）` : ""}`,
            children: latestMessage?.modelRounds?.length ? (
              <div className="debug-blocks">
                {latestMessage.modelRounds.map((round) => (
                  <section className="debug-block" key={`model-round-${round.round_index}`}>
                    <div className="debug-round-heading">
                      <strong>第 {round.round_index} 轮</strong>
                      <Tag>{round.status || "未记录"}</Tag>
                    </div>
                    <Descriptions column={1} size="small" bordered>
                      <Descriptions.Item label="模型">{round.model || round.provider || "未记录"}</Descriptions.Item>
                      <Descriptions.Item label="输入">
                        {`${round.input_message_count} 条消息 · 约 ${round.input_token_count} tokens · ${round.input_char_count} 字符`}
                      </Descriptions.Item>
                      <Descriptions.Item label="模型输出">
                        {`约 ${round.output_token_count} tokens · ${round.output_char_count} 字符 · ${round.model_elapsed_ms} ms`}
                      </Descriptions.Item>
                      <Descriptions.Item label="工具选择">
                        {round.tool_calls.length
                          ? round.tool_calls.map((call) => `${call.name}${call.reason ? `：${call.reason}` : ""}`).join("；")
                          : "本轮直接提出回答"}
                      </Descriptions.Item>
                      {round.blocked_tool_calls?.length ? (
                        <Descriptions.Item label="被阻止的工具">
                          {round.blocked_tool_calls.map((call) => call.name).join("；")}
                        </Descriptions.Item>
                      ) : null}
                      <Descriptions.Item label="验证">
                        {round.verification_errors.length ? round.verification_errors.join("；") : "无拒绝记录"}
                      </Descriptions.Item>
                      {round.context_compaction ? (
                        <Descriptions.Item label="恢复压缩">
                          {`${round.context_compaction.before_token_count} → ${round.context_compaction.after_token_count} tokens；重写轮禁止工具：${round.context_compaction.tools_disabled_for_retry ? "是" : "否"}`}
                        </Descriptions.Item>
                      ) : null}
                    </Descriptions>
                    <DebugBlock title="本轮模型原始输出" value={round.model_output || "未记录"} />
                    {round.tool_calls.map((call, index) => (
                      <DebugBlock
                        key={call.call_id || `${round.round_index}-call-${index}`}
                        title={`工具输入 · ${call.name}`}
                        value={JSON.stringify(call.arguments || {}, null, 2)}
                      />
                    ))}
                    {round.tool_results.map((toolResult, index) => (
                      <DebugBlock
                        key={toolResult.call_id || `${round.round_index}-result-${index}`}
                        title={`工具结果 · ${toolResult.name} · ${toolResult.elapsed_ms} ms`}
                        value={JSON.stringify({
                          status: toolResult.status,
                          summary: toolResult.summary,
                          result: toolResult.result,
                        }, null, 2)}
                      />
                    ))}
                  </section>
                ))}
                <Descriptions column={1} size="small" bordered>
                  <Descriptions.Item label="停止原因">{latestMessage.stopReason || "未记录"}</Descriptions.Item>
                  <Descriptions.Item label="事实校验">
                    {latestMessage.verification?.unsupported?.length
                      ? latestMessage.verification.unsupported.join("；")
                      : "通过"}
                  </Descriptions.Item>
                  <Descriptions.Item label="引用覆盖">
                    {latestMessage.verification?.citation_coverage
                      ? `${latestMessage.verification.citation_coverage.cited_claim_count}/${latestMessage.verification.citation_coverage.claim_count}`
                      : "未记录"}
                  </Descriptions.Item>
                </Descriptions>
              </div>
            ) : (
              <Empty description="暂无逐轮模型轨迹" />
            ),
          },
          {
            key: "model",
            label: "模型 API 输入输出",
            children: latestMessage ? (
              <div className="debug-blocks">
                <Descriptions column={1} size="small" bordered>
                  <Descriptions.Item label="实际模型">{latestMessage.model || latestMessage.provider || "未记录"}</Descriptions.Item>
                  <Descriptions.Item label="项目上下文 Token 数">{latestMessage.contextTokenCount ?? "未记录"}</Descriptions.Item>
                  <Descriptions.Item label="项目上下文字符数">{latestMessage.contextCharCount ?? "未记录"}</Descriptions.Item>
                  <Descriptions.Item label="项目上下文预算">
                    {latestMessage.contextBudget === undefined
                      ? "未记录"
                      : `${latestMessage.contextBudget} ${latestMessage.contextBudgetUnit || "tokens"}`}
                  </Descriptions.Item>
                  <Descriptions.Item label="Token 计数器">{latestMessage.contextTokenCounter ?? "未记录"}</Descriptions.Item>
                  <Descriptions.Item label="系统自动检索">
                    {latestMessage.contextRetrievalCount === undefined
                      ? "未记录"
                      : `${latestMessage.contextRetrievalCount} 条`}
                  </Descriptions.Item>
                  <Descriptions.Item label="检索降级">
                    {latestMessage.contextDegraded === undefined ? "未记录" : latestMessage.contextDegraded ? "是" : "否"}
                  </Descriptions.Item>
                </Descriptions>
                <DebugBlock title="context_bundle" value={(latestMessage.contextBundle || []).join("\n")} />
                <DebugBlock title="API 输入" value={latestMessage.modelInput || latestModelIo?.request.input || "未记录"} />
                <DebugBlock
                  title="API 输出"
                  value={latestMessage.modelOutput || latestModelIo?.responses.map((item) => item.output_text).join("\n\n") || "未记录"}
                />
              </div>
            ) : (
              <Empty description="暂无对话调试记录" />
            ),
          },
          {
            key: "memory-retrieval",
            label: "记忆检索轨迹",
            children: memoryRetrievalSteps.length ? (
              <div className="debug-blocks">
                {memoryRetrievalSteps.map((step, index) => {
                  const trace = step.result || {};
                  const embeddingIndex = asRecord(trace.embedding_index);
                  const degradationReasons = Array.isArray(trace.degradation_reasons)
                    ? trace.degradation_reasons.join("、")
                    : "";
                  return (
                    <section className="debug-block" key={step.call_id || `memory-retrieval-${index}`}>
                      <Descriptions column={1} size="small" bordered>
                        <Descriptions.Item label="查询">{String(trace.query || step.arguments.query || "未记录")}</Descriptions.Item>
                        <Descriptions.Item label="检索模式">{String(trace.retrieval_mode || "未记录")}</Descriptions.Item>
                        <Descriptions.Item label="权限范围先过滤">
                          {trace.access_scope_applied === true ? "是" : trace.access_scope_applied === false ? "否" : "未记录"}
                        </Descriptions.Item>
                        <Descriptions.Item label="可见候选">{formatCount(trace.visible_candidate_count)}</Descriptions.Item>
                        <Descriptions.Item label="FTS 候选">{formatCount(trace.fts_candidate_count)}</Descriptions.Item>
                        <Descriptions.Item label="语义候选">{formatCount(trace.semantic_candidate_count)}</Descriptions.Item>
                        <Descriptions.Item label="并集候选">{formatCount(trace.union_candidate_count)}</Descriptions.Item>
                        <Descriptions.Item label="线程折叠后">{formatCount(trace.thread_collapsed_count)}</Descriptions.Item>
                        <Descriptions.Item label="最终返回">{formatCount(trace.returned_count)}</Descriptions.Item>
                        <Descriptions.Item label="Embedding 索引">
                          {`indexed=${formatScalar(embeddingIndex.indexed)}, pending=${formatScalar(embeddingIndex.pending)}, error=${formatScalar(embeddingIndex.error)}, complete=${formatScalar(embeddingIndex.complete)}`}
                        </Descriptions.Item>
                        <Descriptions.Item label="降级原因">{degradationReasons || "无"}</Descriptions.Item>
                      </Descriptions>
                      <DebugBlock title="检索原始诊断" value={JSON.stringify(trace, null, 2)} />
                    </section>
                  );
                })}
              </div>
            ) : (
              <Empty description="本轮没有调用记忆检索" />
            ),
          },
          {
            key: "audit",
            label: "审计轨迹",
            children: latestAudit ? (
              <div className="debug-blocks">
                <DebugBlock title="发给模型/工具" value={latestAudit.sent} />
                <DebugBlock title="拼接内容" value={latestAudit.assembled_context.join("\n") || "未记录"} />
                <DebugBlock title="返回内容" value={latestAudit.returned || "未记录"} />
              </div>
            ) : (
              <Empty description="暂无审计轨迹" />
            ),
          },
          {
            key: "json",
            label: "原始 JSON",
            children: <pre className="json-view">{JSON.stringify(workspace, null, 2)}</pre>,
          },
        ]}
      />
      <Typography.Paragraph type="secondary" className="detail-drawer-note">
        默认不展示调试信息；只有点击左侧导航底部的「调试」才展开。
      </Typography.Paragraph>
    </Drawer>
  );
}

function DebugBlock({ title, value }: { title: string; value: string }) {
  return (
    <section className="debug-block">
      <strong>{title}</strong>
      <pre>{value || "未记录"}</pre>
    </section>
  );
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function formatScalar(value: unknown): string {
  return value === undefined || value === null || value === "" ? "未记录" : String(value);
}

function formatCount(value: unknown): string {
  return value === undefined || value === null ? "未记录" : `${String(value)} 条`;
}
