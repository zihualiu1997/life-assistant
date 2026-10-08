import datetime
from email.message import EmailMessage
import json
import os
from pathlib import Path
import smtplib
import ssl
import uvicorn
from .broker import create_broker
from .mail import MailRelay
from .store import Fleet


def main():
    root = Path(os.environ.get("LIFE_FLEET_ROOT", "/operator"))
    config = json.loads((root / "operator.json").read_text(encoding="utf-8"))
    fleet = Fleet(root)

    def send(recipient, subject, body, key):
        m = config["smtp"]
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = m["sender"], recipient, subject
        message["Message-ID"] = f"<life-{key}@{m['sender'].split('@')[-1]}>"
        message.set_content(body)
        if m["tls"] == "ssl":
            connection = smtplib.SMTP_SSL(m["host"], m["port"], timeout=30, context=ssl.create_default_context())
        elif m["tls"] == "starttls":
            connection = smtplib.SMTP(m["host"], m["port"], timeout=30)
            connection.ehlo()
            connection.starttls(context=ssl.create_default_context())
            connection.ehlo()
        else: raise ValueError("smtp_tls_required")
        try:
            connection.login(m["username"], m["password"])
            if connection.send_message(message): raise ValueError("recipient_refused")
        finally: connection.close()

    app = create_broker(fleet, config["base_url"], config["api_key"], {config["model"], config["asr_model"]}, mail_relay=MailRelay(fleet, send), voice_config=config.get("voice"))
    uvicorn.run(app, host="0.0.0.0", port=18933, access_log=False, log_level="critical")


if __name__ == "__main__": main()
