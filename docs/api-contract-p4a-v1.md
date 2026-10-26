# P4A 住宿矩阵增量契约

本契约仅在 `P4A_ENABLED=true`、企业会话模式及同一 PostgreSQL 权威库/检索器同时配置时启用。默认关闭，冻结 v1.0–v1.4 的 DTO、OpenAPI 与契约文件不变。所有端点位于 `/api/v1.5/p4a/v1`，复用企业 Cookie、来源校验、公司归属及角色校验。金额均为 Decimal 字符串。

| 方法与相对路径 | 权限 | 行为 |
|---|---|---|
| GET `/people` | ADMIN / FINANCE | 同公司员工的公开身份字段 |
| GET `/facts` | 已登录 | 员工只能读取本人职级/住宿及本公司城市记录 |
| POST `/facts` | ADMIN；FINANCE 仅 STAY | 追加有效期主数据；同公司/类型/key 的有效期不得重叠 |
| GET `/versions` | ADMIN | 同公司 HOTEL 权威版本，含历史原版及未来矩阵 |
| POST `/policies/{upload_id}/read` | ADMIN | 重新校验原文件与安全解析，返回完整矩阵、来源、摘要、确认历史、草稿状态 |
| POST `/policies/{upload_id}/confirm` | ADMIN | 以当前 revision、extraction_digest、confirmed=true、reason 确认全部16格 |
| POST `/policies/{upload_id}/preview` | ADMIN | predecessor_ids 必须精确覆盖全部冲突旧版本，返回范围、日期、原文及 preview_digest 或 blockers |
| POST `/policies/{upload_id}/publish` | ADMIN | preview 请求 + preview_digest、confirmed=true、reason、idempotency_key；单一事务发布完整快照 |
| POST `/versions/{id}/lifecycle-preview` | ADMIN | kind=RETIRE/ROLLBACK、effective_on、可选 restore_id，展示未来变更与恢复范围 |
| POST `/versions/{id}/lifecycle` | ADMIN | 加预览摘要、明确确认、原因和幂等键；回滚新增版本，原版不删除 |
| POST `/invoices/drafts/{id}/checks` | EMPLOYEE | fields、confirmed_fields；复用原票据检查，覆盖同公司重复号码，保留原 OCR 与人工差异说明 |
| POST `/claims` | EMPLOYEE | 新建真实服务端 CLM ID，提交可信事实快照并启动原审核工作流，202 |
| POST `/claims/{id}/resubmit` | 本人 EMPLOYEE | RETURNED + expected_revision；保留业务ID、新建版本/运行，保留旧事实与结果，202 |
| GET `/claims` | 已登录 | 按原企业归属范围列出P4A单据与最新规则结果 |
| GET `/claims/{id}` | 有权读取该单据的用户 | facts、rule_result、全部不可变 reports、schema_version=p4a.1 |

原制度上传、元数据确认和草稿索引仍使用 `/api/v1.5/policies/...`；发票预览、上传及本地OCR仍使用现有企业票据端点。审批、退回与ERP恢复仍使用现有财务工作流。新接口不授予员工维护职级或城市的权限。

## 申请与事实

`HotelSubmission` 包含 fields、stay_id、可选 invoice 和 idempotency_key。fields 仅含 applicant_name、department、position、category=HOTEL、city、trip_start、trip_end、incurred_on、nights、amount、purpose、seller、notes。身份必须等于登录用户；grade/city_tier 等额外字段会被拒绝。退房日减入住日必须等于晚数，费用日必须等于入住日。真实运行使用上海当日，退房不得在未来；合成时钟只允许 APP_ENV=test。

可信数据由服务端按费用日读取：EMPLOYEE 的 key 为员工ID、value.grade=A/B/C/D；CITY 包含规范 name、明确 aliases、tier=I/II/III/IV、level=MUNICIPALITY/PREFECTURE/COUNTY/TOWN；STAY 包含 employee_id、city_key、address、check_in、check_out、evidence_reference。每条记录须含 effective_from/to、source、version、reason，并保存服务端 actor 和 recorded_at。主数据、每版事实、每次审核结果禁止更新/删除。

未覆盖的行政区、未知职级、住宿地点不匹配、入住日期/实际地址未核实、跨职级/城市/制度区间等均返回 UNKNOWN，不能批准。开票方注册地不参与城市归类。

## 完整矩阵与治理

Word提取保留 employee_grade、city_tier、currency=CNY、limit_per_room_per_night、source_table/row/column、original_cell/row/header、source_section、文件SHA和城市分级原文。发布快照进一步绑定 legal_entity、policy_version_id、effective_from/to、source_document_id。16格整体确认、整体发布，禁止无来源手填值。

发布使用 UI-P3B 的表、不可变触发器、同一事务和治理锁。新矩阵覆盖四个受控城市等级，原上传的单地区索引元数据保留。当前种子库需要显式替代深圳和北京两个旧权威版本；原旧版及其有效期字段不物理修改，以不可变切换事件决定适用区间。未到生效日、DRAFT、失效、范围冲突不会放行。回滚到单城市旧版只恢复原城市范围，其他城市缺少有效制度时仍为UNKNOWN。

同一次审核使用 audit-start 可见的唯一权威版本；规则计算、原文引用、人工批准复核使用相同快照。RAG版本不一致记录 RULE_RAG_VERSION_CONFLICT，并禁止批准。

## 结果与失败

rule_result 包含 rule_id、policy_version_id、employee_grade、city、city_tier、limit_per_night、nights、total_limit、claimed_amount、exceeded_amount、decision、source_reference、reason。金额 PASS 仅代表确定性金额检查，仍经过 Hard Gates、发票、风险和人工权限。

同一幂等键和同一请求返回原结果；不同请求复用键、陈旧预览/版本、制度冲突等返回409。角色403、资源/归属不存在404、DTO及日期错误422。数据库或检索错误使原工作流安全中断/转人工，不能产生批准或ERP副作用。发布失败回滚所有矩阵与切换事件。
