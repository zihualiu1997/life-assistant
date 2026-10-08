"""Patch only an explicitly supplied build dependency, never a live installation."""
import argparse
import hashlib
import json
from pathlib import Path
from install import transform


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    package = args.package.resolve()
    if json.loads((package / "package.json").read_text(encoding="utf-8"))["version"] != "2.4.9":
        raise SystemExit("Unreviewed WeChat version")
    output = {}
    here = Path(__file__).parent
    for base, extension in (("src", "ts"), ("dist/src", "js")):
        for name, kind in (("monitor/monitor", "monitor"), ("messaging/process-message", "process")):
            relative = f"{base}/{name}.{extension}"
            output[relative] = transform((package / relative).read_text(encoding="utf-8"), kind)
        output[f"{base}/messaging/life-batcher.mjs"] = (here / "batcher.mjs").read_text(encoding="utf-8")
        output[f"{base}/messaging/life-batching-adapter.mjs"] = (here / "adapter.mjs").read_text(encoding="utf-8")
    hashes = {}
    for name, content in output.items():
        path = package / name
        path.write_text(content, encoding="utf-8", newline="\n")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (package / "life-patch-manifest.json").write_text(json.dumps({"version": "2.4.9", "files": hashes}, indent=2), encoding="utf-8")


if __name__ == "__main__": main()
