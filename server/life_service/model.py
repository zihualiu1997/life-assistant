import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from .core import ServiceError, endpoint, secret
from .context import PRIVATE


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ServiceError("model_redirect_refused")


def messages(kind, context):
    system = (Path(__file__).parent / "prompts" / (kind + ".txt")).read_text(encoding="utf-8")
    if kind == "evening":
        system += f"\n本次收信日期是{context.get('generated_for_local_date', '目标日期的前一天')}，你正在提前介绍明天{context['target_date']}的安排。首段必须写明：明天（{context['target_date']}）。不能把目标日期称作今天，不能用明天的星期问候今晚的用户。\n"
    feedback = {
        "evening_target_called_today": "上一稿日期表达错误。首段写明天与目标日期，逐句移除今天、今日；全文是明日预告。",
        "weather_source_missing": "上一稿漏掉天气来源。提到天气的段落必须包含 https://openweathermap.org/ 链接。",
        "unsupported_status_claim": "上一稿出现未经资料确认的预约或完成断言。保持待预约、可选、待确认；假设条件必须明确写若或如果。",
        "invalid_model_date": "上一稿目标日期错误。首段使用给定目标日期，不混入其他日期。",
    }
    if context.get("validation_feedback") in feedback:
        system += "\n本次重试须修正：" + feedback[context["validation_feedback"]]
    return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]


def validate(body, kind, context):
    if not isinstance(body, str) or not body.strip() or len(body) > 4000:
        raise ServiceError("invalid_model_body", True)
    target = context["target_date"]
    if target not in body[:200] or any(day != target for day in re.findall(r"\d{4}-\d{2}-\d{2}", body)):
        raise ServiceError("invalid_model_date", True)
    if PRIVATE.search(body) or "```" in body or re.search(r"<script|<iframe|data:", body, re.I):
        raise ServiceError("unsafe_model_body", True)
    if len(re.findall(r"(?m)^\s*(?:[-*•]|\d+[.)、])\s*", body)) > 3:
        raise ServiceError("too_many_tasks", True)
    source = json.dumps(context.get("sources", []), ensure_ascii=False)
    for claim in ("已预约", "预约成功", "已预订", "已完成"):
        if claim not in source:
            for match in re.finditer(claim, body):
                clause = re.split(r"[，,。；;\n]", body[:match.start()])[-1]
                conditional = re.search(r"(?:若|如果|假如|待|当)[^，。；\n]{0,16}$", clause) or body[match.end():].startswith("后")
                if not conditional:
                    raise ServiceError("unsupported_status_claim", True)
    if kind == "evening" and re.search(r"WHOOP|恢复分数|恢复评分|降雨概率|天气预报", body, re.I):
        raise ServiceError("evening_extra_analysis", True)
    if kind == "evening" and ("明天" not in body[:200] or re.search(r"今天|今日", body)):
        raise ServiceError("evening_target_called_today", True)
    if kind == "morning" and re.search(r"天气|气温|降雨|多云|晴朗|阵雨|紫外线", body) and context.get("weather", {}).get("status") == "current" and "https://openweathermap.org/" not in body:
        raise ServiceError("weather_source_missing", True)
    if "https://openweathermap.org/" in body and context.get("weather", {}).get("status") != "current":
        raise ServiceError("unsupported_weather", True)
    return body.strip() + "\n"


def generate(c, kind, context):
    if not c["approved"]["model_context"]:
        raise ServiceError("model_context_not_approved")
    url = endpoint(c)
    key = secret(c, c["model"]["secret"])
    payload = {"model": c["model"]["name"], "messages": messages(kind, context), "stream": False}
    if "enable_thinking" in c["model"]:
        payload["enable_thinking"] = c["model"]["enable_thinking"]
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=c["model"]["timeout"]) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ServiceError("model_response_too_large")
            data = json.loads(raw)
        choice = data["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ServiceError("model_incomplete_output", True)
        return validate(choice["message"]["content"], kind, context)
    except urllib.error.HTTPError as exc:
        raise ServiceError("model_http_" + str(exc.code), exc.code in (408, 429, 500, 502, 503, 504)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ServiceError("model_network_error", True) from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise ServiceError("invalid_model_response", True) from None
