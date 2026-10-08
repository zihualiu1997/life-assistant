# 生活助手

本分支为 **0.2.0a1 五人内测开发版**，尚未完成真实部署验收。多人部署请从 [内测运维说明](docs/pilot-operations.md) 和 [验收报告](docs/pilot-validation.md) 开始；下方保留原单人安装说明。

微信随手记录，桌面宠物陪你整理，早晚邮件带来当天真正有用的安排。资料保存在自己的服务器，知识库是可以带走的 Markdown。

**当前源码为 0.2.0a1 内测开发版；已发布的 0.1.0 候选安装包仍保留在 Releases。** 自动测试覆盖核心流程；真实服务器、供应商账号和微信端到端验收进度见 [验证记录](docs/validation.md) 和 [内测验收](docs/pilot-validation.md)。未经过真实验证的接入不标为已验证。安装包以 Releases 中实际列出的预发布附件为准。

## 五步开始

准备 Ubuntu 24.04 或 Debian 13 x86-64 服务器、指向它的域名，以及允许入站 TCP 80/443 的云防火墙规则。安装器需要 sudo；运行服务使用独立非 root 用户。

1. **克隆并安装**，在服务器终端执行：

   ```bash
   git clone https://github.com/zihualiu1997/life-assistant.git
   cd life-assistant
   sudo bash install.sh
   ```

   输入域名，等待安装完成。打开输出的 HTTPS 地址，用终端中的一次性凭据设置管理员密码。凭据 30 分钟有效，过期运行 `sudo life-admin bootstrap`。
2. **连接模型**：选择千问、DeepSeek、OpenAI 或自定义兼容接口，填写模型名称和 API Key，勾选资料处理范围，点击“保存并测试模型”。模型需要支持 Chat Completions；微信工具操作还需要支持工具调用。API Key 只配置一次。
3. **准备知识库与天气**：填写称呼与时区，输入 OpenWeatherMap Key，查找并选择城市，测试天气。知识库已经自动创建，无需安装 Obsidian。
4. **连接微信与邮箱**：扫码后启动微信；填写 SMTP 设置并发送测试邮件。在知识库填写自己的计划和允许用于简报的资料，预览两份简报并确认后启用。
5. **开始使用**：在微信发送“记一下：……”或聊天。桌面用户从 [Releases](https://github.com/zihualiu1997/life-assistant/releases) 下载 Windows 安装包，在网页生成配对码后连接。

微信只接受扫码本人；每套部署属于一个人，可连接多台自己的桌面设备。Windows 客户端没有独立模型配置，服务器断线时不自动补传写入。

## 数据与能力

- [五天渐进认识与长期资料](docs/progressive-profile.md)：每天最多两次、每次一个问题，可跳过、暂停；正常聊天中的明确自述可逐步更新当前资料并保留变化历史。已有用户不强制重填问卷。

- 原话日记、有效计划查询、知识笔记浏览编辑；问答原文与待核验整理分别保留。
- 知识检索为有界本地文本检索，无需向量数据库或额外嵌入模型。
- 默认晨报 08:30、明日安排 21:00、聊天整理 22:30，按设置的时区执行。
- 日记仅“简报可用”章节进入简报；普通对话不会自动成为日记。请在知识库查看和编辑允许外发的内容。
- 模型调用使用你选择的供应商；不自动向其他供应商转发。邮箱仅发简报，不读取收件箱。
- OpenWeatherMap 使用五天／三小时预报；天气失败不阻断简报，不把三小时温度当全天最高最低温。
- 支持 Markdown 导入导出；本地 Obsidian 编辑后可另存导入。首版没有双向同步。
- WHOOP 和新闻采集不在首版启用；[数据源接口](docs/data-sources.md) 为后续扩展保留。

## 开发

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
pip install -e . httpx==0.28.1
python -m unittest discover -s tests -v
python scripts/privacy_scan.py .
life --data ./data bootstrap
life --data ./data serve --insecure-local
```

开发服务只绑定 `127.0.0.1:18932`；`--insecure-local` 只用于本机 HTTP 测试。正式安装必须使用 HTTPS。

桌面开发需要 Node 24、Rust、Windows C++ Build Tools；普通安装包用户不需要这些工具：

发布构建使用仓库中的 pnpm 锁文件：

```bash
cd desktop
npm install -g pnpm@11.19.0
pnpm install --frozen-lockfile
pnpm run build
cargo fetch --locked --target x86_64-pc-windows-msvc --manifest-path src-tauri/Cargo.toml
python ../scripts/collect_licenses.py
pnpm tauri build
```

升级、备份和回滚见 [运维说明](docs/operations.md)，公开接口见 [API](docs/api.md)。源码与用户数据分离，勿将自己的数据目录提交 Git。
