from __future__ import annotations

import asyncio
from dataclasses import replace
from io import BytesIO

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app import official_news_pdf as pdf
from app.settings import settings

PAGE = 'https://www.mpa.gov.sg/media-centre/details/shipping-advisory'
URL = 'https://www.mpa.gov.sg/api/media/6b5685e5-5d77-42da-97a5-03a250586e78/sa_no_23_of_2026.pdf'


def document(text='NO. 23 OF 2026 Shipping security advisory.', pages=1, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(600, 800)
        if text:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                     NameObject('/Subtype'): NameObject('/Type1'),
                                     NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({
                NameObject('/Font'): DictionaryObject({NameObject('/F1'): font}),
            })
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 12 Tf 10 750 Td ({text}) Tj ET'.encode())
            page[NameObject('/Contents')] = writer._add_object(stream)
    if encrypted:
        writer.encrypt('secret')
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def test_attachment_link_is_bound_to_official_detail_and_pdf_wrapper():
    html = f'<nav><a href="{URL}">Unrelated PDF</a></nav><div class="pdf-wrapper"><a href="{URL}">Current PDF</a></div>'
    assert pdf.attachment_url(html, PAGE) == URL
    assert not pdf.attachment_url(html, 'https://unapproved.example/media-centre/details/article')
    assert not pdf.attachment_url(html, 'https://www.mpa.gov.sg/media-centre')
    assert not pdf.attachment_url(f'<nav><a href="{URL}">PDF</a></nav>', PAGE)
    external = '<div class="pdf-wrapper"><a href="https://attacker.example/file.pdf">PDF</a></div>'
    assert not pdf.attachment_url(external, PAGE)


def test_ambiguous_attachments_are_not_silently_chosen():
    html = f'<div class="pdf-wrapper"><a href="{URL}">One</a><a href="{URL.replace("sa_no", "other")}">Two</a></div>'
    with pytest.raises(pdf.OfficialPdfError, match='ambiguous'):
        pdf.attachment_url(html, PAGE)


def test_parser_keeps_every_page_and_final_correction():
    result = asyncio.run(pdf.parse_pdf(document('NO. 23 OF 2026 Final correction: the port remains open.', pages=2)))
    assert result['pages'] == 2
    assert result['text'].count('Final correction: the port remains open.') == 2


@pytest.mark.parametrize(('data', 'reason'), [
    (b'not a pdf', 'invalid_file'),
    (b'%PDF-' + b'x' * pdf.MAX_PDF_BYTES, 'too_large'),
    (document(encrypted=True), 'encrypted'),
    (document(text=''), 'text_unavailable'),
    (document(pages=21), 'page_limit'),
    (b'%PDF-1.7\ncorrupt', 'parse_failed'),
])
def test_parser_rejects_incomplete_encrypted_oversized_and_corrupt_documents(data, reason):
    with pytest.raises(pdf.OfficialPdfError, match=reason):
        asyncio.run(pdf.parse_pdf(data))


def test_parse_timeout_kills_and_reaps_child(monkeypatch):
    class Child:
        returncode = None
        killed = False
        waited = False

        async def communicate(self, data):
            await asyncio.sleep(10)

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waited = True
            self.returncode = -9

    child = Child()

    async def start(*args, **kwargs):
        return child

    monkeypatch.setattr(pdf.asyncio, 'create_subprocess_exec', start)
    monkeypatch.setattr(pdf, 'PDF_PARSE_TIMEOUT', 0.01)
    with pytest.raises(TimeoutError):
        asyncio.run(pdf.parse_pdf(b'%PDF-test'))
    assert child.killed and child.waited


def test_attachment_content_must_match_parent_announcement(monkeypatch):
    async def fetch(*args):
        return document('NO. 9 OF 2026 Other circular.')

    monkeypatch.setattr(pdf, 'fetch_pdf', fetch)
    html = f'<div class="pdf-wrapper"><a href="{URL}">PDF</a></div>'
    with pytest.raises(pdf.OfficialPdfError, match='title_mismatch'):
        asyncio.run(pdf.read_attachment(html, PAGE, 'No. 23 of 2026 - Advisory', {}))


@pytest.mark.parametrize(('status', 'headers', 'data', 'reason'), [
    (302, {'location': 'https://attacker.example/file.pdf'}, b'', 'unapproved_url'),
    (200, {'content-type': 'text/html'}, b'Login required', 'content_type'),
    (200, {'content-type': 'application/pdf', 'content-length': str(pdf.MAX_PDF_BYTES + 1)}, b'', 'too_large'),
    (200, {'content-type': 'application/pdf'}, b'x' * (pdf.MAX_PDF_BYTES + 1), 'too_large'),
])
def test_fetch_checks_redirect_type_and_stream_size(monkeypatch, status, headers, data, reason):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, headers=headers, content=data)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(pdf.httpx, 'AsyncClient', lambda **kwargs: client_class(
        transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(pdf, 'settings', replace(settings, outbound_hosts=('www.mpa.gov.sg',)))
    with pytest.raises(pdf.OfficialPdfError, match=reason):
        asyncio.run(pdf.fetch_pdf(URL, {}))
    assert calls == [URL]


def test_enrichment_preserves_parent_identity_and_records_document_provenance(monkeypatch):
    from app import news

    source = news.get_news_source('mpa_press_releases')
    item = news.RawNewsItem(source.source_id, 'A', PAGE, 'No. 23 of 2026 - Shipping advisory',
                           published_at='2026-10-05', first_seen_at='2026-10-06T00:00:00Z')

    async def fetch(*args, **kwargs):
        return '<html><title>Shipping advisory</title></html>', 'text/html'

    async def attachment(*args):
        return {'text': 'Confirmed shipboard security advisory. ' * 30,
                'url': URL, 'sha256': 'a' * 64, 'pages': 2}

    monkeypatch.setattr(news, '_fetch_text', fetch)
    monkeypatch.setattr(news, '_should_fetch_detail', lambda *args, **kwargs: True)
    monkeypatch.setattr(pdf, 'read_attachment', attachment)
    rows, errors = asyncio.run(news._enrich_items_with_details([item], source=source))
    assert not errors
    result = rows[0]
    assert result.url == PAGE and result.first_seen_at == item.first_seen_at
    assert result.published_at == '2026-10-05'
    assert result.raw_text.endswith('Confirmed shipboard security advisory.')
    assert result.body_method == 'official_pdf_mpa' and result.body_document_url == URL
    assert result.body_document_sha256 == 'a' * 64
    assert news.classify_summary_input(result, source=source)['eligible_for_summary']


def test_pdf_failure_label_remains_chinese():
    from app.workbench_events import _source_content_label
    assert _source_content_label('partial_text', 'official_pdf_title_mismatch') == '官方附件与公告编号不一致，等待复核'
