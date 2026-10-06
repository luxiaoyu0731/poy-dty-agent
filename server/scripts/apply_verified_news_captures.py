"""Apply explicitly approved, hash-bound public-page captures without refetching.

Default dry-run is read-only. No LLM calls or retry-counter resets. Source and
article identity, current content hash, full-body quality and access gates all
must pass; application preserves first_seen_at and journals old mutable rows.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app import news  # noqa: E402
from app.official_news_pdf import attachment_url, parse_pdf  # noqa: E402
from app.settings import settings  # noqa: E402
from scripts.backfill_news_article_bodies import item_for, persist_recovered_body, source_for, write_json  # noqa: E402

MAX_CAPTURES = 20


def capture_bytes(path: Path, expected_hash: str, limit: int) -> bytes:
    if path.stat().st_size > limit:
        raise ValueError('capture_size_limit')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError('capture_hash_mismatch')
    return data


async def prepare(row: dict, capture: dict, base: Path) -> news.RawNewsItem:
    item = item_for(row)
    source = source_for(row)
    if news._id('art', item.discovery_url or item.url or item.title) != row['article_id']:
        raise ValueError('capture_article_identity_mismatch')
    if source is None or capture['url'] != item.url or not news._should_fetch_detail(item, source=source):
        raise ValueError('capture_source_not_permitted')
    html = capture_bytes(base / capture['html_file'], capture['html_sha256'], 5 * 1024 * 1024).decode('utf-8')
    if news._has_access_barrier(html) or news._page_disallows_extraction(html):
        raise ValueError('capture_access_restricted')
    detail = news._extract_article_detail(html, source_url=item.url)
    if news.titles_conflict(item.title, detail['headline']):
        raise ValueError('capture_title_mismatch')
    document_url = document_hash = ''
    if capture.get('pdf_file'):
        if source.source_id != 'mpa_press_releases':
            raise ValueError('capture_pdf_source_not_permitted')
        document_url = attachment_url(html, item.url)
        if not document_url or document_url != capture['pdf_url']:
            raise ValueError('capture_pdf_link_mismatch')
        from app.official_news_pdf import MAX_PDF_BYTES
        data = capture_bytes(base / capture['pdf_file'], capture['pdf_sha256'], MAX_PDF_BYTES)
        result = await parse_pdf(data)
        from app.official_news_pdf import validate_parent_title
        validate_parent_title(result['text'], item.title)
        detail.update(text=result['text'], body_method='official_pdf_mpa', body_reason='')
        document_hash = capture['pdf_sha256']
    title = detail['headline'] or item.title
    prepared = replace(item, title=title, raw_text=news.clean_event_source_text(detail['text']),
                       body_method=detail['body_method'], body_reason=detail['body_reason'],
                       body_truncated=False, detail_reason='', body_document_url=document_url,
                       body_document_sha256=document_hash)
    if not news.classify_summary_input(prepared, source=source)['eligible_for_summary']:
        raise ValueError('capture_not_full_body')
    return prepared


async def run(db: Path, manifest: Path, output: Path, *, apply: bool = False) -> dict:
    document = json.loads(manifest.read_text())
    captures = document['captures']
    if document.get('schema') != 'verified-news-captures.v1' or not 1 <= len(captures) <= MAX_CAPTURES:
        raise ValueError('invalid_capture_manifest')
    ids = [c['article_id'] for c in captures]
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate_capture_identity')
    now = datetime.now(UTC)
    prepared = []
    # Preflight the complete fixed manifest before any application.
    with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=10)) as con:
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        for capture in captures:
            row = con.execute('SELECT * FROM news_articles WHERE article_id=? AND '
                              'julianday(first_seen_at)>=julianday(?)',
                              (capture['article_id'], (now - timedelta(days=7)).isoformat())).fetchone()
            if row is None or row['content_hash'] != capture['expected_content_hash']:
                raise ValueError('capture_current_revision_mismatch')
            summary = con.execute('SELECT * FROM event_ai_summaries WHERE article_id=?',
                                  (capture['article_id'],)).fetchone()
            if summary and summary['summary_status'] == 'processing':
                raise ValueError('capture_summary_in_progress')
            row = dict(row)
            item = await prepare(row, capture, manifest.parent)
            prepared.append((row, item, dict(summary) if summary else None))
    results = []
    if apply:
        object.__setattr__(settings, 'sqlite_path', str(db))
    for row, item, summary in prepared:
        status = 'validated'
        if apply:
            journal = output.parent / 'body-recovery-journal' / f"{row['article_id']}-{row['content_hash'][:16]}.json"
            if not journal.exists():
                write_json(journal, {'article': row, 'summary': summary,
                                     'capture_manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest()})
            await persist_recovered_body(item, source=source_for(row), row=row)
            status = 'applied'
        results.append({'article_id': row['article_id'], 'status': status,
                        'body_chars': len(item.raw_text), 'body_method': item.body_method})
    result = {'generated_at': now.isoformat(), 'mode': 'apply' if apply else 'dry_run',
              'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(), 'results': results}
    if apply:
        write_json(output, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.db, args.manifest, args.output, apply=args.apply)), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
