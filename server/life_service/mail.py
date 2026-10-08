import datetime as dt
from email.message import EmailMessage
from email.utils import format_datetime
import hashlib
import smtplib
import ssl

from .core import ServiceError, delivery_state, in_window, inside, lock, read_json, receipt_path, report_path, secret, write_json


def check_mail(c, kind):
    if not c["approved"][kind]:
        raise ServiceError("content_not_approved")
    from life_app import managed
    if managed.enabled():
        from life_app.store import Store
        if not Store(c["root"]).get("email_verified"): raise ServiceError("mail_connection_not_verified")
        return
    m = c["mail"]
    if any(not m.get(k) for k in ("host", "username", "sender", "recipient")) or m.get("tls") not in ("ssl", "starttls"):
        raise ServiceError("mail_not_configured")
    for key in ("sender", "recipient"):
        if "@" not in m[key] or any(ch in m[key] for ch in "\r\n,;"):
            raise ServiceError("invalid_mail_address")
    receipt = read_json(inside(c["root"], m["connection_receipt"]))
    if receipt.get("smtp_test") != "accepted" or receipt.get("recipient") != m["recipient"]:
        raise ServiceError("mail_connection_not_verified")
    # New receipts bind the complete transport; legacy receipts remain compatible.
    if "host" in receipt and any(receipt.get(k) != m[k] for k in ("host", "port", "tls", "username", "sender")):
        raise ServiceError("mail_transport_changed_retest")


def smtp_send(c, body, subject, key, before_data=None):
    m = c["mail"]
    password = secret(c, m["secret"])
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = m["sender"], m["recipient"], subject
    msg["Date"] = format_datetime(dt.datetime.now(c["tz"]))
    msg["Message-ID"] = f"<life-{key}@{m['sender'].split('@')[-1]}>"
    msg.set_content(body)
    smtp = None
    try:
        if m["tls"] == "ssl":
            smtp = smtplib.SMTP_SSL(m["host"], m["port"], context=ssl.create_default_context(), timeout=30)
        else:
            smtp = smtplib.SMTP(m["host"], m["port"], timeout=30)
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        smtp.login(m["username"], password)
        if before_data is not None and not before_data():
            raise ServiceError("expired_before_smtp_data")
        if smtp.send_message(msg):
            raise ServiceError("smtp_recipient_refused")
    finally:
        if smtp is not None:
            try:
                smtp.close()
            except OSError:
                pass


def send(c, kind, run_day, target, clock):
    # Caller holds the shared mail lock across archive and send.
    status = delivery_state(c, kind, target)
    if status:
        return status
    check_mail(c, kind)
    if not in_window(c, kind, run_day, clock()):
        return "expired"
    body = report_path(c, kind, target).read_text(encoding="utf-8")
    if not body.strip():
        raise ServiceError("empty_report")
    from life_app import managed
    if managed.enabled():
        path = receipt_path(c, kind, target)
        state = {"date": target.isoformat(), "status": "sending", "sha256": hashlib.sha256(body.encode()).hexdigest()}
        write_json(path, state)
        try:
            result = managed.call("mail/submit", {"kind": kind, "day": target.isoformat(), "subject": f"生活助手｜{target.isoformat()}", "body": body})
            accepted = result.get("status") == "smtp_accepted"
            state.update(status="sent" if accepted else "unknown", relay_id=result.get("id"))
        except Exception: state["status"] = "unknown"
        write_json(path, state)
        return "sent" if state["status"] == "sent" else "needs_review"
    secret(c, c["mail"]["secret"])  # Fail before the ambiguous SMTP phase.
    key = hashlib.sha256((c["mail"]["recipient"] + ("evening-" if kind == "evening" else "") + target.isoformat()).encode()).hexdigest()
    state = {"date": target.isoformat(), "status": "sending", "sha256": hashlib.sha256(body.encode()).hexdigest(), "message_id": f"<life-{key}@{c['mail']['sender'].split('@')[-1]}>"}
    path = receipt_path(c, kind, target)
    write_json(path, state)
    if not in_window(c, kind, run_day, clock()):
        # No SMTP attempt; retain a reviewable receipt instead of silently deleting it.
        state["status"] = "not_sent_expired"
        write_json(path, state)
        return "expired"
    try:
        smtp_send(c, body, f"{c['display_name']} 的{'晨报' if kind == 'morning' else '明日安排'}｜{target.isoformat()}", key, before_data=lambda: in_window(c, kind, run_day, clock()))
    except Exception as exc:
        expired = isinstance(exc, ServiceError) and exc.code == "expired_before_smtp_data"
        state["status"] = "not_sent_expired" if expired else "unknown"
        write_json(path, state)
        return "expired" if expired else "needs_review"
    state.update(status="sent", accepted_at=clock().isoformat())
    write_json(path, state)
    return "sent"


def test_connection(c):
    """Explicit CLI command only; contains no personal life context."""
    from life_app import managed
    if managed.enabled():
        from life_app.store import Store
        if not Store(c["root"]).get("email_verified"): raise ServiceError("mail_connection_not_verified")
        result = managed.call("mail/submit", {"kind": "test", "day": dt.datetime.now(c["tz"]).date().isoformat(), "subject": "生活助手连接测试", "body": "生活助手连接测试；不含生活资料。"})
        if result.get("status") != "smtp_accepted": raise ServiceError("connection_test_unknown_check_inbox")
        return {"status": "accepted"}
    with lock(c["root"], "mail"):
        try:
            smtp_send(c, "生活助手连接测试；不含生活资料。", "生活助手连接测试", "connection-" + dt.datetime.now().strftime("%Y%m%d%H%M%S"))
        except Exception:
            raise ServiceError("connection_test_unknown_check_inbox") from None
        result = {k: c["mail"][k] for k in ("recipient", "host", "port", "tls", "username", "sender")}
        result.update(smtp_test="accepted", at=dt.datetime.now(c["tz"]).isoformat())
        write_json(inside(c["root"], c["mail"]["connection_receipt"]), result)
        return {"status": "accepted"}
