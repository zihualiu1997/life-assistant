"""Create an isolated five-container fixture; never use real accounts or public ports.

Run on the Linux Docker host with PYTHONPATH=server. This is not account acceptance.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time
from life_fleet.store import Fleet


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--gateway", action="store_true", help="Real gateways with fictional admission; no platform polling")
    parser.add_argument("--project", help="Distinct Compose project for concurrent release candidates")
    args = parser.parse_args()
    project = args.project or ("life-pilot-gateway-fixture" if args.gateway else "life-pilot-fixture")
    if not re.fullmatch(r"life-pilot-[a-z0-9][a-z0-9-]{0,48}", project):
        raise ValueError("invalid_fixture_project")
    root = Path(args.root).resolve()
    marker = root / "FICTIONAL-TEST-ONLY"
    if root.exists() and any(root.iterdir()) and not marker.is_file():
        raise ValueError("fixture_requires_empty_or_marked_directory")
    previous = root / "compose.json"
    if previous.exists() and json.loads(previous.read_text())["name"] != project:
        raise ValueError("fixture_project_change_refused")
    # A different directory must not silently recreate another running fixture.
    ids = command("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project).split()
    if ids:
        existing = json.loads(command("docker", "inspect", *ids))
        expected = str(root / "compose.json")
        for container in existing:
            if container["Config"]["Labels"].get("com.docker.compose.project.config_files") != expected:
                raise ValueError("fixture_project_already_owned_by_other_root")
    fleet = Fleet(root)
    marker.write_text("No real accounts or data.\n")
    config = {"base_url": "https://fixture.invalid/v1", "api_key": "fictional-only",
              "model": "fixture-model", "asr_model": "fixture-asr"}
    (root / "operator.json").write_text(json.dumps(config))
    for index in range(5):
        name = f"fixture{index}"
        fleet.create(name)
        fleet.state(name, "active")
    spec = fleet.compose(args.image, "fixture.invalid")
    spec["name"] = project
    if args.gateway:
        setup = Path(__file__).resolve().parents[1] / "tests/container_gateway_fixture.py"
        for index in range(5):
            name = f"fixture{index}"
            service = spec["services"][name]
            service["entrypoint"] = ["python", "/fixture-start.py"]
            service["environment"]["LIFE_FIXTURE_NAME"] = name
            service["volumes"].append({"type": "bind", "source": str(setup), "target": "/fixture-start.py", "read_only": True})
        for network in spec["networks"].values(): network["internal"] = True
    broker = spec["services"]["broker"]
    source = Path(__file__).resolve().parents[1] / "tests/container_fixture_broker.py"
    broker["volumes"].append({"type": "bind", "source": str(source), "target": "/fixture-broker.py", "read_only": True})
    broker["entrypoint"] = ["python", "/fixture-broker.py"]
    compose = root / "compose.json"
    compose.write_text(json.dumps(spec, indent=2))
    base = ["docker", "compose", "-f", str(compose), "--profile", "pilot"]
    command(*base, "up", "-d")
    checks = []
    for index in range(5):
        name = f"fixture{index}"
        probe = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:18932/health/ready',timeout=3).read().decode())"
        for attempt in range(30):
            result = subprocess.run([*base, "exec", "-T", name, "python", "-c", probe], capture_output=True, text=True)
            if result.returncode == 0:
                checks.append({"tenant": name, "ready": json.loads(result.stdout)})
                break
            if attempt == 29:
                raise RuntimeError("container_not_ready:" + name)
            time.sleep(1)
    report = {"image": args.image, "container_readiness": checks, "real_accounts_verified": False}
    (root / "readiness.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
