# 后续数据源扩展

数据源适配器返回统一记录：`source`、`source_url`、`published_at`（缺失可为空）、`fetched_at`、`status`、`items`。状态区分 `current`、`stale`、`unavailable`、`disabled`，每条资料保留自身日期和来源。

Python 扩展契约定义在 `server/life_app/sources.py` 的 `DataSource`、`SourceResult` 与 `SourceItem`。首版不注册新闻实现或额外调度。

首版只实现天气；WHOOP 与新闻保留为后续功能。新闻源不得自动成为个人经历，外部文章内的指令不改变助手权限。新数据源默认关闭，启用前在设置页说明访问范围、调度、费用和数据去向。
