# 接口约定

`/v1/*` 接受 `Authorization: Bearer <device-token>`；管理员网页使用 HttpOnly/SameSite 会话，写操作必须携带 `X-CSRF-Token`。未认证 401，权限不符 403，冲突 409，限流 429。错误不回显用户输入或上游异常。

| 接口 | 功能 |
| --- | --- |
| GET /v1/health | 连接与认证检查 |
| POST /v1/pair | 用十分钟一次性 `code` 和设备 `name` 换取设备令牌 |
| POST /v1/journal | `message_id` UUID、`date`、`body`；原文写入，重复不追加，变更内容返回冲突 |
| GET /v1/query | `kind=journal/plan/morning/evening`、`date`；返回原文与相对来源 |
| POST /v1/chat | `message_id`、`conversation_id` UUID、`body`；成功结果可按原编号重取 |
| GET /v1/history | 最近 100 条本人的多入口对话；包括待确认结果 |
| GET /v1/notes | Markdown 路径列表 |
| GET /v1/note | `path`；返回正文与 `revision` |
| PUT /v1/note | `path/text/revision`；并发修改返回 409，更新前留存旧版本 |

后台管理接口位于 `/api`，包括 settings、test/model、test/weather、test/mail、preview/morning、preview/evening、activate、wechat/login、wechat/status、wechat/start、pairing、devices 和 status。

外部聊天导入：设置页选择 JSON 数组，每条为 `{"message_id":"UUID","conversation_id":"import","body":"用户原话"}`。同一编号不同内容拒绝导入；只导入用户明确选择的原文，不扫描本机其他应用。Markdown 导入放入收件箱，拒绝覆盖。导出只包含白名单 Markdown，不包含凭据和运行状态。

内部桥接 `/internal/*` 仅接受本机与独立桥接密钥，Caddy 禁止公网访问。桌面设备令牌不是管理员令牌，也不是 OpenClaw 网关令牌。
