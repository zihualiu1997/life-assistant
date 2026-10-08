"""Container PID 1 supervisor. Only the verified owner's gateway may start."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from life_app.store import Store
from life_app.integrations import gateway_env, verify_wechat_owner


def main():
    os.umask(0o077)
    root = Path(os.environ.get("LIFE_DATA_DIR", "/data"))
    store = Store(root)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, lambda *_: stop.set())
    children = []
    gateway = None
    try:
        env = {**os.environ, "LIFE_CONTAINER": "1"}
        app = subprocess.Popen([sys.executable, "-m", "life_app.cli", "--data", str(root), "serve", "--host", "0.0.0.0"], env=env)
        children.append(app)
        marker = store.state / "gateway-restart"
        # Start after explicit activation only, including after a container restart.
        requested = bool(store.get("gateway_enabled", False))
        backoff = 0
        while not stop.wait(1):
            if app.poll() is not None: raise RuntimeError("app_process_exited")
            requested = bool(store.get("gateway_enabled", False)) and os.environ.get("LIFE_MAINTENANCE") != "1"
            if not requested and gateway and gateway.poll() is None:
                gateway.terminate()
                try: gateway.wait(15)
                except subprocess.TimeoutExpired: gateway.kill(); gateway.wait()
                gateway = None
            if marker.exists():
                marker.unlink()
                if gateway and gateway.poll() is None:
                    gateway.terminate()
                    try: gateway.wait(15)
                    except subprocess.TimeoutExpired: gateway.kill(); gateway.wait()
                gateway = None
            if requested and (gateway is None or gateway.poll() is not None) and time.monotonic() >= backoff:
                try:
                    verify_wechat_owner(store)
                    gateway = subprocess.Popen([os.environ.get("LIFE_OPENCLAW_BIN", "openclaw"), "gateway", "run"], env=gateway_env(store), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    children.append(gateway)
                except Exception:
                    store.put("gateway_runtime", {"status": "failed", "error": "gateway_start_failed"})
                backoff = time.monotonic() + 30
    finally:
        for child in children:
            if child.poll() is None: child.terminate()
        for child in children:
            try: child.wait(20)
            except subprocess.TimeoutExpired: child.kill(); child.wait()


if __name__ == "__main__": main()
