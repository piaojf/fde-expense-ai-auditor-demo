# M2 Contract 变更提案：住宿超标的显式人工例外审批

## 最终架构决定（2026-10-07）

STATUS: REJECTED / NOT REQUIRED

The M2 scenario was invalid.
Human Review must not override a deterministic BLOCK/FAIL.
The scenario has been redesigned to exercise an ambiguity path instead.

冻结 API Contract v1.0.0 保持不变。下文保留为已否决的历史设计证据，
不构成实施授权；其中的 v1.1.0、exception_approval 与酒店硬规则例外方案均不实施。
新场景采用 680 <= 700、所有硬规则通过，人工仅解决住宿日期与出差审批期间的业务歧义。

日期：2026-10-07（Asia/Shanghai）  
状态：DRAFT / 待审批；触发用户指定的 Contract Gate，M2 实现 STOP。  
当前公开 Contract：v1.0.0，保持原文、Schema、OpenAPI 和后端行为。  
M1_CHECKPOINT=PASS  
commit：`6dfc3c09e7af87935f47cd56c880f61e472ae159`  
tag：`m1-pass`

## 1. 结论与精确阻塞点

现有 HumanReviewRequest / ResumeRequest 足以表达普通 MEDIUM 审核的
interrupt/resume。阻塞点不是 LangGraph 或路由，而是**本次指定酒店例外的决策语义**。

| 项目 | 本次 M2 验收要求 | 冻结契约及现有实现 |
|---|---|---|
| 2026 深圳住宿 | 780.00 > 700.00，HOTEL_CITY_LIMIT 保留 FAIL | `rules/travel_rules.py::hotel_city_limit` 返回 BLOCK/FAIL |
| 首次决策 | MEDIUM / HUMAN_REVIEW / requires_human=true | Contract §4：BLOCK/FAIL 只允许 REJECT；Risk Engine 返回 HIGH |
| 人工 APPROVE | 保留真实超标事实，批准特殊报销并写 ERP APPROVED | Contract §4：人工不得绕过 BLOCK；APPROVE 禁止 REVIEW/FAIL 或 UNKNOWN |
| 财务例外权限 | 航班取消材料有效时，人工承担例外决策 | `docs/decision-policy.md` 将财务例外审批列为后续新流程；当前没有该授权记录语义 |

复现使用真实现有酒店规则和 DecisionResult 校验器，但输入是隔离的合成 DTO，
**不是已创建、已 interrupt 或已执行的 CLM-2026-00004 工作流**。
复现结果：

```text
HOTEL_CITY_LIMIT: actual=780.00 limit=700.00 outcome=FAIL severity=BLOCK policy=2026
CURRENT_RISK=HIGH; CURRENT_ROUTE=REJECT
REQUESTED_INTERRUPT_RESULT=VALIDATION_REJECTED; Value error, BLOCK / FAIL cannot be overridden
REQUESTED_HUMAN_APPROVAL=VALIDATION_REJECTED; Value error, BLOCK / FAIL cannot be overridden
REVIEW_FAIL_APPROVAL=VALIDATION_REJECTED; Value error, Approval requires LOW and complete authoritative evidence
CONTRACT_COMPLIANT_RESULT=HIGH/REJECT
```

仅将 BLOCK 改为 REVIEW 仍不满足批准校验；清空失败规则、把 780 改为 700、
伪造 PASS、替换有效制度或绕过 DTO 校验都不是契约兼容的实现。
因此保留 M1 后端，只新增本提案和本次证据。

## 2. 不构成核心阻塞的命名与条件要求

- `WAITING_HUMAN` 可在说明和验收中映射到公开的 `PENDING_HUMAN`，不新增状态枚举。
- `HumanReviewRequest` 已含 reviewer_id、comment、run_id、expected_revision、idempotency_key。
- `ResumeRequest` 已含 run_id、expected_revision、idempotency_key；服务器决定 checkpoint 和节点。
- Contract 仅支持 APPROVE / REJECT；本次条件性的 REQUEST_MORE_INFO 路径不实施。
  当前也没有 WAITING_INFO。以后引入时须另行审批动作与状态。
- AuditEvent 使用冻结的 event_type / occurred_at / payload；可在 payload 内记录
  node/status/summary/reviewer/action/reason，无需重命名顶层字段。
- 内部 workflow_id 可复用 run_id；thread/checkpoint/interrupt/resume 元数据可置于
  ClaimState.audit_context，human_review 复用已有字段。公开 DTO 不必暴露 LangGraph 内部对象。

## 3. 供审批的最小语义变更（尚未实施）

建议目标版本 v1.1.0；最终版本、权限边界及变更内容由 Contract 负责人审批。

### 3.1 明确人工例外权限，而不是自动豁免

1. 明确列举可人工例外审核的规则和场景：本阶段仅 HOTEL_CITY_LIMIT 与航班取消导致延住。
2. 发布受控的财务例外审批依据，明确审核材料及 Demo reviewer 的职责；
   航班取消说明本身不生成授权，也不被模型认定为自动豁免。
3. 仅命中已批准的例外资格时，由确定性代码送 MEDIUM/HUMAN_REVIEW；
   原规则仍保留 FAIL、780.00、700.00、2026 制度版本及原始 BLOCK 分类。
4. 硬门失败、未列入可例外范围的 BLOCK、缺乏有效制度、身份和真实性相关阻断继续 REJECT。
   普通超标（例如现有 CLM-2026-001）保持 HIGH/REJECT 回归行为。
5. 人工 APPROVE 必须有明确的已授权例外记录；人工 REJECT 保持最终优先级。
   模型仅提供分析，不拥有例外批准权。

### 3.2 DecisionResult 的显式例外记录

建议新增 nullable `exception_approval`（名称和结构待审批），仅在合法人工例外批准时非 null：

```json
{
  "rule_ids": ["HOTEL_CITY_LIMIT"],
  "reviewer_id": "finance_reviewer_01",
  "reason": "航班取消证明有效，批准特殊报销。",
  "reviewed_at": "<带时区的实际审核时间>",
  "document_ids": ["<该 run 中已核实的取消通知 document_id>"],
  "authority_reference": "<经审批发布的例外权限依据 ID/版本>"
}
```

上例是草案，不是当前有效 API payload，也不是已有证据或授权。
审批校验需核对 rule_ids、review、附件及 authority_reference 均来自同一已持久化 run，
禁止用任意 comment 或 reviewer_id 字符串单独解除规则。
保留初审 MEDIUM 和全部原始规则事实；最终风险采用何种枚举以及与原始风险的关系
需在 Contract 中明确。若最终采用 LOW/APPROVE，LOW 表示治理审批后的剩余风险，
不表示 780.00 已变成符合 700.00 的 PASS。
Contract、DecisionResult 校验器、Decision Policy、OpenAPI 和测试必须同批更新。

### 3.3 保留两个现有写接口

继续使用以下路由和当前 request 字段，不新建隐式批准接口：

```text
POST /claims/{claim_id}/human-review  -> 200，保存审核意见，不隐式 resume
POST /claims/{claim_id}/resume        -> 202，同 run/thread 的服务器 checkpoint 恢复
```

审批后明确初审 revision、提交意见 revision、恢复 revision 及非法转换响应。
claim + operation + idempotency_key + payload 为重放边界；不同 operation 互不误命中。
首次审核、恢复完成、重复请求及重启后的重放都必须由测试验证。

## 4. 审批后 M2 实施与验收路线（本轮未执行）

- 使用真实 LangGraph interrupt 和持久化 checkpointer；恢复使用同一 thread/run 的
  Command(resume=已校验的持久化审核意见)，不重新从 START 跑图。
- interrupt 所在节点只执行可重放的纯操作；中断前的 ERP context、document parse、
  policy retrieval 不因 resume 重复执行，人工相关审计事件去重。
- 初审停在 PENDING_HUMAN/HUMAN_REVIEW，ERP=NOT_STARTED；保留 checkpoint 身份和原因。
- Case 4 重启真实 Uvicorn 进程后提交 APPROVE，再 resume，最终 COMPLETED/APPROVED。
- Case 5 提交 REJECT，再 resume，最终 COMPLETED/REJECTED。
- 验证未审核先 resume、非中断 claim 提交审核、错误 run/revision、重复 human-review、
  重复 resume、同键异 payload，以及人类决定不被后续 LLM 覆盖。
- 记录 HUMAN_REVIEW_REQUIRED、WORKFLOW_INTERRUPTED、HUMAN_REVIEW_SUBMITTED、
  WORKFLOW_RESUMED、HUMAN_APPROVED/HUMAN_REJECTED、ERP_UPDATED、WORKFLOW_COMPLETED。
- 在隔离副本回滚到 m1-pass 并复验 M1；不涉及 WorkBuddy 目录，不进入 M3。

## 5. 本轮实际验证与保留状态

- M1 tests：99 passed、1 skipped、0 failed；Ruff PASS。
- 真实 Uvicorn M1 Happy Path、服务重启持久化、same-key replay、12 条审计日志 PASS。
- M1 的 human-review/resume 仍是 501；Case 4/5 尚未添加。
- 冻结 Contract、Decision Policy、Schemas、OpenAPI 和业务代码保持原状。
- 完整命令、字面输出、hash、HTTP 响应与隔离回滚记录见：
  `D:\FDE-Expense-Auditor\.m2-evidence\VERIFICATION.txt`。
- 这次回滚验证的是“新增提案 → 回滚 → M1”，不是尚未实现的 M2 interrupt 工作流。

## 6. 审批后可复用的继续开发指令

```text
继续 D:\FDE-Expense-Auditor 的 M2，只以已审批的 api-contract-change-proposal.md
及确认后的目标 Contract 版本为准。先保护 WorkBuddy 与未知改动，复验 m1-pass。
完成显式人工例外决策语义、真实持久化 LangGraph interrupt/resume、Case 4/5、
进程重启、人工优先级、revision、operation-scoped 幂等、完整审计和隔离回滚。
保持失败规则事实与授权例外可追溯，不自动豁免，不扩大认证、真实 LLM/pgvector
或 M3 范围。Contract 尚未批准时保留 STOP。
```
