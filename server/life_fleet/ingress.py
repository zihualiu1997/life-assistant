"""Local operator setup; never starts containers or contacts Cloudflare."""
import json
import re
from life_assistant import atomic_write, lock


def configure_ingress(fleet, images, token):
    # Accept the token alone, never a pasted shell install command.
    if not re.fullmatch(r"[A-Za-z0-9_+/=-]{32,16384}", token):
        raise ValueError("paste_only_tunnel_token")
    selected = {}
    for key in ("caddy_image", "tunnel_image"):
        value = images.get(key, "")
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9./:_-]+@sha256:[a-f0-9]{64}", value):
            raise ValueError("ingress_requires_pinned_images")
        selected[key] = value
    with lock(fleet.root, "fleet-operations"):
        target = fleet.root / "operator.json"
        token_path = fleet.root / "tunnel-token"
        config = json.loads(target.read_text(encoding="utf-8"))
        if config.get("ingress") or token_path.exists() or token_path.is_symlink():
            raise ValueError("ingress_exists_review_locally_before_replacing")
        selected["token_file"] = str(token_path)
        atomic_write(token_path, token)
        try:
            config["ingress"] = selected
            atomic_write(target, json.dumps(config, indent=2) + "\n")
        except BaseException:
            token_path.unlink()
            raise
    return {"configured": True, "started": False, "public_network_verified": False}
