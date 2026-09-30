# 安装与维护

安装器只支持 Ubuntu 24.04、Debian 13 x86-64。系统必须具备可用 DNS、Git 和 sudo，域名必须解析到本机，云防火墙开放 TCP 80/443。若已有网站占用端口，安装器停止，不会替换它；在独立服务器部署，或由管理员手动将现有 HTTPS 反向代理接到回环端口 18932，并阻断 `/internal/*`。

应用发布版本位于 `/opt/life-assistant/releases`，运行时位于 `/opt/life-assistant/runtime`，数据位于 `/var/lib/life-assistant`。账户凭据在数据目录的 `.secrets`，会话和设备摘要在 `.local`。OpenClaw 18789 及应用 18932 不对公网监听。

```bash
sudo life-admin status
sudo life-admin bootstrap
sudo life-admin backup /root/life-backup.tar.gz
sudo life-admin rollback
sudo life-admin uninstall
```

备份包含个人资料、设备令牌和服务凭据，文件权限限制为本人可读写。备份前停止两个写入服务，完成后恢复；备份不能上传公开仓库。卸载保留数据、发布目录和 Caddy 软件包。

升级：下载并检查新版本，先备份，再从新版本源码目录运行 `sudo bash install.sh`，输入同一域名。安装器在新目录构建，成功后切换 `current`，旧版本保存为 `previous`。不要同时启动两套邮件发送器。

恢复：先停 `life-app` 和 `life-gateway`，将旧数据目录保留为另一个备份位置。使用 `/opt/life-assistant/current/venv/bin/life --data /var/lib/life-assistant restore /root/life-backup.tar.gz` 恢复到空目录，再执行 `chown -R life-assistant:life-assistant /var/lib/life-assistant`，启动服务。程序拒绝路径穿越、链接和非空目标；不自动覆盖既有资料。

网关只允许运行生活助手工具。微信扫码与模型回复是两个独立验证步骤，完成扫码后需本人发送测试消息。若登录过期，在设置页重新扫码并启动。插件版本由安装器锁定，升级插件需重新验收本人访问限制及归档。

邮件 `sent` 表示 SMTP 接受，`unknown/sending` 表示需核对邮箱，程序不会自动重发。请保留回执，不以删除记录的方式重试。模型/天气/SMTP 设置修改后重新测试和预览；重启不会重置已经消耗的简报重试次数。

默认不记录私密正文或凭据。`life-admin status` 提供固定错误码；排错时不要把整个数据目录上传。
