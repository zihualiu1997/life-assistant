# 域名与外网入口准备：给运营者的步骤

核对日期：2026-10-07。你要准备的是一个自己管理的域名和 Cloudflare 账号；朋友以后直接用浏览器打开各自的网址。购买、登录、邮箱验证由你操作，服务器配置由实施者完成。模型 API 已有独立配置流程，和这里的 Tunnel 凭据是两回事。

## 先准备域名

1. 登录 [Cloudflare 控制台](https://dash.cloudflare.com/)。没有账号就先注册并完成邮箱验证。
2. 没有域名：在域名注册页面查询想要的名字，确认首年价、续费价和可用支付方式后自行购买。域名不需要包含真实姓名。购买之前不用先买云服务器。具体价格以结算页为准。
3. 已有域名：在 Domains 中选择 Onboard a domain，填写根域名，例如 `example.com`。按页面提示检查已有 DNS 记录，再去原注册商把 nameservers 改成 Cloudflare 分配的两个地址。如果已有网站或企业邮箱，先保留、核对原有记录；不要直接删掉它们。
4. 等域名状态显示已激活。新域名直接由 Cloudflare 注册时，DNS 接入步骤通常由它完成。以 [Cloudflare 域名接入说明](https://developers.cloudflare.com/fundamentals/manage-domains/add-site/) 和实际控制台为准。

完成后，只需告诉实施者**域名名称**，例如 `example.com`。账号密码不用分享。项目将使用 `friend1.example.com` 等一级子域名，每人独立入口；不是让五个人共用一个密码。

## 创建 Tunnel

1. 在 Cloudflare 控制台进入 Networking → Tunnels，选择 Create a tunnel，取名 `life-pilot`。
2. 创建后保留连接器安装页面。页面给出的安装命令包含 Tunnel 凭据，不要发到聊天、截图或公开文档里；也不要在 Windows 随手运行它，以免另装一份连接器。
3. 实施者会用项目的固定版本容器连接 Tunnel。按下面的本机输入步骤保存凭据，随后由实施者生成部署配置并启动入口。连接状态变成 Healthy 后，再添加 Published application 路由。
4. 为每个已开通用户填写对应子域名，服务协议选 HTTP，服务地址填 `caddy:8080`，路径留空。这里是本项目容器内的入口地址；用户访问的仍是 HTTPS 网址。主模型中转、数据库和 OpenClaw 管理端口不创建外网路由。

操作名称以 [Cloudflare Tunnel 官方创建步骤](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/get-started/create-remote-tunnel/) 为准。注册域名、创建 Tunnel、添加路由是三个不同步骤；只看到 Healthy 还不能证明朋友能登录。

## 在自己电脑上保存 Tunnel Token

安装好运营者工具后，在 Windows Terminal 的 PowerShell 标签页粘贴下面这一行。它会进入独立测试 Linux 环境并显示隐藏输入提示：

```powershell
wsl -d LifeAssistantPilot -u root -- /opt/life-pilot/venv/bin/life-fleet --root /var/lib/life-pilot configure-ingress --images /opt/life-pilot/deploy/ingress-images.json
```

从 Cloudflare 连接器安装命令中复制 `--token` 后面的那一段 Token（不要复制整个命令、引号或 `--token`），粘贴到提示位置，然后按回车。输入时看不到字符是正常的。完成后只需告诉实施者“Tunnel 已保存”，不要把 Token 发到聊天。

命令把 Token 保存在该 Linux 环境的私有文件中，保留现有模型和邮箱配置；不会启动服务或发送消息。重复配置会拒绝覆盖，换 Token 时由实施者检查现有连接后处理。`configured: true` 仅表示本机保存成功，尚未验证 Cloudflare 连接、域名或外网访问。

以后迁往普通 Linux 服务器时，在管理员终端直接运行同一条 `/opt/life-pilot/venv/bin/life-fleet ...` 命令，省略前面的 `wsl ... --`。

## 分享邀请前，与你一起检查

- 实施者核对固定镜像、域名路由和缓存设置，确认未登录不能读取资料，跨用户凭据无效。
- 你用手机关闭 Wi-Fi，以移动网络打开测试用户的 HTTPS 网址，完成登录和保存设置，再刷新页面确认仍在。
- 请一位异地测试者再做一次访问；本人扫码连接微信，验证实际邮箱收件。
- 这些检查通过后，再生成 30 分钟一次性邀请，私下发给对应朋友。过期重新生成，不共用凭据。

目前域名及外网路由仍待配置。若手机流量或异地网络无法稳定访问，就先定位网络原因，不把本机浏览器能打开记作通过。已有的个人助手不接入这组新域名。
