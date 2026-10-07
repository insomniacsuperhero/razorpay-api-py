"""Outbound channels for dispute nudges. Each sender returns a provider message id or raises MessageError.

  email     Resend HTTP API (free tier; with the shared onboarding@resend.dev sender it only delivers to
            the email address that owns the Resend account, which is enough for the demo)
  whatsapp  Meta WhatsApp Cloud API. With WHATSAPP_TEMPLATE set, sends that approved template with four
            body parameters (amount, reason, deadline, link). Without it, sends a free-form text message,
            which WhatsApp only delivers if the merchant messaged the business number in the last 24 h.
"""
import html

import httpx

from app.config import get_settings


class MessageError(Exception):
    pass


def _post(url: str, transport: httpx.BaseTransport | None, **kw) -> dict:
    try:
        with httpx.Client(timeout=15.0, transport=transport) as c:
            res = c.post(url, **kw)
    except httpx.HTTPError as e:
        raise MessageError(f"{e.__class__.__name__}: network error") from e
    if res.status_code >= 400:
        try:
            body = res.json()
            msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("message")
        except ValueError:
            msg = res.text[:200]
        raise MessageError(f"HTTP {res.status_code}: {msg or res.text[:200]}")
    return res.json()


class EmailSender:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    @staticmethod
    def configured() -> bool:
        s = get_settings()
        return bool(s.resend_api_key and s.merchant_email)

    def send(self, subject: str, text: str, link: str | None, cta: str) -> str:
        s = get_settings()
        button = (f'<p style="margin:24px 0"><a href="{html.escape(link)}" style="background:#1566f1;color:#fff;'
                  f'padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:600">{html.escape(cta)}</a></p>'
                  if link else "")
        body_html = (f'<div style="font-family:-apple-system,Segoe UI,sans-serif;font-size:15px;color:#1b2b44;max-width:520px">'
                     f'<p>{html.escape(text).replace(chr(10), "<br>")}</p>{button}'
                     f'<p style="color:#64748b;font-size:12px">Only the files you pick are read. Nothing is sent to the bank '
                     f'until you approve it.</p></div>')
        data = _post("https://api.resend.com/emails", self.transport,
                     headers={"Authorization": f"Bearer {s.resend_api_key}"},
                     json={"from": s.notify_email_from, "to": [s.merchant_email], "subject": subject,
                           "html": body_html, "text": text + (f"\n\n{cta}: {link}" if link else "")})
        return str(data.get("id", ""))


class WhatsAppSender:
    API = "https://graph.facebook.com/v21.0"

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport

    @staticmethod
    def configured() -> bool:
        s = get_settings()
        return bool(s.whatsapp_token and s.whatsapp_phone_number_id and s.merchant_whatsapp)

    def send(self, text: str, params: list[str], link: str | None) -> str:
        s = get_settings()
        to = "".join(ch for ch in s.merchant_whatsapp if ch.isdigit())
        if s.whatsapp_template:
            payload = {"messaging_product": "whatsapp", "to": to, "type": "template", "template": {
                "name": s.whatsapp_template, "language": {"code": s.whatsapp_template_lang},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": p[:1000]} for p in params]}]}}
        else:
            payload = {"messaging_product": "whatsapp", "to": to, "type": "text",
                       "text": {"preview_url": True, "body": text + (f"\n\nAdd proof: {link}" if link else "")}}
        data = _post(f"{self.API}/{s.whatsapp_phone_number_id}/messages", self.transport,
                     headers={"Authorization": f"Bearer {s.whatsapp_token}"}, json=payload)
        return str(((data.get("messages") or [{}])[0]).get("id", ""))


class PushSender:
    """Web Push (VAPID) to every device where the merchant turned on notifications in the installed app."""

    @staticmethod
    def configured() -> bool:
        s = get_settings()
        return bool(s.vapid_public_key and s.vapid_private_key)

    def send_one(self, sub: dict, payload: dict) -> int:
        """Returns the push service's HTTP status. 404/410 mean the subscription is gone."""
        import json

        from py_vapid import Vapid
        from pywebpush import WebPushException, webpush
        s = get_settings()
        try:
            res = webpush(subscription_info=sub, data=json.dumps(payload), ttl=86400, timeout=10,
                          vapid_private_key=Vapid.from_raw(s.vapid_private_key.encode()),
                          vapid_claims={"sub": s.vapid_subject})
            return getattr(res, "status_code", 201)
        except WebPushException as e:
            code = getattr(e.response, "status_code", 0) if e.response is not None else 0
            if code in (404, 410):
                return code
            raise MessageError(f"push failed: HTTP {code or '?'}") from e


def mask_email(e: str) -> str:
    if "@" not in e:
        return ""
    name, dom = e.split("@", 1)
    return f"{name[:1]}•••@{dom}"


def mask_phone(p: str) -> str:
    d = "".join(ch for ch in p if ch.isdigit())
    return f"•••••{d[-4:]}" if len(d) >= 4 else ""


# replaced in tests
email_sender = EmailSender()
whatsapp_sender = WhatsAppSender()
push_sender = PushSender()
