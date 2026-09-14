import base64
import json
from types import SimpleNamespace
from uuid import uuid4

from atlas.notifications import push as push_module
from atlas.notifications.push import (
    MAX_PAYLOAD_BYTES,
    PushClient,
    VapidKeys,
    build_payload,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException


def _pem_and_raw() -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    raw = key.private_numbers().private_value.to_bytes(32, "big")
    return pem, base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_public_key_is_uncompressed_point_base64url_for_pem_and_raw_inputs(tmp_path) -> None:
    pem, raw = _pem_and_raw()
    (tmp_path / "key.pem").write_text(pem)
    (tmp_path / "key.raw").write_text(raw + "\n")
    from_pem = VapidKeys.load(tmp_path / "key.pem").public_key_b64url()
    from_raw = VapidKeys.load(tmp_path / "key.raw").public_key_b64url()
    assert from_pem == from_raw
    assert len(from_pem) == 87 and "=" not in from_pem
    assert base64.urlsafe_b64decode(from_pem + "=")[0] == 0x04


def test_empty_key_file_is_rejected(tmp_path) -> None:
    (tmp_path / "empty").write_text("\n")
    try:
        VapidKeys.load(tmp_path / "empty")
    except ValueError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_payload_is_bounded_and_carries_only_display_fields() -> None:
    row = SimpleNamespace(id=uuid4(), title="t", body="b" * 10_000, severity="warning", thread_key="k",
        detail={"open_url": "/control", "code": "SECRET", "huge": "x" * 5000}, push_quiet=True)
    payload = build_payload(row)
    assert set(payload) == {"id", "title", "body", "severity", "tag", "url", "quiet"}
    assert payload["url"] == "/control" and payload["tag"] == "k" and payload["quiet"] is True
    assert "SECRET" not in json.dumps(payload)
    assert len(json.dumps(payload, ensure_ascii=False).encode()) <= MAX_PAYLOAD_BYTES + 8


def test_send_maps_push_service_failures_to_status_codes(monkeypatch, tmp_path) -> None:
    pem, _ = _pem_and_raw()
    (tmp_path / "key.pem").write_text(pem)
    client = PushClient(VapidKeys.load(tmp_path / "key.pem"), "mailto:owner@example.com")
    calls = []

    def fake_webpush(**kwargs):
        calls.append(kwargs)
        if kwargs["subscription_info"]["endpoint"].endswith("gone"):
            raise WebPushException("gone", response=SimpleNamespace(status_code=410))
        return SimpleNamespace(status_code=201)

    monkeypatch.setattr(push_module, "webpush", fake_webpush)
    ok = client.send({"endpoint": "https://push.example/ok", "keys": {"p256dh": "a", "auth": "b"}}, {"title": "t"})
    gone = client.send({"endpoint": "https://push.example/gone", "keys": {"p256dh": "a", "auth": "b"}}, {"title": "t"})
    assert (ok, gone) == (201, 410)
    assert calls[0]["vapid_claims"] == {"sub": "mailto:owner@example.com"} and calls[0]["timeout"] == 10
    assert calls[0]["vapid_private_key"] is client.keys.vapid
