import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path

path = Path(__file__).resolve().parents[2] / "scripts/experiments/snapshot_replay_inputs.py"
spec = importlib.util.spec_from_file_location("compact_replay", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_compact_copy_preserves_input_foreign_keys_active_index_and_schema():
    with closing(sqlite3.connect(":memory:")) as source, closing(sqlite3.connect(":memory:")) as target:
        source.executescript("""
          CREATE TABLE source_registry(id PRIMARY KEY);
          INSERT INTO source_registry VALUES('s');
          CREATE TABLE source_capture_revisions(id PRIMARY KEY,source REFERENCES source_registry(id));
          INSERT INTO source_capture_revisions VALUES('p','s');
          CREATE TABLE semantic_index_state(state_key,active_index_id);
          INSERT INTO semantic_index_state VALUES('default','active');
          CREATE TABLE semantic_indices(index_id PRIMARY KEY);
          INSERT INTO semantic_indices VALUES('active'),('old');
          CREATE TABLE semantic_documents(index_id,document_id);
          INSERT INTO semantic_documents VALUES('active','d'),('old','archive');
          CREATE TABLE issued_ledger(id);
          INSERT INTO issued_ledger VALUES('old-issued');
          CREATE INDEX documents_idx ON semantic_documents(document_id);
          CREATE VIRTUAL TABLE semantic_chunks_fts USING fts5(index_id UNINDEXED,text);
          INSERT INTO semantic_chunks_fts VALUES('active','outage'),('old','archive');
          PRAGMA user_version=42;
        """)
        result = m.copy_inputs(source, target)
        assert result["copied_rows"]["source_registry"] == 1
        assert target.execute("SELECT index_id FROM semantic_indices").fetchall() == [("active",)]
        assert target.execute("SELECT count(*) FROM issued_ledger").fetchone()[0] == 0
        assert (
            target.execute("SELECT text FROM semantic_chunks_fts WHERE semantic_chunks_fts MATCH 'outage'").fetchone()[
                0
            ]
            == "outage"
        )
        assert (
            target.execute(
                "SELECT count(*) FROM semantic_chunks_fts WHERE semantic_chunks_fts MATCH 'archive'"
            ).fetchone()[0]
            == 0
        )
        assert target.execute("PRAGMA user_version").fetchone()[0] == 42
        assert source.execute("SELECT count(*) FROM semantic_indices").fetchone()[0] == 2
        assert result["integrity"] == "ok" and result["paid_calls"] == 0
