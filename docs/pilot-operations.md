# 五人内测运维入口（开发版）

当前开发分支 `codex/pilot-5-users`，版本 `0.2.0a1`。请先读 [验收状态](pilot-validation.md)。没有完成容器、真实账号及迁移验收之前，不发正式邀请。

给受邀测试者的操作步骤见[私人助手内测开通说明](pilot-user-guide.md)。其中真实验收清单需逐人记录，不得以自动测试结果代填。

独立运行时安装到 `/opt/life-pilot/venv` 后，本版本 `scripts/life-admin pilot ...` 与 `life-fleet --root /var/lib/life-pilot ...` 等效。既有个人助手的已安装管理脚本无需替换。

## 保护边界

仅在新建的 `LifeAssistantPilot` WSL 发行版或空白 Linux 服务器操作。原个人助手的工作区、资料、定时任务、运行中的 OpenClaw 都不作为部署来源。镜像构建上下文、发布包均使用白名单。移植目录仅包含通用代码与虚构测试。

每人一个容器、一个数据卷、一个网络和一个中转凭据。云端主密钥和 SMTP 密码仅存运营者目录。容器本身不是独立物理电脑；运营者与宿主机管理员仍有读取数据的能力，共用宿主机故障也会同时影响所有人。

## 构建与配置

Linux 主机运维命令在 root 终端执行；`/opt/life-pilot` 与运营者目录为 0700，配置与 Tunnel 凭据文件为 root 所有、0600。不要通过放宽秘密文件权限解决容器读取问题。入口已验证版本见 `deploy/ingress-images.json`：Caddy 仅保留官方二进制执行所需的 NET_BIND_SERVICE；cloudflared 以 UID 0 读取唯一的秘密挂载，丢弃全部 capabilities、禁止提权、只读根目录，不挂载用户资料或 Docker socket。

1. 在空白 Ubuntu 24.04／Debian 13 上，以 root 运行 `bash scripts/pilot-host-setup.sh --new-pilot-host`。此脚本拒绝检测到旧版单人安装的主机。
2. 校验发布包 SHA256 后解压，进入解压目录，运行 `bash scripts/pilot-install-release.sh --new-pilot-host`。它将管理工具安装到 `/opt/life-pilot/venv`，复制运维说明与未启用的服务模板，不启动助手。失败后同版本可继续安装；已有完整安装则拒绝覆盖。后续可用 `/opt/life-pilot/bin/life-admin pilot ...`，或者把 `/opt/life-pilot/venv/bin` 加入当前终端 PATH。再构建 `docker build -f deploy/Dockerfile -t life-pilot:0.2.0a1 .`，记录实际摘要用于部署；不要把示例摘要当成构建结果。
3. 运行 `life-fleet --root /var/lib/life-pilot configure`，在本机终端输入 API 与 SMTP。输入密钥不回显。配置完成不等于账号已验证。
4. `life-fleet --root /var/lib/life-pilot create friend1` 按需创建，最多五人，重复调用不重复占名额。`broker/caddy/tunnel/operator/default` 为保留名称。
5. `operator.json` 可选 `ingress` 设置必须由运营者本机填写：`caddy_image`、`tunnel_image` 均为已核对的 `仓库@sha256:摘要`，`token_file` 为 Tunnel token 的本机文件路径。Cloudflare 中将每个用户的子域名连接到 `http://caddy:8080`，禁用敏感页面缓存。子域名 DNS、证书及手机流量连通另行验证。
6. `life-fleet --root /var/lib/life-pilot render --image 仓库@sha256:实际摘要 --domain 你的域名 --output /var/lib/life-pilot/compose.json` 生成隔离服务及 Caddy 配置。无公网端口映射。配置不存在时不会假装提供统一服务。
7. 启动公共服务后，用 `life-fleet --root /var/lib/life-pilot start friend1` 启动实例。`invite friend1` 必须在交互终端运行，输出 30 分钟一次性开通凭据。凭据只私下分享给对应用户。

用户打开自己的 HTTPS 子域名，设置密码，确认隐私偏好，验证邮箱，扫码连接微信，测试模型和邮件，预览后启用简报。网页不会展示运营者供应商密钥和 SMTP 参数。尚未启用云端处理时不能识别语音或开启微信助手。

## 仅用于虚构数据的网关负载检查

在独立测试主机运行 `PYTHONPATH=server python3 scripts/pilot-container-fixture.py --gateway --root /var/lib/life-pilot-gateway-fixture --image 仓库@sha256:已验证摘要`，会创建另一个五人测试工程。该模式关闭微信轮询并禁止容器外网；不得用于真实账号。先用 `scripts/pilot-soak.py --gateway --root /var/lib/life-pilot-gateway-fixture --seconds 25 --interval 5` 验证，再开始 `--seconds 86400 --interval 60`。两条命令都需 `PYTHONPATH=server`。报告会明确区分实际网关、虚构来源与真实平台传输；短跑报告应另存后再长跑。

## 用量和费用

已核对的价格模板与五人月费用情景见 [费用估算](pilot-costs.md)。`deploy/prices-conservative-20261007.json` 可安装为运营者目录的 `prices.json`，按高峰且不计缓存折扣作保守估算，不是供应商实际扣款。

2026-10-07 起，新部署默认主模型为 DeepSeek-V4.1-Flash，接口模型 ID 为 `deepseek-flash`，地址为 `https://api.deepseek.com/v1`。聊天、图片、主动联系、记忆与简报统一使用该主模型，关闭思考模式。原始语音另配语音识别供应商的地址、模型和密钥，保存在运营者配置的 `voice` 中；`asr_model` 与 `voice.model` 保持一致。已有平台转写可直接交给主模型。未配置独立识别服务时，原始音频会明确失败，不会把 DeepSeek 密钥提交给其他供应商。该改动尚待真实账号验证及新镜像部署，既有测试镜像不自动改变。

`usage friend1` 查看归属到该用户的请求。开始调用即记为 `in_flight`，完成后更新，重试是不同请求。公共台账不存请求正文。缺失 Token 或完整回执为 `pending_reconciliation/interrupted`；成本为空表示尚不可估算，不是零元。

可在运营者目录创建 `prices.json`，格式为 `{"version":"运营者核对日期","currency":"CNY","models":{"模型名":{"unit":"tokens","input_per_million":输入单价,"output_per_million":输出单价}}}`；语音使用 `unit:audio_seconds,per_second:每秒单价`。价格版本附文件摘要；不自动抓取价格，也不设金额硬上限。缓存、阶梯价、账单折扣未纳入当前估算；实际账单优先。

## 备份与恢复

本机计划路径为 `/mnt/e/LifeAssistantLabBackups`；服务器需另选独立存储。在本机终端设置 Restic 密码文件（Linux 权限 0600），初始化加密仓库。运营者目录的 `backup.json` 包含 `repository` 和 `password_file` 两个绝对路径。不要将密码写进命令行参数或聊天。

`backup friend1` 撤销新中转请求、停止实例，等待已登记的模型／邮件请求结束，保存该用户对应的中转记录后备份，成功快照后才保留 7 份每日／4 份每周并清理，最后恢复之前的运行状态。超过等待期限仍有在途状态时拒绝声称备份完成。原始资料、媒体、微信状态和任务状态保存在用户卷中。`backup-operator` 单独备份运营者配置、实例凭据与一致的 SQLite 快照；`daily-backup` 逐人备份并记录失败，最后备份公共服务。安装 `deploy/life-pilot-backup.service` 和 `.timer` 前，先确认虚拟环境位于 `/opt/life-pilot/venv`，再手动完成一次备份与恢复。定时器默认上海时间 04:15，附加最多 15 分钟随机延迟；本机目前尚未启用。

`freeze friend1` 冻结并停止实例；`restore friend1 --snapshot 精确快照ID --target 空的恢复目录` 校验快照归属、要求源实例停止，并用 Restic `--verify` 恢复。不直接覆盖已有卷，不自动启用第二个发送实例。恢复后核对资料、任务、邮件回执，迁移中转记录并更换内部凭据，然后才切入口与恢复服务。

恢复演练脚本 `scripts/test-restic-recovery.sh` 仅使用临时虚构文件，验证实际加密、完整读取与恢复及错误密码拒绝；它不能替代完整助手卷的恢复演练。

## 升级与回退

发布清单字段：`version`、`image`（不可变摘要）、`data_format:1`、`compatible_from:[1]`。当前工具只支持数据格式 1 的兼容代码升级；格式变化直接拒绝，必须先开发对应迁移与恢复程序。

先在虚构实例验证，再 `upgrade friend1 --manifest 发布清单文件`。工具停写、备份，以维护状态启动新代码，检查进程和数据库就绪，再启用。真实微信和邮箱测试通过后，另一次命令 `upgrade friend2 friend3 friend4 friend5 --manifest 发布清单文件` 按顺序升级；第一处失败立即终止。就绪检查不证明真实通道可用。

原本冻结的用户升级成功后仍保持停止和维护状态，返回 `upgraded_frozen`；需要另行 `resume` 才恢复。宿主处于迁移 PAUSED 时，启动、恢复和升级均被拒绝，不得通过升级绕过旧端暂停。尚未完成开通的 provisioning 实例先完成或核对开通状态，再升级。

统一模型／邮件中转使用 `upgrade-broker --manifest 发布清单文件`，清单还需 `broker_api_version:1`。它先冻结全部用户、分别备份，再备份公共台账，然后更新 broker。成功返回 `broker_ready_users_frozen`，所有用户保持停止；可继续升级各用户镜像，检查后先恢复一人验证，再恢复其余原本启用的用户。`upgrade-broker.json` 保存原状态，原本冻结的人不要恢复。

中转升级失败会尝试停止所有用户和中转，并保留快照。用 `rollback-broker` 恢复之前的中转镜像，用户仍保持冻结，未知邮件回执不会重发。如果回执出现 `stop_requires_manual_check`，先核对实际进程停止状态。仅支持数据格式 1 的兼容回退；公共数据格式变化也必须使用快照迁移流程。中转就绪检查只验证本地认证接口，真实模型和邮件仍需另行测试。

`rollback friend1` 只允许已记录的同格式回退，保留个人数据，并停留在维护状态；检查结果后用 `resume friend1` 恢复。`sending/unknown` 不会因此自动补发。非兼容格式必须恢复升级前快照，不能仅换镜像。

## 自动启动与迁移

Linux 将 `deploy/life-pilot.service` 安装为独立系统服务；Windows 使用 `scripts/Install-PilotStartup.ps1` 创建独立登录任务，隐藏运行守护脚本。本机已注册并启动 `LifeAssistantPilot-Startup`，实际 Windows 重启恢复尚未验收。保活进程不可省略：仅启动 systemd 后，WSL 仍可能因没有前台进程而自动退出。维护前放置 `/var/lib/life-pilot/PAUSED`，避免守护重新启动发送服务；冻结账号的 Compose profile 也会保持禁用。

迁移固定顺序：停止旧实例和发送任务 → 一致备份 → 在空目标恢复 → 检查数据格式与真实回执 → 迁移公共台账、轮换每个内部凭据和入口 → 确认源端保持停止 → 启用新实例。微信可能要求本人重新扫码。

源端执行 `prepare-migration`，保留 `PAUSED` 并生成 `migration.json`。目标端使用一个独立引导目录配置同一备份仓库，执行 `restore-migration --manifest 迁移清单 --target 空目标目录`；恢复公共台账、逐人数据、检查数据库并轮换内部凭据，但不启动发送。以目标目录为 `--root` 重新 `render`（修正入口和路径），逐人 `install-restored-volume 用户ID`。核对源端停止、目标数据、SMTP 不确定回执及微信重新授权后，由运营者移除**目标端** `PAUSED`，逐人 `resume`。源端保留暂停。Tunnel token 文件不包含在公共备份中，需在目标本机重新配置。干净 Ubuntu 24.04 的五人虚构数据迁移、Debian 13 部署冒烟均已通过，详见验收报告；真实账号重新授权与公网切换仍待执行。

`doctor` 只读检查 Docker、Compose、Restic、配置是否存在及磁盘空间；不会读取或输出密码，也不会把工具可用标成真实账号已通过。

## 外部准备与验收

不熟悉域名和 Tunnel 的运营者先读 [外网入口准备步骤](pilot-domain-setup.md)，完成账号与域名准备后再配置正式路由。

需要运营者在本机提供 API、SMTP、域名／Tunnel，以及测试者本人扫码和检查收件箱。不要通过聊天发送凭据。文字、图片、语音、真实到时主动联系、事件改期与取消、手机流量访问都逐人验收。未提供外部材料不影响虚构测试，但不能把这些验收项标成通过。

公共加密备份也包含配置引用的 Tunnel Token。迁移恢复会将 token_file 改为目标目录路径；Token 缺失会阻止备份清理或恢复继续。恢复仍保持 PAUSED，必须重新生成 Compose 和入口配置并检查旧端停止后才能启用。旧版不含 Token 的快照需要在目标本机补配凭据，不能把路径存在当作恢复成功。
