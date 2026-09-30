import argparse,json
from pathlib import Path
from .core import load_config
from .runner import sync_worker
p=argparse.ArgumentParser()
p.add_argument("--config", required=True)
p.add_argument("command", choices=["sync-worker"])
p.add_argument("--source", choices=["weather"], required=True)
a=p.parse_args()
print(json.dumps({"status":sync_worker(load_config(Path(a.config)),a.source)}))
