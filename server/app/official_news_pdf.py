"""Bounded text-only reading of public MPA attachments; never decrypt or OCR."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .settings import settings

MAX_PDF_BYTES = 3 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_PDF_TEXT = 1_000_000
PDF_PARSE_TIMEOUT = 10


class OfficialPdfError(ValueError):
    pass


class _AttachmentLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: set[str] = set()
        self.stack: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        active = any(flag for _, flag in self.stack) or 'pdf-wrapper' in (data.get('class') or '').split()
        if active and tag == 'a' and data.get('href'):
            self.links.add(data['href'])
        if tag not in {'br', 'img', 'input', 'meta', 'link', 'hr', 'source', 'wbr'}:
            self.stack.append((tag, active))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                break


def attachment_url(html: str, page_url: str) -> str:
    page = urlparse(page_url)
    if (page.hostname != 'www.mpa.gov.sg' or page.scheme != 'https'
            or not page.path.startswith('/media-centre/details/')):
        return ''
    parser = _AttachmentLinks()
    parser.feed(html)
    urls = {urljoin(page_url, link) for link in parser.links}
    valid = {url for url in urls if _valid_pdf_url(url)}
    if len(valid) > 1:
        raise OfficialPdfError('official_pdf_ambiguous_attachment')
    return next(iter(valid), '')


def _valid_pdf_url(url: str) -> bool:
    parsed = urlparse(url)
    return bool(parsed.scheme == 'https' and parsed.hostname == 'www.mpa.gov.sg'
                and not parsed.username and not parsed.password and parsed.port in {None, 443}
                and re.fullmatch(r'/api/media/[0-9a-f-]{36}/[\w.-]+\.pdf', parsed.path, re.I))


async def fetch_pdf(url: str, headers: dict[str, str]) -> bytes:
    current = url
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        for redirect in range(4):
            if not _valid_pdf_url(current):
                raise OfficialPdfError('official_pdf_unapproved_url')
            settings.require_outbound_url_allowed(current)
            async with client.stream('GET', current, headers=headers) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect == 3 or not response.headers.get('location'):
                        raise OfficialPdfError('official_pdf_redirect_limit')
                    current = urljoin(current, response.headers['location'])
                    continue
                response.raise_for_status()
                if 'application/pdf' not in response.headers.get('content-type', '').lower():
                    raise OfficialPdfError('official_pdf_invalid_content_type')
                declared = response.headers.get('content-length')
                if declared and int(declared) > MAX_PDF_BYTES:
                    raise OfficialPdfError('official_pdf_too_large')
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_PDF_BYTES:
                        raise OfficialPdfError('official_pdf_too_large')
                return bytes(chunks)
    raise OfficialPdfError('official_pdf_redirect_limit')


async def parse_pdf(data: bytes) -> dict:
    if len(data) > MAX_PDF_BYTES:
        raise OfficialPdfError('official_pdf_too_large')
    if not data.startswith(b'%PDF-'):
        raise OfficialPdfError('official_pdf_invalid_file')
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'app.official_news_pdf',
        cwd=str(Path(__file__).resolve().parents[1]),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(data), PDF_PARSE_TIMEOUT)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode != 0 or len(stdout) > MAX_PDF_TEXT * 8:
        raise OfficialPdfError('official_pdf_parse_failed')
    result = json.loads(stdout)
    if result.get('error'):
        raise OfficialPdfError(result['error'])
    return result


async def read_attachment(html: str, page_url: str, title: str, headers: dict[str, str]) -> dict | None:
    url = attachment_url(html, page_url)
    if not url:
        return None
    data = await fetch_pdf(url, headers)
    try:
        result = await parse_pdf(data)
    except TimeoutError as exc:
        raise OfficialPdfError('official_pdf_parse_timeout') from exc
    validate_parent_title(result['text'], title)
    return {**result, 'url': url, 'sha256': hashlib.sha256(data).hexdigest()}


def validate_parent_title(text: str, title: str) -> None:
    # Bind the document's circular/advisory number to the parent announcement.
    number = re.search(r'No\.\s*(\d+)\s+of\s+(\d{4})', title, re.I)
    if not number or not re.search(
        rf'\bNo\.?\s*0*{int(number[1])}\s+(?:of\s+)?{number[2]}\b', text[:1500], re.I,
    ):
        raise OfficialPdfError('official_pdf_title_mismatch')


def _worker() -> None:
    # The parser is isolated so CPU/wall-clock/memory limits actually stop it.
    import resource
    from io import BytesIO

    from pypdf import PdfReader, apply_configuration

    resource.setrlimit(resource.RLIMIT_CPU, (8, 9))
    # Production runs in Linux containers. macOS exposes these constants but
    # rejects address-space/data limits; local validation retains CPU/time caps.
    if sys.platform == "linux":
        _, existing_hard_limit = resource.getrlimit(resource.RLIMIT_AS)
        memory_limit = 512 * 1024 * 1024
        if existing_hard_limit != resource.RLIM_INFINITY:
            memory_limit = min(memory_limit, existing_hard_limit)
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    try:
        data = sys.stdin.buffer.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES:
            raise OfficialPdfError('official_pdf_too_large')
        with apply_configuration(maximum_declared_stream_length=MAX_PDF_BYTES):
            reader = PdfReader(BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise OfficialPdfError('official_pdf_encrypted')
            pages = len(reader.pages)
            if not 1 <= pages <= MAX_PDF_PAGES:
                raise OfficialPdfError('official_pdf_page_limit')
            texts = [page.extract_text() or '' for page in reader.pages]
            if any(not text.strip() for text in texts):
                raise OfficialPdfError('official_pdf_text_unavailable')
            text = '\n\n'.join(texts)
            if len(text) > MAX_PDF_TEXT:
                raise OfficialPdfError('official_pdf_text_limit')
        result = {'text': text, 'pages': pages}
    except OfficialPdfError as exc:
        result = {'error': str(exc)}
    except Exception:
        result = {'error': 'official_pdf_parse_failed'}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    _worker()
