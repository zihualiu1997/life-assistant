"""Validate actual fixture containers: auth, volume/network boundaries and restart."""
import argparse
import json
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not (root / "FICTIONAL-TEST-ONLY").is_file(): raise ValueError("fixture_marker_required")
    base = ["docker", "compose", "-f", str(root / "compose.json"), "--profile", "pilot"]
    source = (Path(__file__).resolve().parents[1] / "tests/container_probe.py").read_text()
    def probe(payload):
        result = subprocess.run([*base, "exec", "-T", payload["name"], "python", "-"],
            input="PAYLOAD = " + repr(payload) + "\n" + source, text=True, capture_output=True, timeout=45)
        if result.returncode: raise RuntimeError(result.stderr)
        return json.loads(result.stdout)
    sessions = {}
    ips = {}
    for index in range(5):
        name = f"fixture{index}"
        sessions[name] = probe({"name": name, "phase": "initialize"})
        container = subprocess.check_output([*base, "ps", "-q", name], text=True).strip()
        metadata = json.loads(subprocess.check_output(["docker", "inspect", container], text=True))[0]
        ips[name] = next(iter(metadata["NetworkSettings"]["Networks"].values()))["IPAddress"]
        assert metadata["HostConfig"]["ReadonlyRootfs"] is True
        assert not metadata["HostConfig"]["PortBindings"]
    results = []
    for after_restart in (False, True):
        if after_restart:
            subprocess.run([*base, "restart"], check=True, capture_output=True, timeout=120)
            time.sleep(5)
        for index in range(5):
            name, other = f"fixture{index}", f"fixture{(index + 1) % 5}"
            result = probe({"name": name, "phase": "isolation", "cookie": sessions[name]["cookie"],
                            "other": other, "other_cookie": sessions[other]["cookie"], "other_ip": ips[other]})
            results.append({"tenant": name, "after_restart": after_restart, **result})
    report = {"checks": results, "simulated_model_calls": 5, "real_channels_verified": False}
    (root / "container-check.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__": main()
