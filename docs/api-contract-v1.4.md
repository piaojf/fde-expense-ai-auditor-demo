# v1.4 本机受控制度规则确认与发布

v1.0 / v1.1 / v1.2 / v1.3 的 DTO、路由定义与 OpenAPI 保持不变。
v1.4 独立挂载 `/api/v1.4`，默认关闭，不进入根 OpenAPI。
所有接口为 POST，沿用 v1.3 的直接 loopback peer / Host / Origin / 禁止转发头及
至少 32 字符 `token` 能力凭证。token 属于 SecretStr，不入审计、摘要、错误响应。
本机审计 actor 是标签，不是企业身份认证；没有生产 RBAC 或双人审批。

| 路径（前缀 /api/v1.4/policies） | 输入（均带 token） | 行为 |
| --- | --- | --- |
| /history | token | 已发布版本、追加式发布/退休/回退审计 |
| /{upload_id}/rules | token | 原始提取候选、来源、全部人工审核修订、关联发布 |
| /{upload_id}/confirm-rule | actor, reason, confirmed=true, amount decimal string, currency=CNY, rule_type=HOTEL_LIMIT_PER_NIGHT, clause_start/end, expected_revision | 追加人工确认，不修改原始提取 |
| /{upload_id}/publication-preview | predecessor_id 或 null | 当前规则、旧/新版本、日期、金额差异、影响范围、索引身份、缺失/冲突及 preview_digest |
| /{upload_id}/publish | preview 参数 + actor, confirmed=true, preview_digest, idempotency_key | 重验证并在事务中创建 ACTIVE 版本、chunks、规则、替代关系、审计 |
| /{policy_id}/lifecycle-preview | kind=RETIRE/ROLLBACK, effective_on, restore_id（回退必需） | 未来退休或回退差异与 preview_digest |
| /{policy_id}/lifecycle | preview 参数 + actor, reason, confirmed=true, preview_digest, idempotency_key | 追加未来退休；或复制历史快照新建未来回退版本 |

日期为 ISO date，区间为 `[from, to)`。金额为最多两位小数、正值的 decimal string，
内部 Decimal；不接受 JSON float。只支持明确地区 HOTEL、CNY，固定 authority_level=100。
公司、地区、费用、版本和日期必须与原文可验证标签一致；人工修正值和说明保留，无法验证不发布。
规则金额必须与选定具体原文条款一致，UNKNOWN、低置信度或多个冲突限额均拒绝。

preview 冲突/缺失时返回 `blockers` 与 `message`，无有效发布 digest；不会改变任何状态。
发布必须命名唯一重叠的旧版本，范围一致且新生效日严格晚于旧开始日；已安排后继、重复版本、
同权威冲突、跨公司、伪造来源、模型/维度/向量不一致均 409。所有新变更必须从本机上海时区未来日期开始。
使用实际检索 Provider 的模型名和维度检查全部向量元数据，不发起 HTTP 调用来完成发布检查。
只有 APP_ENV=test 可发布 deterministic test-only 索引；真实 Provider 与其不匹配时拒绝。

成功响应含服务器生成的发布 event_id / policy_version_id、版本、规则摘要、actor、时间与前驱。
发布前预览必须与事务内重新计算的摘要相同；过时预览 409。提交键与请求摘要持久化绑定，
完全相同请求返回原结果（包括重启后），不同请求复用键 409；再次发布同一草稿的新键 409。
数据库异常 503，整笔事务回滚，客户端必须使用同一提交键重试。

旧 ACTIVE 正文、authoritative_rules、向量与区间不覆盖；发布从 DRAFT 创建 PUB-* 快照。
原上传仍为 v1.3 DRAFT/INDEXED，已发布关联来源受保护，不允许删除。
数据库触发器保护审计、审核修订、曾使用的制度/向量和已发布原始上传，防止历史修改/删除。
`ACTIVE` 表示已发布权威快照，真正适用仍由业务日期、审核开始时间与追加式 cutover 决定。

RETIRE 在未来切断适用范围，保留旧版本并阻止降级到旧 FAQ；之后安全 UNKNOWN。
ROLLBACK 校验同范围、较早权威快照的原文摘要、条款规则、模型和向量，再创建 ROLL-* 新版本；
不重新利用旧版本 ID，不物理删除，不改变已完成或正在复核的原始审核依据。

本机应用须显式迁移 0004；启动不会 create_all。此次仅迁移新的 fde_m0_test_* 数据库。
不迁移/发布真实演示库，不调用付费 BGE-M3 / DeepSeek，不提供真实 ERP 或付款能力。
