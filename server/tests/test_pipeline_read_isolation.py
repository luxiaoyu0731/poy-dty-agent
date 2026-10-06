"""Status reads must not queue behind retrieval or enter writer setup."""
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from app import main, semantic_index, storage
from app.settings import settings


@pytest.mark.parametrize('name', ['pipeline_graph', 'pipeline_node_detail', 'prediction_event_factors'])
def test_observation_reads_do_not_wait_for_heavy_retrieval_lock(monkeypatch, name):
    monkeypatch.setattr(main, 'build_pipeline_graph', lambda *_: {'nodes': []})
    monkeypatch.setattr(main, 'build_pipeline_node_detail', lambda *_: {'node_id': 'collect'})
    from app import prediction_event_factors
    monkeypatch.setattr(prediction_event_factors, 'build_prediction_event_factors', lambda **_: {})
    entered = threading.Event()
    release = threading.Event()

    @main._serialize_heavy_workbench_build
    def heavy():
        entered.set()
        assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        blocked = pool.submit(heavy)
        assert entered.wait(2)
        try:
            read = getattr(main, name)
            args = ('collect', None) if name == 'pipeline_node_detail' else (None,)
            assert isinstance(pool.submit(read, *args).result(timeout=1), dict)
        finally:
            release.set()
        blocked.result(timeout=2)


def test_index_status_uses_readonly_connection_during_writer_reservation(tmp_path, monkeypatch):
    original = settings.sqlite_path
    object.__setattr__(settings, 'sqlite_path', str(tmp_path / 'status.db'))
    try:
        with closing(storage.connect()):
            pass
        def reject_write_connection():
            raise AssertionError('status read entered writer initialization')
        monkeypatch.setattr(semantic_index, 'connect', reject_write_connection)
        with closing(storage.connect()) as writer:
            writer.execute('BEGIN IMMEDIATE')
            try:
                assert semantic_index.semantic_index_status()['status'] == 'missing'
                assert storage.get_grounded_article_summaries([], prompt_version='unused', readonly=True) == {}
            finally:
                writer.rollback()
    finally:
        object.__setattr__(settings, 'sqlite_path', original)
