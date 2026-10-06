import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "scripts/experiments/preflight_semantic_visibility.py"
spec = importlib.util.spec_from_file_location("semantic_visibility_preflight", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_current_index_does_not_become_historically_available():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript("""
        CREATE TABLE semantic_index_state(state_key,active_index_id);
        INSERT INTO semantic_index_state VALUES('default','idx');
        CREATE TABLE semantic_indices(index_id,created_at,index_version,status);
        INSERT INTO semantic_indices VALUES('idx','2026-10-02T06:00:00Z','test','ready');
        CREATE TABLE semantic_documents(index_id,source_kind,visible_at);
        INSERT INTO semantic_documents VALUES('idx','news_article','2026-10-01T05:00:00Z');
        INSERT INTO semantic_documents VALUES('idx','news_article','2026-10-04T05:00:00Z');
        INSERT INTO semantic_documents VALUES('idx','knowledge_node','');
        """)
        result = module.inspect_visibility(
            connection, ["2025-01-01T08:00:00+08:00", "2026-10-02T08:00:00+08:00", "2026-10-03T08:00:00+08:00"]
        )
        old, pre_index, after = result["samples"]
        assert old["visible_documents"] == 0
        assert old["blocked_reasons"] == ["no_visible_documents", "index_created_after_cutoff"]
        assert pre_index["visible_documents"] == 1 and not pre_index["vector_index_available_at_cutoff"]
        assert after["visible_documents"] == 1 and after["vector_index_available_at_cutoff"]
        assert result["paid_calls"] == 0
        with pytest.raises(ValueError, match="aware_cutoff_required"):
            module.inspect_visibility(connection, ["2025-01-01"])
