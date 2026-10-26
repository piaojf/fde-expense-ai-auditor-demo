# P4A 与既有企业差旅最小整合合同

本合同记录用户确认的整合模式：住宿、职级、城市采用 P4A；交通/补助保留既有独立差旅发布。冻结 v1.0–v1.4 不变，P4A `/api/v1.5/p4a/v1` 请求/响应与矩阵发布/回滚保持原样。默认不开启 P4A 或收费服务。

## 整合模式（服务端 p4a_enabled=true）

- 新 HOTEL 只能走 P4A 住宿申请和 16 格矩阵。`POST /api/v1.5/claims`、旧 resubmit 中的 HOTEL，旧差旅发布中包含 HOTEL 的规则，以及旧单限额 confirm-rule/publish/lifecycle 写入，返回 `409 P4A_ROUTE_REQUIRED`。客户端不能通过删掉 travel_details 绕过。
- `POST /api/v1.5/users/{id}/travel-profile` 返回 `409 P4A_ROUTE_REQUIRED`。职级/城市写入统一走 P4A facts，有完整来源、版本及左闭右开有效期。旧档案不推断截止日期、不搬迁、没有主数据时为 UNKNOWN。
- 交通和补助继续使用 `travel-compliance-contract.md` 的发布/退休/原文校验、TravelDetails、金额/席别/供餐计算。新交通请求即使省略 travel_details，也由服务端补成交通明细，缺失席别为 UNKNOWN。
- 新差旅 employee_snapshot 的服务端增量字段为 `authority="p4a.1"`、`grade_fact`、`city_fact`、`p4a_blockers`。不能由请求指定。匹配城市仅使用唯一、有效的 P4A 名称或明确别名；不猜地名或行政层级。整段行程未被档案有效期覆盖时 UNKNOWN；金额或席别符合不抵消事实缺失。
- SQLite immutable guards 保护整个 employee_snapshot 与 travel_details。缺少 guards 时，新差旅写入返回 `503 TRAVEL_SCHEMA_REQUIRED`，API 不自动安装。迁移 CLI 必须显式 `--database` 指向已有库；本轮只在临时测试库调用 install(connection)。
- 历史版本没有 authority 标识时，保持原已保存员工/行程/规则快照和既有 pinned 审核路径；P4A 住宿继续原适配器。历史只读/审计不需要搬迁。P4A 模式下，旧 HOTEL 退回单不能经旧入口重提，须单独安排兼容或按 P4A 创建新申请；不默默转换原编号或历史。
- WorkBuddy 前端详情、审批、交通/补助表单和视觉增量保留；整合构建使用 `VITE_ENTERPRISE_MODE=true` 与 `VITE_P4A_ENABLED=true`。住宿和职级从统一入口办理，旧表单不再提供另一套写入口。

## 历史兼容模式（p4a_enabled=false）

既有差旅合同保持原行为，包括旧职级事件与 HOTEL 条件规则，供历史独立环境兼容；不能把该模式配置为整合模式却声称住宿已统一。原目录及既有实例本轮不切换模式。

## 治理与验收边界

住宿仍使用 PostgreSQL P4A 发布/回滚。交通/补助仍使用 SQLite append-only 发布/未来退休，不具备跨类别原子发布或统一回滚。原 v1.0–v1.4 不增字段、不改历史 eval 标签。收费 API、生产、真实 ERP 与新浏览器完整链路不因自动化测试通过而自动验收。
