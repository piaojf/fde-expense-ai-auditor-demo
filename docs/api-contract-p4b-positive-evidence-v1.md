# P4B Positive Evidence Contract v1

适用范围：P4B-FIX离线修复。原制度唯一来源为222.docx，SHA-256 `65043838143a8d043130881b8fda35fd2573ed7fcd8716f1a33fc72f32bf1a98`。P4A、旧Expense DTO、既有财务批准和ERP幂等协议不变。新契约版本为 `p4b.positive.1`，作为P4B报告的可选 `positive_evidence` 增量字段保留；历史报告未自动迁移。

## 状态和来源

| 状态 | 含义 | 能否作为已核实合规依据 |
|---|---|---|
| VERIFIED | 服务端从可信主数据、已发布制度、原件完整性或有权人的独立核验记录构造；含来源、核验人、时间和范围 | 可以在记录范围内使用；结果本身仍可能是规则FAIL |
| DECLARED | 员工说明或候选事实 | 不可单独支撑COMPLIANT |
| MISSING | 值缺失，或缺少必要来源/核验信息 | 补证；不补猜 |
| CONFLICTED | 候选绑定、来源版本、核验范围等存在冲突，或必要事实被核验为否 | 处理冲突；不能自动放行 |

每项包含 `evidence_id/status/value/source_reference/verified_by/verified_at/scope`。只有后端构造证据状态；SemanticAnalysis中没有写这些字段的权限。所有VERIFIED的来源、核验人、时间、范围和值必填。`FACT:<claim_id>:<key>` 为本次事实证据ID；模型的制度 `evidence_refs` 仍仅能用实际传入的原文chunk_id，事实通过实际 `positive.<key>` 路径引用。

共用 `scope` 包括申请ID、审核版本、候选内容SHA摘要、费用日期及出差范围。为避免重复大对象占满既有32KB边界，逐项scope通过 `contract_scope=trusted.positive_scope` 指向请求中的共用范围；主数据和制度再带有效日期，人工事实再带最初核验版本及依据。报告的共用范围保存在 `positive_evidence.scope`；请求中保存在 `facts.trusted.positive_scope`。核验时间记录的是实际核验时间，不把合成费用日期伪装成核验日期。

## 逐项契约

| 项 | 来源与核验权限 | 有效范围/限制 |
|---|---|---|
| employee、grade | 认证申请所属员工与P4A已留痕HR主数据，原管理员及录入时间 | 原主数据有效期覆盖实际行程；未知职级不升级 |
| city_tier | P4A有效城市主数据 | 原城市、有效日期及明确别名 |
| expense_type、category_mapping | 服务端绑定的P4B提交或P4A住宿适配器 | 验证业务类别与旧存储类别的绑定，不声称仅凭类别字段证明线下交易真实性 |
| policy_version | 已有权威发布/有效期核验及不可变人工范围确认，222.docx原文SHA | 本申请费用日期和制度版本；DRAFT、过期、冲突不可用 |
| rule_results | 确定性规则引擎现成输出 | 数值、规则版本原样传递；结果VERIFIED不等于outcome=PASS |
| business_related | 独立FINANCE/ADMIN核对业务材料 | 当前申请内容/摘要和业务范围；员工自述不可自证 |
| prior_approval | 独立FINANCE/ADMIN核验本申请范围内有效事前业务授权 | 不等于财务最终批准/支付权限；特殊例外授权另核实 |
| duplicate_clear | 独立FINANCE/ADMIN核验重复/拆单风险 | 本申请当前事实及历史记录范围 |
| invoice_verified | 独立FINANCE/ADMIN核验合法交易票据、金额/日期/购买方/销售方/发票号码等制度要求 | 表示核验覆盖这些要求，原始敏感值无需重复披露给模型；发现具体冲突仍须处理 |
| original_invoice | 已绑定原始票据文件SHA与服务端实时完整性检查 | 只证明绑定和字节完整性，不把OCR或员工确认升级为财务核验 |
| full_days/lunches/dinners | 独立核实的实际整日公务、批准行程和供餐记录 | 本轮只支持整天范围；候选餐次和员工notes不参与最终金额计算 |
| participants/customer_identified/entertainment_category | 独立核对客户、名单、人数和招待类别 | 本轮确定性金额只支持GENERAL |
| cancellation_notice/itinerary_verified/exception_authorized | 独立核验取消通知、行程、特殊例外有效授权 | 只要存在例外仍须人工复核；不能覆盖硬禁令 |

核验API保持原请求字段，追加的逐字段留痕由服务端自动维护。部分核验只更新实际提交字段的actor/time/source/basis；其余字段保留原记录，不把新来源冒充成旧事实来源。旧汇总快照缺少逐字段留痕时不自动升级，需有权限的人重新核实必要字段。新审核仅增加审核版本且内容摘要完全相同，保留原核验并记录attested_claim_version；内容变化、来源冲突、票据变化或后续核验导致旧报告失效。

## 四个Demo真正必需的证据

共同必需：可信员工/职级/城市、准确服务端类型映射、有效发布版本和范围确认、实际规则结果、独立核验的业务说明、有效事前业务授权、去重核验。共同完整也不能绕过金额或Hard Gate。

| Demo | 特有材料 | 不足时行为 |
|---|---|---|
| D/深圳/一晚850住宿 | 已核实入住/酒店订单/晚数、绑定原发票、财务票据核验 | 限额800、规则FAIL；即使全部补齐也不能批准 |
| 航班取消额外住宿680 | 上述住宿材料，再加取消通知、交通订单/行程、负责人说明和有效例外授权 | 缺证要求补证并由人工处理；金额PASS不能自动批准 |
| 整日差旅补助36 | 实际完整公务日、经批准行程、客户午晚餐核验；180及40%扣餐规则来自原文 | 未核实餐次则UNKNOWN；没有实际购买交易时不制造餐费发票要求，不支持半日计算 |
| 一般客户招待1200/无人数 | 客户及业务事由、事前批准记录、名单/核实人数、GENERAL类别、绑定交易发票和财务核验 | 人数缺失UNKNOWN补证；四人时限额1000、规则FAIL |

`invoice_verified=True` 本身不能替代住宿/招待原件。补助没有交易发票需求不等于不需要出差/业务/授权/供餐材料；该区分沿用原P4B规则范围，并以222.docx第二十五、二十六、三十八、三十九条为依据。

## Prompt、Schema、Validator 与决策

- JSON只接受SemanticAnalysis已有字段；provider/model固定，八个主题逐项分析。Schema列本次实际事实路径和原文ID；禁止嵌套伪路径、伪条款、虚构数值、职级或授权。
- 正向事实中的VERIFIED可在范围内使用。已核验的合法原发票不因原始号码未披露给模型而重复补证；已核实有效业务授权不重复要求不明确审批层级。仍缺失、为否或实际矛盾的事实照常补证。
- 说明中的明确“第X条”须来自实际传入条款，逐项finding须引用其所列evidence_refs对应条款。校验引用存在性与版本一致性，不能证明整段自然语言语义正确，后者仍须真实复验及人工评分。
- 不同有效期的历史/未来行不能仅因并存被判冲突。一般超标不等于制度版本冲突；未声称例外不制造例外。
- 代码围栏最多本地解包一次，不修补字段、不重挂对象、不改金额、不请求模型。结构错误记录安全loc/type及JSON行列/偏移；事实路径错误定位finding及字段。HTTP重试/预算实现不变。
- 所有既有强制缺证、模型新增缺证、模型风险或例外继续阻止直接财务批准。没有通过删除缺证、修改Golden标签或写假VERIFIED放行。

状态链：`CHECK_PASSED`仅表示合规检查；申请仍在`FINANCE_REVIEW`，ERP计数为0。认证财务明确APPROVE后才进入批准/ERP步骤；ERP Mock成功状态为`SUCCEEDED`，申请为`APPROVED`。同请求幂等重放只返回原结果，不再次写入；再次批准也不产生第二次写入。真实ERP、支付、真实模型、真实向量检索与浏览器复验均未执行。
