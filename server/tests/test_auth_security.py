from starlette.requests import Request

from app.auth import request_is_loopback


def _request(peer: str, headers: dict[str, str] | None = None) -> Request:
    raw_headers = [(key.lower().encode("ascii"), value.encode("ascii")) for key, value in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/local-session",
            "headers": raw_headers,
            "client": (peer, 49152),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "query_string": b"",
        }
    )


def test_loopback_detection_trusts_transport_peer() -> None:
    assert request_is_loopback(_request("127.0.0.1"))
    assert request_is_loopback(_request("::1"))


def test_loopback_detection_rejects_forged_browser_headers() -> None:
    request = _request(
        "203.0.113.8",
        {
            "host": "127.0.0.1:8000",
            "origin": "http://localhost:5173",
            "referer": "http://[::1]/",
        },
    )
    assert not request_is_loopback(request)
