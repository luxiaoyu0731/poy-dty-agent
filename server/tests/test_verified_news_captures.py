from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app import news, storage
from app.settings import settings
from scripts import apply_verified_news_captures as repair
from scripts import backfill_news_article_bodies as recovery

BODY = '港口通道因风暴停航，港务局发布恢复作业安排，原油运输受到影响。' * 40
URL = 'https://www.cnbc.com/2026/10/04/shipping-disruption.html'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = replace(settings, sqlite_path=str(tmp_path / 'captures.db'))
    for module in (news, storage, repair, recovery):
        monkeypatch.setattr(module, 'settings', config)
    monkeypatch.setattr(news, '_deepseek_client', lambda: type('Client', (), {'model': 'test'})())
    monkeypatch.setattr(news, '_should_fetch_detail', lambda *args, **kwargs: True)
    article_id = news._id('art', URL)
    storage.upsert_news_article(article_id=article_id, payload=dict(
        source_id='google_news_oil_rss', tier='C', url=URL, canonical_url=URL,
        title='Shipping disruption', content_hash='old', raw_text='Old discovery snippet',
        first_seen_at=datetime.now(UTC).isoformat(), published_at='2026-10-04',
    ))
    html = f'<title>Shipping disruption</title><div class="ArticleBody-articleBody"><p>{BODY}</p></div>'
    (tmp_path / 'capture.html').write_text(html)
    capture = {'article_id': article_id, 'expected_content_hash': 'old', 'url': URL,
               'html_file': 'capture.html', 'html_sha256': hashlib.sha256(html.encode()).hexdigest()}
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'schema': 'verified-news-captures.v1', 'captures': [capture]}))
    return tmp_path, config, article_id, manifest, capture


def test_dry_run_validates_without_any_database_or_sidecar_writes(setup):
    root, config, aid, manifest, _ = setup
    with closing(storage.connect()) as con:
        before = dict(con.execute('SELECT * FROM news_articles WHERE article_id=?', (aid,)).fetchone())
    result = asyncio.run(repair.run(root / 'captures.db', manifest, root / 'report.json'))
    assert result['results'][0]['status'] == 'validated'
    assert not (root / 'report.json').exists() and not (root / 'body-recovery-journal').exists()
    with closing(storage.connect()) as con:
        assert dict(con.execute('SELECT * FROM news_articles WHERE article_id=?', (aid,)).fetchone()) == before


def test_apply_journals_and_preserves_identity_time_without_resetting_retries(setup):
    root, config, aid, manifest, _ = setup
    with closing(storage.connect()) as con:
        before = dict(con.execute('SELECT * FROM news_articles WHERE article_id=?', (aid,)).fetchone())
    state = root / 'body-acquisition-state.json'
    state.write_text(json.dumps({aid: {'attempts': 3, 'content_hash': 'old'}}))
    before_state = state.read_bytes()
    result = asyncio.run(repair.run(root / 'captures.db', manifest, root / 'report.json', apply=True))
    assert result['results'][0]['status'] == 'applied'
    assert state.read_bytes() == before_state
    journal = json.loads(next((root / 'body-recovery-journal').glob('*.json')).read_text())
    assert journal['article']['raw_text'] == 'Old discovery snippet'
    with closing(storage.connect()) as con:
        row = dict(con.execute('SELECT * FROM news_articles WHERE article_id=?', (aid,)).fetchone())
        assert con.execute('SELECT COUNT(*) FROM news_articles').fetchone()[0] == 1
        assert row['raw_text'] == BODY and row['first_seen_at'] == before['first_seen_at']
        assert row['content_hash'] != 'old'
        assert json.loads(row['raw'])['source_content']['status'] == 'full_text'
    with pytest.raises(ValueError, match='revision_mismatch'):
        asyncio.run(repair.run(root / 'captures.db', manifest, root / 'report.json', apply=True))


@pytest.mark.parametrize('defect', ['hash', 'permission', 'revision', 'duplicate', 'access', 'identity'])
def test_invalid_manifest_fails_before_any_write(setup, monkeypatch, defect):
    root, config, aid, manifest, capture = setup
    payload = json.loads(manifest.read_text())
    if defect == 'hash':
        payload['captures'][0]['html_sha256'] = 'wrong'
    elif defect == 'permission':
        monkeypatch.setattr(news, '_should_fetch_detail', lambda *args, **kwargs: False)
    elif defect == 'revision':
        payload['captures'][0]['expected_content_hash'] = 'wrong'
    elif defect == 'duplicate':
        payload['captures'].append(capture)
    elif defect == 'identity':
        monkeypatch.setattr(repair, 'item_for', lambda row: news.RawNewsItem('google_news_oil_rss', 'C',
                            URL, 'Shipping disruption', discovery_url='https://news.google.com/other'))
    else:
        html = '<div class="ArticleBody-articleBody">Subscribe to read</div>'
        (root / 'capture.html').write_text(html)
        payload['captures'][0]['html_sha256'] = hashlib.sha256(html.encode()).hexdigest()
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        asyncio.run(repair.run(root / 'captures.db', manifest, root / 'report.json', apply=True))
    assert not (root / 'report.json').exists() and not (root / 'body-recovery-journal').exists()
    with closing(storage.connect()) as con:
        assert con.execute('SELECT content_hash FROM news_articles WHERE article_id=?', (aid,)).fetchone()[0] == 'old'
