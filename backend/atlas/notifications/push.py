"""Web Push (VAPID) client. Blocking; callers wrap sends in a thread."""
import base64
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

MAX_PAYLOAD_BYTES = 3_000


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class VapidKeys:
    def __init__(self, vapid: Vapid) -> None:
        self.vapid = vapid

    @classmethod
    def load(cls, path: Path) -> VapidKeys:
        text = path.read_text().strip()
        if not text:
            raise ValueError("VAPID private key file is empty")
        if "BEGIN" in text:
            return cls(Vapid.from_pem(text.encode()))
        return cls(Vapid.from_string(text))

    def public_key_b64url(self) -> str:
        point = self.vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return _b64url(point)


def build_payload(row: Any) -> dict[str, Any]:
    """Only what the phone needs to show and open. Never the full detail."""
    detail = dict(getattr(row, "detail", None) or {})
    url = detail.get("open_url") or "/"
    payload = {
        "id": str(row.id), "title": row.title, "body": row.body or "", "severity": row.severity,
        "tag": row.thread_key or str(row.id), "url": str(url), "quiet": bool(getattr(row, "push_quiet", False)),
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    if len(encoded) > MAX_PAYLOAD_BYTES:
        overflow = len(encoded) - MAX_PAYLOAD_BYTES
        payload["body"] = payload["body"][: max(0, len(payload["body"]) - overflow - 1)] + "…"
    return payload


class PushClient:
    def __init__(self, keys: VapidKeys, subject: str) -> None:
        self.keys = keys
        self.subject = subject

    def send(self, subscription_info: dict[str, Any], payload: dict[str, Any], *, ttl: int = 900) -> int:
        """Return the push service HTTP status; 0 for transport failure."""
        try:
            response = webpush(subscription_info=subscription_info, data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=self.keys.vapid, vapid_claims={"sub": self.subject}, ttl=ttl, timeout=10)
        except WebPushException as exc:
            status = getattr(exc, "status_code", None)
            if status is None and exc.response is not None:
                status = getattr(exc.response, "status_code", None)
            return int(status or 0)
        return int(getattr(response, "status_code", 0) or 0)
