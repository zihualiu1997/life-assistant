"""Explicit local source selection. Never traverse links or attachment folders."""
import datetime as dt
import json
import re

from .core import ServiceError, inside, read_json

# Drop the whole paragraph, rather than replacing a number while retaining private context.
PRIVATE = re.compile(r"授权码|令牌|密码|密钥|api[_ -]?key|access_token|refresh_token|sk-[A-Za-z0-9]|财务|收入|工资|余额|存款|负债|借款|借钱|理财|转账|人民币|[¥￥]|\d[\d,.]*\s*(?:元|万元)|恋爱|分手|前任|伴侣|暗恋|暧昧|私密|情感|感情|性生活", re.I)


def safe_text(text):
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    paragraphs = re.split(r"\n\s*\n", text)
    return "\n\n".join(p for p in paragraphs if not PRIVATE.search(p)).strip()


def sections(text, names):
    """Exact heading names; include descendants until the next peer heading."""
    if not names:
        return ""
    result, level = [], None
    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            depth, name = len(match[1]), match[2]
            if level is not None and depth <= level:
                level = None
            if name in names:
                level = depth
        if level is not None:
            result.append(line)
    return "\n".join(result)


def source_text(c, spec, target):
    if spec.get("start") and target < dt.date.fromisoformat(spec["start"]):
        return None
    if spec.get("end") and target > dt.date.fromisoformat(spec["end"]):
        return None
    path = inside(c["root"], spec["path"])
    if path.suffix.lower() != ".md" or any(p in (".local", ".runtime", ".git", "附件") for p in path.relative_to(c["root"]).parts):
        raise ServiceError("context_source_not_allowed")
    if not path.exists():
        return {"source": spec["path"], "status": "missing"}
    raw = path.read_text(encoding="utf-8-sig")
    text = sections(raw, spec.get("sections", []))
    if spec.get("include_intro"):
        text = re.split(r"(?m)^##\s", raw, maxsplit=1)[0] + "\n" + text
    if spec.get("table_section"):
        table = sections(raw, [spec["table_section"]])
        selected, headers = [], []
        for line in table.splitlines():
            if not line.startswith("|"):
                continue
            first = line.split("|")[1].strip()
            iso = re.search(r"\d{4}-\d{2}-\d{2}", first)
            short = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", first)
            if iso:
                if iso[0] == target.isoformat():
                    selected.append(line)
            elif short:
                # Month/day tables require a bounded year/date range in configuration.
                if not spec.get("start") or not spec.get("end"):
                    raise ServiceError("table_requires_date_range")
                if (int(short[1]), int(short[2])) == (target.month, target.day):
                    selected.append(line)
            elif len(headers) < 2:
                headers.append(line)
        if not selected and not text.strip():
            return {"source": spec["path"], "target_date": target.isoformat(), "text": "目标日期未找到安排行。", "status": "missing"}
        text += "\n\n" + ("\n".join(headers + selected) if selected else "目标日期未找到安排行。")
    text = safe_text(text)
    return {"source": spec["path"], "target_date": target.isoformat(), "text": text, "status": "selected" if text else "no_allowed_sections"}


def weather_context(c, now):
    if not c["weather"]["enabled"]:
        return {"status": "disabled"}
    try:
        cache = read_json(c["root"] / ".local/openweather/latest.json")
        if cache.get("status") != "synced" or cache.get("date") != now.date().isoformat():
            raise ValueError()
        if dt.datetime.fromisoformat(cache["fetched_at"]).astimezone(c["tz"]).date() != now.date():
            raise ValueError()
        if cache.get("city") != c["weather"]["city"]:
            raise ValueError()
        periods = []
        for p in cache["periods"]:
            stamp = dt.datetime.fromisoformat(p["time"]).astimezone(c["tz"])
            if now <= stamp and stamp.date() == now.date():
                periods.append({k: p[k] for k in ("time", "temperature_c", "description", "precipitation_probability", "rain_3h_mm") if k in p})
        if not periods:
            raise ValueError()
        return {"status": "current", "city": c["weather"]["city"], "periods": periods, "source": "https://openweathermap.org/"}
    except (ServiceError, ValueError, KeyError, TypeError):
        return {"status": "unavailable"}


def whoop_context(c, now):
    if not c["whoop"]["enabled"]:
        return {"status": "disabled"}
    try:
        folder = inside(c["root"], c["whoop"]["data_dir"])
        pointer = read_json(folder / "最新同步.json")
        if pointer.get("status") != "synced" or dt.datetime.fromisoformat(pointer["synced_at"]).astimezone(c["tz"]).date() != now.date():
            raise ValueError()
        bundle = read_json(inside(folder, pointer["snapshot"]))
        sleeps = [s for s in bundle["records"]["sleep"] if s.get("nap") is False and s.get("id") is not None and dt.datetime.fromisoformat(s["end"].replace("Z", "+00:00")).astimezone(c["tz"]).date() == now.date()]
        if not sleeps:
            raise ValueError()
        sleep = max(sleeps, key=lambda s: s["end"])
        recovery = next((r for r in bundle["records"]["recovery"] if r.get("sleep_id") == sleep.get("id")), None)
        if recovery is None or sleep.get("score_state") != "SCORED" or recovery.get("score_state") != "SCORED":
            raise ValueError()
        stages = sleep.get("score", {}).get("stage_summary", {})
        stage_keys = ("total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli", "total_rem_sleep_time_milli")
        asleep = sum(stages[k] for k in stage_keys) if all(isinstance(stages.get(k), (int, float)) for k in stage_keys) else None
        return {"status": "current", "date": now.date().isoformat(), "source": "WHOOP", "sleep_end": sleep["end"], "sleep_minutes": round(asleep / 60000) if asleep is not None else None, "recovery_score": recovery.get("score", {}).get("recovery_score"), "limitations": "单日设备记录，不判断趋势、因果或诊断；不使用未闭合周期负荷"}
    except (ServiceError, ValueError, KeyError, TypeError):
        return {"status": "unavailable", "limitations": "没有可核验的当天主睡眠与恢复配对，不使用旧分数"}


def collect(c, kind, target, now, sync=None):
    now = now.astimezone(c["tz"])
    result = {"kind": kind, "target_date": target.isoformat(), "generated_for_local_date": now.date().isoformat(), "display_name": c["display_name"], "sources": []}
    persona = inside(c["root"], c["context"]["persona"])
    if persona.is_file() and persona.suffix.lower() == ".md":
        # Persona's report examples are not factual context and are never supplied.
        raw = persona.read_text(encoding="utf-8-sig")
        result["persona"] = safe_text(sections(raw, ["交流气质", "怎样说", "情境与分寸", "语气"]))
    for spec in c["context"]["sources"]:
        if spec.get("kinds") and kind not in spec["kinds"]:
            continue
        item = source_text(c, spec, target)
        if item is not None:
            result["sources"].append(item)
    # Explicit per-file journal sections; only records from the last seven local dates.
    journal_specs = list(c["context"].get("journals", []))
    if c["context"].get("journal_dir"):
        for offset in range(7):
            day = now.date() - dt.timedelta(days=offset)
            journal_specs.append({"date": day.isoformat(), "path": f"{c['context']['journal_dir']}/{day.year}/{day.isoformat()}.md", "sections": c["context"].get("journal_sections", ["简报可用"])})
    for spec in journal_specs:
        day = dt.date.fromisoformat(spec["date"])
        if now.date() - dt.timedelta(days=6) <= day <= now.date():
            item = source_text(c, spec, target)
            if item is not None:
                result["sources"].append(item)
    result["journal_policy"] = "仅包含明确批准的日记章节；未列入不代表当天无活动"
    if kind == "morning":
        result["weather"] = weather_context(c, now)
        result["whoop"] = whoop_context(c, now)
        for name in ("weather", "whoop"):
            if sync and sync.get(name) not in ("synced", "cached", "disabled"):
                result[name] = {"status": "unavailable"}
    if len(json.dumps(result, ensure_ascii=False)) > c["context"]["max_chars"]:
        raise ServiceError("context_too_large_select_fewer_sections")
    return result
