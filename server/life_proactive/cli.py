import argparse
import datetime as dt
import json
from pathlib import Path
import sys

from life_service.core import ServiceError, write_json
from .core import Store, load_config, stamp, iso
from .transport import WeChatSender


def main():
    p = argparse.ArgumentParser(description="Project-owned personal assistant WeChat check-ins")
    p.add_argument("--config", type=Path, required=True)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("test-send", help="Send one real, explicitly labelled connection test; never retry an uncertain result")
    preview = sub.add_parser("preview")
    preview.add_argument("--at", help="Timezone-aware hypothetical time; preview never sends")
    for name in ("tick", "ingest", "record", "context", "control", "followup", "preferences", "knowledge", "profile"):
        sub.add_parser(name)
    args = p.parse_args()
    store = None
    try:
        c = load_config(args.config)
        if c.get("managed") and not c.get("memory_allowed") and args.command in {"ingest", "record", "followup", "context", "preferences", "knowledge", "profile"}:
            print(json.dumps({"status": "disabled", "messages": [], "reason": "memory_consent_required"}))
            return 0
        store = Store(c)
        if args.command == "status":
            result = store.status()
        elif args.command == "doctor":
            result = {"status": WeChatSender(c).check(), "enabled": c["enabled"], "schedule": c["windows"],
                      "note": "Local readiness only; no delivery or image-recognition claim."}
        elif args.command == "test-send":
            result = store.connection_test(WeChatSender(c))
        elif args.command == "preview":
            result = store.decide(stamp(args.at) if args.at else None)
        elif args.command == "tick":
            result = store.tick(WeChatSender(c))
            write_json(c["root"] / ".local/proactive/heartbeat.json", {"at": iso(store.now()), "result": result})
        else:
            data = json.load(sys.stdin)
            if args.command == "ingest":
                result = store.ingest(data)
            elif args.command == "record":
                result = store.record(data["message_id"], data["session"], data["items"])
            elif args.command == "context":
                result = store.pending(data["session"])
            elif args.command == "profile":
                result = store.profile.manage(data)
            elif args.command == "knowledge":
                from .knowledge import manage
                result = manage(store, data)
            elif args.command == "preferences":
                from .preferences import manage
                result = manage(store, data)
            elif args.command == "followup":
                data["mid"] = data.pop("message_id")
                result = store.manage_followup(data)
            else:
                result = {"status": store.controls(data["text"]) or "unknown_control"}
                store.db.commit()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ServiceError as e:
        print(json.dumps({"status": "error", "error": e.code}))
        return 1
    except Exception:
        print(json.dumps({"status": "error", "error": "local_operation_failed"}))
        return 1
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
