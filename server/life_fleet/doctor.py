"""Read-only host probes; never print credentials or claim channel acceptance."""
import platform
import shutil
import subprocess


def inspect_host(fleet):
    checks = {}
    for name, argv in {
        "docker_engine": ["docker", "info", "--format", "{{.ServerVersion}}"],
        "compose": ["docker", "compose", "version", "--short"],
        "restic": ["restic", "version"],
    }.items():
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            checks[name] = {"ok": result.returncode == 0,
                            "version": result.stdout.strip()[:100] if result.returncode == 0 else None}
        except (OSError, subprocess.TimeoutExpired):
            checks[name] = {"ok": False, "version": None}
    return {"os": platform.system(), "architecture": platform.machine(), "checks": checks,
            "operator_configured": (fleet.root / "operator.json").is_file(),
            "backup_configured": (fleet.root / "backup.json").is_file(),
            "paused": (fleet.root / "PAUSED").exists(),
            "disk_free_bytes": shutil.disk_usage(fleet.root).free,
            "real_accounts_verified": False, "capacity_verified": False}
