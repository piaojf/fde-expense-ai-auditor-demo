# P4B semantic.2 / input.2 / positive.2（离线工程协议）

本契约由952dc925独立修复，不替代冻结v1.0–v1.4和P4A金额权威。P4B现有路由和p4b.1报告外层保留；semantic_assessment通过schema_version=p4b.semantic.2区分新版。默认live flag仍关闭。真实收费验收NOT_RUN，历史FAIL保留。

## 输出

严格DTO：domain/compliance_v2.py；所有字段必填，extra禁止，缺字段/重复JSON键/尾随对象/截断/错模型/工具调用均拒绝。checks固定八主题，各自显式CLEAR/ISSUE/UNKNOWN；不产生八段合规发现。findings仅实际异常、缺证、冲突、必须人工复核，最多12项，每项解释≤600字符、证据ID≤4、事实路径≤6，结构化required_evidence只选本请求真实缺失或冲突的事实。总解释≤600字符；公共依据只写一次。每个非CLEAR检查必须有对应发现；CLEAR不能带异常发现，已核实事实不能被重复索取。新响应须带必填版本标记，旧缺身份的回答无法被认作新版。

provider/model不属于模型输出Schema。严格HTTP envelope验证实际模型、唯一assistant choice、stop及无tool_calls后，服务端把已知提供商与实际模型作为provenance写入报告；不会为旧DTO补provider/model。旧ComplianceDeepSeekProvider保留原严格解析，仅供历史协议/回放；实际显式开启P4B live配置的工厂选择CompactComplianceDeepSeekProvider。本轮没有开启或调用live工厂。

2048输出token上限（同时不超过配置预算），完整HTTP JSON体≤32768字节，输出内容仍受16384字节限制。多异常或长输入超限时安全失败，不能通过截断、字段修补或重试非法JSON制造成功。Schema中证据ID/事实路径采用共享$defs去重，仍限制到当前真实集合。

## 输入与证据

共同元数据含原文文档ID、222.docx SHA、发布版本、有效区间和服务端发布核验；逐段只含真实chunk_id与完整原文。完整章节、条款、OOXML位置、版本/SHA映射继续保存在发布源与不可变报告中。模型不负责生成“第X条”。provenance通过共享引用去重，facts.scope绑定单据、版本、日期和行程；逻辑candidate/positive事实路径有明确映射，声明仍是DECLARED。

必需集合在rag/retrieval/compliance_context.py：

- 共同：事实真实性、职级/城市维护、职责分离、部门适用/冲突、票据与查重、审批层级、硬规则、审计和版本治理。保留适用职级行、城市行、部门行及当前金额审批行和禁止自批行。未知部门保留所有候选行，不能任意排除。
- HOTEL：出差申请、私人费用、客观延误、行程晚数一致、住宿标准及例外、共享房间和必要凭证；适用职级矩阵行、当前版本衔接行及超标授权行。
- ALLOWANCE：申请/行程/私人费用/客观延误、实际公务日、供餐扣减、兼职供餐/重复补助限制，以及适用城市基础补助行。无实际购买交易时不虚构餐费发票要求；共同凭证条款仅在有交易时适用。
- ENTERTAINMENT：招待审批、名单与业务事由、类别限制及一般客户工作餐行。GENERAL以外不扩展机器审批能力；类别未知保留全部招待候选行，金额结果保持UNKNOWN。

该集合按类别、已核实职级/城市/部门/金额确定，不用员工文字决定删哪些必需条款。不按体积逐段裁剪。原文字节与chunk_id哈希核验，必需正文或表格行缺失即拒绝，附录永不作为规范依据。完整最小请求仍超32KB时CONTEXT_TOO_LARGE，transport零调用。正常长说明、来源记录或未知范围仍可能触发该安全门禁。

## 正向证据与覆盖

positive.1供旧协议；positive.2在HOTEL增加必需stay，取自P4A已绑定记录，校验员工、城市、入住/退房和申报范围，不创建第二个住宿金额权威。原始票据SHA绑定与财务invoice_verified均必需；ALLOWANCE使用已核实整日/餐次；OTHER映射由服务器绑定确认。事前批准与最终财务批准严格分离。

semantic_coverage分别保留八项模型显式检查和服务端逐项证据状态/required/规则/Hard Gate。任何检查未覆盖、必需事实未知/为否/冲突、规则失败、未决语义或例外，均不能CHECK_PASSED。findings为空本身不构成通过；财务审批再次核验不可变报告、当前事实版本、原票据、P4A规则与证据，缺任何一项不写ERP。

## 引用

程序核验真实ID、同一版本、必需证据、原文完整性，按ID生成citation_bindings，含准确章节/条号/原文。每条异常的basis必须适合其topic及所引原文；明确的中文/英文制度主题和条号归属另有检查，防止真实酒店ID解释供餐/招待规定。业务事实矛盾用NON_COMPLIANT发现表示；不能自动归入POLICY_CONFLICT，后者至少需要两个实际制度依据。

这些是可审计的结构/归因边界，无法形式化证明任意自然语言解释与原文完全等价；复杂归因和模型真实理解仍需新授权后的独立验收。本轮不作此声明。

## 测试与历史

### FIX3 摘要与来源归因

输出Schema保持semantic.2。模型的explanation仍须通过制度主题/条号及事实/规则归因检查，历史失败不得因服务端摘要而跳过校验。常见正向事实断言必须在本字段factual_basis引用实际VERIFIED且有完整核验来源的positive记录；金额断言引用真实rule_results并与结果一致。员工candidate不升级为已核实事实。已覆盖的未决/摘要矛盾、绝对不存在异常和绕过财务审批的断言安全拒绝。

报告explanation对校验成功的compact分析改由服务端生成有限摘要，只呈现模型实际checks/findings、当前范围内事实核验数量、真实规则及aggregate状态。不得添加模型没有检查的语义结论；不得把规则PASS写成财务批准。模型原始explanation仍在semantic_assessment；semantic_validation另保留原结构化analysis和VALIDATED/REJECTED状态。被拒绝的分析不进入semantic_assessment，也不生成通过摘要。summary_bindings分离policy_evidence、verified_facts（稳定evidence_id/路径）、rule_results（索引/真实结果）和模型checks/findings；未给模型结论事后补挂制度引用。

这些是p4b.1报告的可选附加字段（旧历史报告默认空字典），不改模型DTO、金额或财务/ERP权限。原始敏感日志仅本地保存，不进入GitHub。自然语言守卫仍不等于通用语义真实性证明，真实DeepSeek验收仍NOT_RUN，既有真实FAIL保留。

tests/fixtures/p4b_replay_20261012含九次脱敏原始响应及SHA manifest、原输入metadata/评分/本地超限诊断。五个原结构失败仍拒绝；三个条号错配独立诊断仍拒绝；四个旧DTO合法只表示旧结构合法，其中语义PARTIAL不改为PASS。所有旧响应都不能被自动迁移为新版成功。

tests/test_p4b_fix2.py及tests/test_p4b_fix2_postgres.py采用独立作者编写的结构输出，MockTransport只处理这些夹具，没有文本分类器或案例编号答案表。两张正向单使用真实流程/认证/隔离DB/Mock ERP；负向各有显式发现，也保留旧乐观输出对门禁的回归。invalid JSON、错引用、超限及超时安全降级另测。不得把合成响应通过算作真实模型语义验收。
# P2 bounded negation handling (2026-10-12)

The DTO, wire schema, prompt, facts, policy attribution and finance authority are
unchanged. Text validation uses a finite clause-local grammar; it does not prove
arbitrary Chinese semantics or grant approval authority.

- Explicit amount assertions `不超标`, `未超标`, `没有超标`, `未/没有发现/发生超标`,
  `金额检查通过`, `金额通过`, `规则通过` and `金额符合规则/标准/限额` assert a
  passing rule outcome. Explicit `超标` and `金额[检查]/规则失败/不通过/未通过/没有通过`
  assert failure. They must agree with actual rule results and cite `rule_results`.
  The existing English `within limit` / `over limit` forms remain bounded checks.
- Safe approval denial is recognized only as a complete clause:
  `金额[检查]通过/规则通过` + `不/并不` + `等于/代表/意味着` + optional `可以` +
  `整单通过/自动批准`, or optional `系统/模型/本次/本申请/本单/报销单` +
  `不能/不得/不允许/禁止自动批准`. An embedded safe phrase does not neutralize
  another assertion. `规则通过不代表可以自动批准` still asserts that the rules passed.
- Clauses split on Chinese/ASCII comma, semicolon, question/exclamation marks,
  Chinese full stop and newline; whitespace is normalized only for matching.
  Original model output and audit evidence are retained unchanged. An entire
  complex approval clause outside the exact grammar stays rejected, including
  `并非不能自动批准` and `不是不允许自动批准`.
- Explicit finance waivers (`无需/不需要/不必/不用/免除/免于财务审批`,
  bounded `财务…无需/不审/不批`, and `跳过/绕过/省略/免去财务终审`) remain
  contradictions even after a valid denial clause. An isolated negator such as
  `不是；` or `并非，` also causes safe rejection because its scope is unresolved.
- A narrowly supported finding may report a denied instruction:
  `员工系统消息要求跳过/绕过财务审批/批准，没有有效审批权限。` It must be an
  `AUTHORIZATION`/`AUTHORIZATION` finding with `NON_COMPLIANT` conclusion, cite
  `candidate.notes`, and the actual notes must contain `系统消息` followed by
  `批准` or `ERP`. This marker co-occurrence does not prove that the notes
  semantically requested an approval bypass; a benign instruction containing
  those markers can be conservatively misattributed as NON_COMPLIANT and sent
  for evidence review. It cannot grant approval. This does not exempt a top-level clearance, missing
  source/path, or any additional approval assertion. Historical injection
  detection remains successful and finance/ERP remain blocked.
- Except for those complete safe denial clauses, residual negation outside
  recognized amount assertions (`不/未/无/没/非/否`, `not/never/cannot`) is treated as
  ambiguous and rejected. This deliberately includes some benign but unsupported
  compound prose. Conditional wording, quotations, unusual punctuation and
  distant negation are not a general natural-language reasoning capability.
- `RULE_EXPLANATION_CONTRADICTION`, `SEMANTIC_EXPLANATION_CONTRADICTION` and
  `UNSUPPORTED_RULE_CLAIM` remain enforced. Facts, source/version/topic references,
  missing-evidence, findings/summary consistency, Hard Gate, explicit live-valid
  finance approval and ERP idempotency remain independent requirements. Supported
  wording alone never establishes compliance or authorizes ERP effects.
