from types import SimpleNamespace

import pytest

from app import rag, storage
from app.citation import bind_claims_to_evidence
from app.news import EVENT_SUMMARY_PROMPT_VERSION
from app.settings import settings


@pytest.fixture(autouse=True)
def isolated_summary_db(tmp_path):
    previous = settings.sqlite_path
    object.__setattr__(settings, 'sqlite_path', str(tmp_path / 'flow.db'))
    yield
    object.__setattr__(settings, 'sqlite_path', previous)


@pytest.mark.parametrize('include_historical_news', [False, True])
def test_completed_summary_reaches_rag_without_future_leak(tmp_path, monkeypatch, include_historical_news):
    monkeypatch.setattr(storage, '_now', lambda: '2026-09-11T08:00:00+00:00')
    storage.upsert_news_article(article_id='flow', payload=dict(
        source_id='eia_press_room', tier='A', url='https://www.eia.gov/test',
        canonical_url='https://www.eia.gov/test', title='POY price update',
        published_at='2026-09-11T08:00:00+00:00', first_seen_at='2026-09-11T08:00:00+00:00',
        content_hash='source-v1', language='en', raw_text='POY price rose to 9242.50 yuan per tonne.',
        summary='English original', score=80, category='oil_policy', raw={},
    ))
    storage.enqueue_event_ai_summary('flow', 'source-v1', 'test', EVENT_SUMMARY_PROMPT_VERSION)

    def article(at=None):
        return next((d for d in rag._collect_documents(
            as_of_time=at, include_historical_news=include_historical_news,
        ) if d.doc_id == 'news_article:flow'), None)

    assert article() is None  # discovery-only text is not factual RAG evidence
    monkeypatch.setattr(storage, '_now', lambda: '2026-09-12T08:00:00+00:00')
    text = 'POY价格上涨至9242.50元/吨。'
    result = SimpleNamespace(model_dump=lambda **_: dict(
        usable=True, status='completed', factual_summary=text, facts={}, business_impact={},
        input_quality='full_text', fact_summary_status='completed', impact_analysis_status='not_requested',
    ))
    storage.mark_event_ai_grounded_summary_result('flow', result, provider='test', model='test',
        prompt_version=EVENT_SUMMARY_PROMPT_VERSION, source_hash='source-v1', input_chars=100)
    current = article()
    assert current.summary == text
    assert current.observed_at == '2026-09-11T08:00:00+00:00'
    assert current.visible_at == '2026-09-12T08:00:00+00:00'
    assert bind_claims_to_evidence([text], [current])[0].supported
    assert article('2026-09-11T23:59:59+00:00') is None
    storage.enqueue_event_ai_summary('flow', 'source-v2', 'test', EVENT_SUMMARY_PROMPT_VERSION)
    assert article().summary == text  # stale enqueue cannot invalidate the current article
    revised = storage.list_news_articles(limit=1)[0]
    revised.update(content_hash='source-v2', raw_text='POY price revised to 9250 yuan per tonne.')
    storage.upsert_news_article(article_id='flow', payload=revised)
    assert article() is None  # old summary must not support the changed source revision
    storage.enqueue_event_ai_summary('flow', 'source-v2', 'test', EVENT_SUMMARY_PROMPT_VERSION)
    assert article() is None  # discovery-only text is not factual RAG evidence


def test_legacy_completed_ui_counter_is_not_promoted_to_rag(monkeypatch):
    import json
    from contextlib import closing

    storage.upsert_news_article(article_id='counter', payload=dict(
        source_id='us_centcom_press', tier='A', url='https://www.dvidshub.net/video/1011021/test',
        canonical_url='https://www.dvidshub.net/video/1011021/test', title='Flight operations',
        published_at='2026-06-16T08:58:06-04:00', content_hash='counter', language='en',
        raw_text='Public Affairs Subscribe 100 F-35B aircraft take off from the flight deck.',
        summary='Flight operations', score=80, category='oil_policy', raw={},
    ))
    storage.enqueue_event_ai_summary('counter', 'counter', 'test', EVENT_SUMMARY_PROMPT_VERSION)
    with closing(storage.connect()) as c, c:
        c.execute("UPDATE event_ai_summaries SET summary_status='completed',quality_status='completed',"
                  "fact_summary_status='completed',factual_summary=?,fact_payload=?,generated_at=? WHERE article_id=?",
                  ('F-35B飞机数量为100架', json.dumps({'numbers': [{'value': '100', 'context': '飞机数量',
                   'evidence_quote': '100 F-35B aircraft take off'}]}), '2026-09-14T02:00:00+00:00', 'counter'))
    summaries = storage.get_grounded_article_summaries(['counter'], prompt_version=EVENT_SUMMARY_PROMPT_VERSION)
    assert 'counter' not in summaries
    assert not any(d.doc_id == 'news_article:counter' for d in rag._collect_documents())

    from app.workbench_events import build_event_library_workbench
    events = build_event_library_workbench(q="Flight operations")["events"]
    assert events
    assert all("100架" not in event["factual_summary"] for event in events)


def test_index_includes_recovered_history_beyond_online_limit(monkeypatch):
    from contextlib import closing

    from app import semantic_index
    from app.semantic_embedding import EmbeddingConfig, EmbeddingService

    monkeypatch.setitem(rag.RAG_SOURCE_POLICY, 'news_articles', {'index_limit': 1})
    for aid, day in [('old', '10'), ('title-only', '11'), ('new', '12')]:
        instant = f'2026-09-{day}T08:00:00+00:00'
        monkeypatch.setattr(storage, '_now', lambda instant=instant: instant)
        storage.upsert_news_article(article_id=aid, payload=dict(
            source_id='eia_press_room', tier='A', url=f'https://www.eia.gov/{aid}',
            canonical_url=f'https://www.eia.gov/{aid}', title=f'POY price update {aid}',
            published_at=instant, first_seen_at=instant, content_hash=aid, language='en',
            raw_text='POY price rose to 9242.50 yuan per tonne.', summary='',
            score=80, category='oil_policy', raw={},
        ))
        if aid != 'title-only':
            storage.enqueue_event_ai_summary(aid, aid, 'test', EVENT_SUMMARY_PROMPT_VERSION)
            monkeypatch.setattr(storage, '_now', lambda: '2026-09-13T08:00:00+00:00')
            result = SimpleNamespace(model_dump=lambda **_: dict(
                usable=True, status='completed', factual_summary='POY价格上涨至9242.50元/吨。',
                facts={}, business_impact={}, input_quality='full_text',
                fact_summary_status='completed', impact_analysis_status='not_requested',
            ))
            storage.mark_event_ai_grounded_summary_result(
                aid, result, provider='test', model='test', prompt_version=EVENT_SUMMARY_PROMPT_VERSION,
                source_hash=aid, input_chars=100,
            )

    def article_ids(**kwargs):
        return {d.doc_id for d in rag._collect_documents(**kwargs) if d.doc_type == 'news_article'}

    assert article_ids() == {'news_article:new'}  # online fallback remains bounded
    assert article_ids(include_historical_news=True) == {'news_article:old', 'news_article:new'}
    assert not article_ids(include_historical_news=True, as_of_time='2026-09-12T23:59:59+00:00')

    class OfflineEmbedding:
        def embed(self, texts, *, kind):
            return [[1.0, 0.0, 0.0] for _ in texts]

    config = EmbeddingConfig(
        provider='test', model='test', model_version='1', dimensions=3, normalization=True,
        batch_size=32, device='cpu', timeout_seconds=1.0, fallback_policy='error',
        query_prefix='', document_prefix='',
    )
    builder = semantic_index.SemanticIndexBuilder(EmbeddingService(config, OfflineEmbedding()))
    monkeypatch.setattr(semantic_index, 'SemanticIndexBuilder', lambda: builder)
    built = semantic_index.rebuild_semantic_index()
    assert built['status'] == 'ready'
    with closing(storage.connect()) as connection:
        indexed = connection.execute(
            "SELECT document_id,visible_at FROM semantic_documents WHERE index_id=? AND source_kind='news_article'",
            (built['index_id'],),
        ).fetchall()
    assert {row['document_id'] for row in indexed} == {'news_article:old', 'news_article:new'}
    assert {row['visible_at'] for row in indexed} == {'2026-09-13T08:00:00+00:00'}
