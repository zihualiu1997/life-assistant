import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile

from .store import Store

def backup(store, target):
    target = Path(target).resolve()
    if target.is_relative_to(store.root): raise ValueError("backup_must_be_outside_data_directory")
    with tempfile.TemporaryDirectory() as temporary:
        dbcopy = Path(temporary) / "state.sqlite3"
        source = sqlite3.connect(store.path)
        destination = sqlite3.connect(dbcopy)
        source.backup(destination)
        source.close()
        destination.close()
        with tarfile.open(target, "w:gz") as archive:
            for path in store.root.rglob("*"):
                if path.is_symlink(): raise ValueError("backup_symlink_refused")
                if path.is_file() and path != store.path and path.name not in {store.path.name + '-wal', store.path.name + '-shm'}:
                    archive.add(path, arcname=path.relative_to(store.root).as_posix(), recursive=False)
            archive.add(dbcopy, arcname=".local/state.sqlite3")
    if os.name != "nt": target.chmod(0o600)

def restore(target, archive_path):
    target = Path(target).resolve()
    if target.exists() and any(target.iterdir()): raise ValueError("restore_requires_empty_directory")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if sum(m.size for m in members) > 10_000_000_000: raise ValueError("archive_too_large")
        for member in members:
            path = Path(member.name)
            if not member.isfile() or path.is_absolute() or ".." in path.parts or "\\" in member.name or not (target/path).resolve().is_relative_to(target):
                raise ValueError("unsafe_archive")
        archive.extractall(target, members=members, filter="data")
    if os.name != "nt":
        for path in target.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)

def main():
    parser = argparse.ArgumentParser(description="Self-hosted Life Assistant")
    parser.add_argument("--data", default=os.environ.get("LIFE_DATA_DIR", "/var/lib/life-assistant"))
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("bootstrap")
    subs.add_parser("status")
    subs.add_parser("gateway-check")
    serve = subs.add_parser("serve")
    serve.add_argument("--port", type=int, default=18932)
    serve.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "0.0.0.0"])
    serve.add_argument("--insecure-local", action="store_true")
    back = subs.add_parser("backup")
    back.add_argument("file")
    rest = subs.add_parser("restore")
    rest.add_argument("file")
    args = parser.parse_args()
    os.environ["LIFE_DATA_DIR"] = str(Path(args.data).resolve())
    if args.command == "restore":
        restore(args.data, args.file)
        print("Restored. Keep old senders stopped before starting this copy.")
        return
    store = Store(args.data)
    if args.command == "bootstrap":
        if store.get("admin"):
            print("Already initialized: sign in with your existing admin password.")
            return
        if not sys.stdout.isatty(): raise SystemExit("bootstrap_requires_interactive_terminal")
        print("一次性初始化凭据（30 分钟有效）：", store.bootstrap())
    elif args.command == "gateway-check":
        from .integrations import verify_wechat_owner
        verify_wechat_owner(store)
    elif args.command == "status":
        from .config import service_config
        from life_service.runner import status
        print(json.dumps({**status(service_config(store)), "heartbeat": store.get("heartbeat", {}), "archive": store.get("capture_run", {})}, ensure_ascii=False, indent=2))
    elif args.command == "backup":
        backup(store, args.file)
        print("Backup complete. Contains private records and credentials; keep it private.")
    else:
        import uvicorn
        from .app import create_app
        os.umask(0o077)
        if args.host != "127.0.0.1" and os.environ.get("LIFE_CONTAINER") != "1":
            raise SystemExit("non_loopback_requires_container_runtime")
        uvicorn.run(create_app(args.data, secure=not args.insecure_local), host=args.host, port=args.port, access_log=False, log_level="critical", proxy_headers=True, forwarded_allow_ips="127.0.0.1")

if __name__ == "__main__": main()
