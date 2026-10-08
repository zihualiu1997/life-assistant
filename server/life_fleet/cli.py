import argparse
import json
import os
import getpass
import sys
from pathlib import Path
from .store import Fleet
from .operations import Operations


def main():
    parser = argparse.ArgumentParser(description="Isolated five-person pilot registry (operator only)")
    parser.add_argument("--root", required=True, help="New fleet directory; never the personal assistant directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("configure")
    ingress = sub.add_parser("configure-ingress")
    ingress.add_argument("--images", required=True, help="Verified ingress-images.json from the release")
    sub.add_parser("backup-operator")
    sub.add_parser("daily-backup")
    sub.add_parser("prepare-migration")
    sub.add_parser("rollback-broker")
    broker_upgrade = sub.add_parser("upgrade-broker")
    broker_upgrade.add_argument("--manifest", required=True)
    create = sub.add_parser("create")
    create.add_argument("id")
    for name in ("freeze", "start", "resume", "invite", "backup", "rollback", "usage", "install-restored-volume"):
        item = sub.add_parser(name)
        item.add_argument("id")
    render = sub.add_parser("render")
    render.add_argument("--image", required=True)
    render.add_argument("--domain", required=True)
    render.add_argument("--output", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("id")
    restore.add_argument("--snapshot", required=True)
    restore.add_argument("--target", required=True)
    upgrade = sub.add_parser("upgrade")
    upgrade.add_argument("ids", nargs="+", help="Order: already validated canary first, then other users; stops at the first failure")
    upgrade.add_argument("--manifest", required=True)
    migrate = sub.add_parser("restore-migration")
    migrate.add_argument("--manifest", required=True)
    migrate.add_argument("--target", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    fleet = Fleet(args.root)
    if args.command == "configure":
        if not sys.stdin.isatty(): raise SystemExit("interactive_terminal_required")
        target = fleet.root / "operator.json"
        if target.exists(): raise SystemExit("operator_config_exists_edit_locally")
        config = {"base_url": input("模型 API 基础地址 [https://api.deepseek.com/v1]: ").strip() or "https://api.deepseek.com/v1", "api_key": getpass.getpass("API Key（不回显）: "),
                  "model": input("文字图片模型 [deepseek-flash]: ").strip() or "deepseek-flash",
                  "asr_model": input("独立语音识别模型（留空仅支持平台已有转写）: ").strip()}
        if config["asr_model"]:
            config["voice"] = {"model": config["asr_model"], "base_url": input("语音服务 API 基础地址: ").strip(),
                               "api_key": getpass.getpass("语音服务 API Key（不回显）: ")}
        config["smtp"] = {"host": input("SMTP 地址: ").strip(), "port": int(input("SMTP 端口: ") or "465"),
                          "tls": input("TLS 类型 ssl/starttls: ").strip() or "ssl", "username": input("SMTP 用户名: ").strip(),
                          "sender": input("统一发件地址: ").strip(), "password": getpass.getpass("SMTP 密码（不回显）: ")}
        target.write_text(json.dumps(config), encoding="utf-8")
        if os.name != "nt": target.chmod(0o600)
        result = {"configured": True, "account_verified": False}
    elif args.command == "configure-ingress":
        if not sys.stdin.isatty(): raise SystemExit("interactive_terminal_required")
        if os.name == "nt" or os.geteuid() != 0: raise SystemExit("run_in_linux_operator_root_terminal")
        import warnings
        from .ingress import configure_ingress
        images = json.loads(Path(args.images).read_text(encoding="utf-8"))
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Tunnel Token（仅粘贴 token，不回显）: ").strip()
        result = configure_ingress(fleet, images, token)
    elif args.command == "create": result = fleet.create(args.id)
    elif args.command == "status": result = fleet.tenants()
    elif args.command == "doctor":
        from .doctor import inspect_host
        result = inspect_host(fleet)
    elif args.command == "backup-operator": result = Operations(fleet).backup_operator()
    elif args.command == "daily-backup": result = Operations(fleet).daily_backup()
    elif args.command == "prepare-migration": result = Operations(fleet).prepare_migration()
    elif args.command == "upgrade-broker": result = Operations(fleet).upgrade_broker(json.loads(Path(args.manifest).read_text(encoding="utf-8")))
    elif args.command == "rollback-broker": result = Operations(fleet).rollback_broker()
    elif args.command == "restore-migration": result = Operations(fleet).restore_migration(json.loads(Path(args.manifest).read_text(encoding="utf-8")), args.target)
    elif args.command == "install-restored-volume": result = Operations(fleet).install_restored_volume(args.id)
    elif args.command == "usage": result = fleet.usage(args.id)
    elif args.command in {"freeze", "start", "resume", "invite", "backup", "rollback"}:
        result = getattr(Operations(fleet), args.command)(args.id) or {"status": "completed"}
    elif args.command == "restore":
        result = Operations(fleet).restore(args.id, args.snapshot, args.target)
    elif args.command == "upgrade":
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        result = [Operations(fleet).upgrade(name, manifest) for name in args.ids]
    else:
        Path(args.output).write_text(json.dumps(fleet.compose(args.image, args.domain), indent=2), encoding="utf-8")
        (fleet.root / "Caddyfile").write_text(fleet.caddyfile(args.domain), encoding="utf-8")
        result = {"rendered": True, "started": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
