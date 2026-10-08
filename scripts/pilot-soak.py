"""Long-running synthetic web/broker test. Does not certify WeChat/gateway load."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import time
from life_fleet.store import Fleet
from life_assistant import atomic_write


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--seconds", type=int, default=86400)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--gateway", action="store_true", help="Exercise actual gateways with fictional SDK admission")
    args = parser.parse_args()
    if args.seconds < 1 or args.interval < 1: raise ValueError("invalid_duration")
    root = Path(args.root).resolve()
    if not (root / "FICTIONAL-TEST-ONLY").is_file(): raise ValueError("fixture_marker_required")
    fleet = Fleet(root)
    base = ["docker", "compose", "-f", str(root / "compose.json"), "--profile", "pilot"]
    source = (Path(__file__).resolve().parents[1] / "tests/container_probe.py").read_text()
    def probe(payload):
        result = subprocess.run([*base, "exec", "-T", payload["name"], "python", "-"],
            input="PAYLOAD = " + repr(payload) + "\n" + source, text=True, capture_output=True, timeout=120 if args.gateway else 45)
        if result.returncode:
            # Fixture-only diagnostics; never include browser cookies or input.
            atomic_write(root / ("failed-probe-" + payload["name"] + ".txt"), result.stderr[-8000:])
            raise RuntimeError("fixture_request_failed:" + payload["name"])
        return json.loads(result.stdout)
    def usage_counts():
        return {row["id"]: len(fleet.usage(row["id"])) for row in fleet.tenants()}
    names = [f"fixture{i}" for i in range(5)]
    sessions = {name: probe({"name": name, "phase": "initialize"}) for name in names}
    start = time.monotonic()
    last_login = start
    report = {"status": "running", "requested_seconds": args.seconds, "cycles": 0, "elapsed_seconds": 0,
              "scope": "five actual gateways with fictional SDK admission and simulated upstream" if args.gateway else "five concurrent web journals and simulated broker requests",
              "openclaw_gateway_exercised": args.gateway, "real_accounts_verified": False,
              "wechat_platform_delivery_exercised": False, "gateway_request_seconds_max": 0,
              "full_pilot_acceptance": False, "container_memory_peak_bytes": 0,
              "windows_available_min_bytes": None, "windows_resource_samples": 0}
    def save():
        report["elapsed_seconds"] = round(time.monotonic() - start, 2)
        atomic_write(root / "soak-progress.json", json.dumps(report, indent=2))
    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            while time.monotonic() - start < args.seconds:
                # Browser sessions expire at 12 hours; exercise normal sign-in again.
                if time.monotonic() - last_login >= 10 * 3600:
                    sessions = {name: probe({"name": name, "phase": "initialize"}) for name in names}
                    last_login = time.monotonic()
                before = usage_counts()
                tasks = [{"name": name, "phase": "gateway-load" if args.gateway else "load", "cookie": sessions[name]["cookie"], "csrf": sessions[name]["csrf"]} for name in names]
                results = list(pool.map(probe, tasks))
                if args.gateway:
                    report["gateway_request_seconds_max"] = max(report["gateway_request_seconds_max"], *(r["elapsed_seconds"] for r in results))
                after = usage_counts()
                assert all(after[name] >= before[name] + 1 if args.gateway else after[name] == before[name] + 1 for name in names), "usage_attribution_or_duplicate"
                assert all(not any(row["status"] == "in_flight" for row in fleet.usage(name)) for name in names), "unfinished_requests"
                ids = subprocess.check_output([*base, "ps", "-q"], text=True).split()
                lines = subprocess.check_output(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", *ids], text=True).splitlines()
                total = 0
                for line in lines:
                    match = re.fullmatch(r"([\d.]+)(B|KiB|MiB|GiB)", line.split("/")[0].strip())
                    if not match: raise ValueError("unrecognized_memory_unit")
                    total += float(match[1]) * {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[match[2]]
                report["container_memory_peak_bytes"] = max(report["container_memory_peak_bytes"], int(total))
                assert total <= 12 * 1024**3, "container_memory_exceeded"
                try:
                    output = subprocess.check_output(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"], text=True, stderr=subprocess.DEVNULL, timeout=15)
                    available = int(output.strip()) * 1024
                except (OSError, ValueError, subprocess.SubprocessError):
                    available = None
                if available is not None:
                    report["windows_resource_samples"] += 1
                    report["windows_available_min_bytes"] = min(report["windows_available_min_bytes"] or available, available)
                    assert available >= 2 * 1024**3, "windows_memory_below_reserve"
                report["cycles"] += 1
                save()
                print(json.dumps({"cycles": report["cycles"], "elapsed_seconds": report["elapsed_seconds"]}), flush=True)
                remaining = args.seconds - (time.monotonic() - start)
                if remaining > 0: time.sleep(min(args.interval, remaining))
        report["status"] = "completed_synthetic_scope_only"
        save()
    except BaseException as exc:
        report["status"] = "failed_or_interrupted"
        report["error_type"] = type(exc).__name__
        save()
        raise


if __name__ == "__main__": main()
