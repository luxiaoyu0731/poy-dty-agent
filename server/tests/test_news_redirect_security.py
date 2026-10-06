from __future__ import annotations

import asyncio

import pytest

from app import news


class _Response:
    def __init__(self, url: str, status_code: int, *, location: str = "", text: str = "ok") -> None:
        self.url = url
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": "text/html"}
        if location:
            self.headers["location"] = location

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise news.httpx.HTTPStatusError(
                "failed",
                request=news.httpx.Request("GET", self.url),
                response=news.httpx.Response(self.status_code, request=news.httpx.Request("GET", self.url)),
            )


class _Client:
    def __init__(self, responses: list[_Response], calls: list[str], *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.responses = responses
        self.calls = calls

    async def __aenter__(self):  # noqa: ANN201
        return self

    async def __aexit__(self, *args) -> None:  # noqa: ANN002
        return None

    async def get(self, url: str, **kwargs) -> _Response:  # noqa: ANN003
        self.calls.append(url)
        return self.responses.pop(0)


def test_news_fetch_rejects_redirect_to_non_allowlisted_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    responses = [_Response("https://www.eia.gov/start", 302, location="https://attacker.invalid/secret")]
    monkeypatch.setattr(news.httpx, "AsyncClient", lambda *args, **kwargs: _Client(responses, calls))

    with pytest.raises(ValueError, match="OUTBOUND_FETCH_HOSTS"):
        asyncio.run(news._fetch_text("https://www.eia.gov/start"))

    assert calls == ["https://www.eia.gov/start"]


def test_news_fetch_validates_and_follows_allowlisted_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    responses = [
        _Response("https://www.eia.gov/start", 302, location="/pressroom/final"),
        _Response("https://www.eia.gov/pressroom/final", 200, text="official release"),
    ]
    monkeypatch.setattr(news.httpx, "AsyncClient", lambda *args, **kwargs: _Client(responses, calls))

    text, content_type = asyncio.run(news._fetch_text("https://www.eia.gov/start"))

    assert text == "official release"
    assert content_type == "text/html"
    assert calls == ["https://www.eia.gov/start", "https://www.eia.gov/pressroom/final"]


def test_news_fetch_rejects_redirect_loop_after_bounded_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    responses = [
        _Response("https://www.eia.gov/loop", 302, location="/loop")
        for _ in range(news.MAX_FETCH_REDIRECTS + 1)
    ]
    monkeypatch.setattr(news.httpx, "AsyncClient", lambda *args, **kwargs: _Client(responses, calls))

    with pytest.raises(ValueError, match="redirect limit exceeded"):
        asyncio.run(news._fetch_text("https://www.eia.gov/loop"))

    assert len(calls) == news.MAX_FETCH_REDIRECTS + 1
