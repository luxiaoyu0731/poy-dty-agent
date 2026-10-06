from __future__ import annotations

# ruff: noqa: E501 -- frozen canonical schema-manifest JSON is intentionally byte-exact.
import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

GOVERNANCE_TABLES = {
    "data_source_governance",
    "authoritative_reconciliations",
    "data_governance_audit_runs",
    "data_governance_dataset_results",
    "data_governance_source_results",
    "data_governance_quarantine_records",
    "data_governance_quarantine_correction_links",
    "schema_migrations",
}

HASH_ALGORITHM_VERSION = "sha256:dg-cjson-v1"
VERIFIED_RECOVERY_CONTRACT_VERSION = "dg-timestamp-recovery-v1"
SCHEMA_MANIFEST_ALGORITHM = "sha256"
SCHEMA_MANIFEST_VERSION = "dg-sqlite-schema-manifest-v1"
EXPECTED_V24_APPLICATION_ID = 0
EXPECTED_V25_APPLICATION_ID = 0
EXPECTED_SCHEMA_ENCODING = "UTF-8"
EXPECTED_V24_SCHEMA_MANIFEST: dict[str, object] = json.loads(
    r"""{"algorithm":"sha256","database_pragmas":{"application_id":0,"encoding":"UTF-8","user_version":24},"manifest_version":"dg-sqlite-schema-manifest-v1","objects":[{"foreign_keys":[{"from":"correction_quarantine_id","id":0,"match":"NONE","on_delete":"NO ACTION","on_update":"NO ACTION","seq":0,"table":"data_governance_quarantine_records","to":"quarantine_id"},{"from":"original_quarantine_id","id":1,"match":"NONE","on_delete":"NO ACTION","on_update":"NO ACTION","seq":0,"table":"data_governance_quarantine_records","to":"quarantine_id"}],"name":"data_governance_quarantine_correction_links","sql":"CREATE TABLE data_governance_quarantine_correction_links ( link_id TEXT PRIMARY KEY CHECK (length(trim(link_id)) > 0), created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0), original_quarantine_id TEXT NOT NULL REFERENCES data_governance_quarantine_records(quarantine_id), correction_quarantine_id TEXT NOT NULL REFERENCES data_governance_quarantine_records(quarantine_id), CHECK (original_quarantine_id <> correction_quarantine_id), UNIQUE (original_quarantine_id, correction_quarantine_id) )","table_info":[{"cid":0,"dflt_value":null,"name":"link_id","notnull":false,"pk":1,"type":"TEXT"},{"cid":1,"dflt_value":null,"name":"created_at","notnull":true,"pk":0,"type":"TEXT"},{"cid":2,"dflt_value":null,"name":"original_quarantine_id","notnull":true,"pk":0,"type":"TEXT"},{"cid":3,"dflt_value":null,"name":"correction_quarantine_id","notnull":true,"pk":0,"type":"TEXT"}],"tbl_name":"data_governance_quarantine_correction_links","type":"table"},{"foreign_keys":[{"from":"recovered_observation_id","id":0,"match":"NONE","on_delete":"NO ACTION","on_update":"NO ACTION","seq":0,"table":"market_observations","to":"observation_id"}],"name":"data_governance_quarantine_records","sql":"CREATE TABLE data_governance_quarantine_records ( quarantine_id TEXT PRIMARY KEY CHECK (length(trim(quarantine_id)) > 0), created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0), received_at TEXT NOT NULL CHECK (length(trim(received_at)) > 0), updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0), source_id TEXT NOT NULL CHECK (length(trim(source_id)) > 0), payload_hash TEXT NOT NULL CHECK ( length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*' ), hash_algorithm_version TEXT NOT NULL DEFAULT 'sha256:dg-cjson-v1' CHECK (hash_algorithm_version = 'sha256:dg-cjson-v1'), failure_code TEXT NOT NULL DEFAULT 'timestamp_invalid' CHECK (failure_code = 'timestamp_invalid'), failure_detail TEXT NOT NULL DEFAULT '' CHECK (length(failure_detail) <= 512), raw_payload TEXT NOT NULL CHECK (length(raw_payload) > 0 AND json_valid(raw_payload)), status TEXT NOT NULL DEFAULT 'quarantined' CHECK (status IN ('quarantined', 'recovered')), recovered_at TEXT DEFAULT NULL, recovery_payload_hash TEXT DEFAULT NULL, recovery_hash_algorithm_version TEXT DEFAULT NULL, recovered_observation_id TEXT DEFAULT NULL REFERENCES market_observations(observation_id), recovery_detail TEXT NOT NULL DEFAULT '' CHECK (length(recovery_detail) <= 512), UNIQUE (source_id, payload_hash, failure_code), CHECK ( recovery_payload_hash IS NULL OR ( length(recovery_payload_hash) = 64 AND recovery_payload_hash NOT GLOB '*[^0-9a-f]*' ) ), CHECK ( ( status = 'quarantined' AND recovered_at IS NULL AND recovery_payload_hash IS NULL AND recovery_hash_algorithm_version IS NULL AND recovered_observation_id IS NULL ) OR ( status = 'recovered' AND recovered_at IS NOT NULL AND recovery_payload_hash IS NOT NULL AND recovery_hash_algorithm_version = 'sha256:dg-cjson-v1' AND recovered_observation_id IS NOT NULL AND recovery_payload_hash <> payload_hash ) ) )","table_info":[{"cid":0,"dflt_value":null,"name":"quarantine_id","notnull":false,"pk":1,"type":"TEXT"},{"cid":1,"dflt_value":null,"name":"created_at","notnull":true,"pk":0,"type":"TEXT"},{"cid":2,"dflt_value":null,"name":"received_at","notnull":true,"pk":0,"type":"TEXT"},{"cid":3,"dflt_value":null,"name":"updated_at","notnull":true,"pk":0,"type":"TEXT"},{"cid":4,"dflt_value":null,"name":"source_id","notnull":true,"pk":0,"type":"TEXT"},{"cid":5,"dflt_value":null,"name":"payload_hash","notnull":true,"pk":0,"type":"TEXT"},{"cid":6,"dflt_value":"'sha256:dg-cjson-v1'","name":"hash_algorithm_version","notnull":true,"pk":0,"type":"TEXT"},{"cid":7,"dflt_value":"'timestamp_invalid'","name":"failure_code","notnull":true,"pk":0,"type":"TEXT"},{"cid":8,"dflt_value":"''","name":"failure_detail","notnull":true,"pk":0,"type":"TEXT"},{"cid":9,"dflt_value":null,"name":"raw_payload","notnull":true,"pk":0,"type":"TEXT"},{"cid":10,"dflt_value":"'quarantined'","name":"status","notnull":true,"pk":0,"type":"TEXT"},{"cid":11,"dflt_value":"NULL","name":"recovered_at","notnull":false,"pk":0,"type":"TEXT"},{"cid":12,"dflt_value":"NULL","name":"recovery_payload_hash","notnull":false,"pk":0,"type":"TEXT"},{"cid":13,"dflt_value":"NULL","name":"recovery_hash_algorithm_version","notnull":false,"pk":0,"type":"TEXT"},{"cid":14,"dflt_value":"NULL","name":"recovered_observation_id","notnull":false,"pk":0,"type":"TEXT"},{"cid":15,"dflt_value":"''","name":"recovery_detail","notnull":true,"pk":0,"type":"TEXT"}],"tbl_name":"data_governance_quarantine_records","type":"table"},{"foreign_keys":[],"name":"market_observations","sql":"CREATE TABLE market_observations ( observation_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source_id TEXT NOT NULL, observed_at TEXT NOT NULL, indicator TEXT NOT NULL, product TEXT NOT NULL, value REAL, unit TEXT NOT NULL, frequency TEXT NOT NULL, region TEXT NOT NULL, evidence_url TEXT NOT NULL, notes TEXT NOT NULL, raw TEXT NOT NULL )","table_info":[{"cid":0,"dflt_value":null,"name":"observation_id","notnull":false,"pk":1,"type":"TEXT"},{"cid":1,"dflt_value":null,"name":"created_at","notnull":true,"pk":0,"type":"TEXT"},{"cid":2,"dflt_value":null,"name":"source_id","notnull":true,"pk":0,"type":"TEXT"},{"cid":3,"dflt_value":null,"name":"observed_at","notnull":true,"pk":0,"type":"TEXT"},{"cid":4,"dflt_value":null,"name":"indicator","notnull":true,"pk":0,"type":"TEXT"},{"cid":5,"dflt_value":null,"name":"product","notnull":true,"pk":0,"type":"TEXT"},{"cid":6,"dflt_value":null,"name":"value","notnull":false,"pk":0,"type":"REAL"},{"cid":7,"dflt_value":null,"name":"unit","notnull":true,"pk":0,"type":"TEXT"},{"cid":8,"dflt_value":null,"name":"frequency","notnull":true,"pk":0,"type":"TEXT"},{"cid":9,"dflt_value":null,"name":"region","notnull":true,"pk":0,"type":"TEXT"},{"cid":10,"dflt_value":null,"name":"evidence_url","notnull":true,"pk":0,"type":"TEXT"},{"cid":11,"dflt_value":null,"name":"notes","notnull":true,"pk":0,"type":"TEXT"},{"cid":12,"dflt_value":null,"name":"raw","notnull":true,"pk":0,"type":"TEXT"}],"tbl_name":"market_observations","type":"table"},{"index_list":{"origin":"pk","partial":false,"unique":true},"index_xinfo":[{"cid":0,"coll":"BINARY","desc":false,"key":true,"name":"link_id","seqno":0},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":1}],"name":"auto/data_governance_quarantine_correction_links/pk/469b85d35882dadd2b14ebe58309fd5010c17045523092b0f041a1a62d167759","sql":null,"tbl_name":"data_governance_quarantine_correction_links","type":"index"},{"index_list":{"origin":"u","partial":false,"unique":true},"index_xinfo":[{"cid":2,"coll":"BINARY","desc":false,"key":true,"name":"original_quarantine_id","seqno":0},{"cid":3,"coll":"BINARY","desc":false,"key":true,"name":"correction_quarantine_id","seqno":1},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":2}],"name":"auto/data_governance_quarantine_correction_links/u/36f813de71f780fae4aceba4cc7e4a723ec3176e2bd5c21aeab391071f79656f","sql":null,"tbl_name":"data_governance_quarantine_correction_links","type":"index"},{"index_list":{"origin":"pk","partial":false,"unique":true},"index_xinfo":[{"cid":0,"coll":"BINARY","desc":false,"key":true,"name":"quarantine_id","seqno":0},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":1}],"name":"auto/data_governance_quarantine_records/pk/af9cd7aabaa16e690971bf00befa7ee732c8e985bf33286ce0642b08d5fbf89a","sql":null,"tbl_name":"data_governance_quarantine_records","type":"index"},{"index_list":{"origin":"u","partial":false,"unique":true},"index_xinfo":[{"cid":4,"coll":"BINARY","desc":false,"key":true,"name":"source_id","seqno":0},{"cid":5,"coll":"BINARY","desc":false,"key":true,"name":"payload_hash","seqno":1},{"cid":7,"coll":"BINARY","desc":false,"key":true,"name":"failure_code","seqno":2},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":3}],"name":"auto/data_governance_quarantine_records/u/3b8d3fd7a7eb8433235fc8ae7965da7d3c7e19a4029cf817d15d285b006f6304","sql":null,"tbl_name":"data_governance_quarantine_records","type":"index"},{"index_list":{"origin":"pk","partial":false,"unique":true},"index_xinfo":[{"cid":0,"coll":"BINARY","desc":false,"key":true,"name":"observation_id","seqno":0},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":1}],"name":"auto/market_observations/pk/44bfc8141495b314daf25327d875c29376af17f29abf8f3665af5c22f51f91de","sql":null,"tbl_name":"market_observations","type":"index"},{"index_list":{"origin":"c","partial":false,"unique":false},"index_xinfo":[{"cid":3,"coll":"BINARY","desc":false,"key":true,"name":"correction_quarantine_id","seqno":0},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":1}],"name":"idx_dg_quarantine_correction_reverse","sql":"CREATE INDEX idx_dg_quarantine_correction_reverse ON data_governance_quarantine_correction_links(correction_quarantine_id)","tbl_name":"data_governance_quarantine_correction_links","type":"index"},{"index_list":{"origin":"c","partial":true,"unique":false},"index_xinfo":[{"cid":14,"coll":"BINARY","desc":false,"key":true,"name":"recovered_observation_id","seqno":0},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":1}],"name":"idx_dg_quarantine_recovered_observation","sql":"CREATE INDEX idx_dg_quarantine_recovered_observation ON data_governance_quarantine_records(recovered_observation_id) WHERE recovered_observation_id IS NOT NULL","tbl_name":"data_governance_quarantine_records","type":"index"},{"index_list":{"origin":"c","partial":false,"unique":false},"index_xinfo":[{"cid":4,"coll":"BINARY","desc":false,"key":true,"name":"source_id","seqno":0},{"cid":2,"coll":"BINARY","desc":true,"key":true,"name":"received_at","seqno":1},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":2}],"name":"idx_dg_quarantine_source_received","sql":"CREATE INDEX idx_dg_quarantine_source_received ON data_governance_quarantine_records(source_id, received_at DESC)","tbl_name":"data_governance_quarantine_records","type":"index"},{"index_list":{"origin":"c","partial":false,"unique":false},"index_xinfo":[{"cid":10,"coll":"BINARY","desc":false,"key":true,"name":"status","seqno":0},{"cid":7,"coll":"BINARY","desc":false,"key":true,"name":"failure_code","seqno":1},{"cid":2,"coll":"BINARY","desc":true,"key":true,"name":"received_at","seqno":2},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":3}],"name":"idx_dg_quarantine_status_failure_received","sql":"CREATE INDEX idx_dg_quarantine_status_failure_received ON data_governance_quarantine_records(status, failure_code, received_at DESC)","tbl_name":"data_governance_quarantine_records","type":"index"},{"index_list":{"origin":"c","partial":false,"unique":false},"index_xinfo":[{"cid":2,"coll":"BINARY","desc":false,"key":true,"name":"source_id","seqno":0},{"cid":3,"coll":"BINARY","desc":false,"key":true,"name":"observed_at","seqno":1},{"cid":4,"coll":"BINARY","desc":false,"key":true,"name":"indicator","seqno":2},{"cid":5,"coll":"BINARY","desc":false,"key":true,"name":"product","seqno":3},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":4}],"name":"idx_market_observation_identity","sql":"CREATE INDEX idx_market_observation_identity ON market_observations(source_id, observed_at, indicator, product)","tbl_name":"market_observations","type":"index"},{"name":"trg_dg_quarantine_correction_link_exists","sql":"CREATE TRIGGER trg_dg_quarantine_correction_link_exists BEFORE INSERT ON data_governance_quarantine_correction_links WHEN NOT EXISTS ( SELECT 1 FROM data_governance_quarantine_records WHERE quarantine_id = NEW.original_quarantine_id ) OR NOT EXISTS ( SELECT 1 FROM data_governance_quarantine_records WHERE quarantine_id = NEW.correction_quarantine_id ) BEGIN SELECT RAISE(ABORT, 'correction_quarantine_not_found'); END","tbl_name":"data_governance_quarantine_correction_links","type":"trigger"},{"name":"trg_dg_quarantine_correction_link_no_delete","sql":"CREATE TRIGGER trg_dg_quarantine_correction_link_no_delete BEFORE DELETE ON data_governance_quarantine_correction_links BEGIN SELECT RAISE(ABORT, 'correction_link_immutable'); END","tbl_name":"data_governance_quarantine_correction_links","type":"trigger"},{"name":"trg_dg_quarantine_correction_link_no_update","sql":"CREATE TRIGGER trg_dg_quarantine_correction_link_no_update BEFORE UPDATE ON data_governance_quarantine_correction_links BEGIN SELECT RAISE(ABORT, 'correction_link_immutable'); END","tbl_name":"data_governance_quarantine_correction_links","type":"trigger"},{"name":"trg_dg_quarantine_immutable_fact","sql":"CREATE TRIGGER trg_dg_quarantine_immutable_fact BEFORE UPDATE OF quarantine_id, created_at, received_at, source_id, payload_hash, hash_algorithm_version, failure_code, raw_payload ON data_governance_quarantine_records BEGIN SELECT RAISE(ABORT, 'quarantine_fact_immutable'); END","tbl_name":"data_governance_quarantine_records","type":"trigger"},{"name":"trg_dg_quarantine_no_delete","sql":"CREATE TRIGGER trg_dg_quarantine_no_delete BEFORE DELETE ON data_governance_quarantine_records BEGIN SELECT RAISE(ABORT, 'quarantine_fact_immutable'); END","tbl_name":"data_governance_quarantine_records","type":"trigger"},{"name":"trg_dg_quarantine_recovery_observation_exists","sql":"CREATE TRIGGER trg_dg_quarantine_recovery_observation_exists BEFORE UPDATE OF status, recovered_observation_id ON data_governance_quarantine_records WHEN NEW.status = 'recovered' AND NOT EXISTS ( SELECT 1 FROM market_observations WHERE observation_id = NEW.recovered_observation_id ) BEGIN SELECT RAISE(ABORT, 'recovery_observation_not_found'); END","tbl_name":"data_governance_quarantine_records","type":"trigger"},{"name":"trg_dg_quarantine_recovery_only","sql":"CREATE TRIGGER trg_dg_quarantine_recovery_only BEFORE UPDATE ON data_governance_quarantine_records WHEN NOT ( OLD.status = 'quarantined' AND NEW.status = 'recovered' AND NEW.updated_at = NEW.recovered_at ) BEGIN SELECT RAISE(ABORT, 'invalid_quarantine_state_transition'); END","tbl_name":"data_governance_quarantine_records","type":"trigger"}],"runtime_pragmas":{"defer_foreign_keys":false,"foreign_keys":true,"legacy_alter_table":false}}"""
)
EXPECTED_V25_SCHEMA_MANIFEST: dict[str, object] = json.loads(
    r"""{"algorithm":"sha256","database_pragmas":{"application_id":0,"encoding":"UTF-8","user_version":25},"manifest_version":"dg-sqlite-schema-manifest-v1","objects":[{"foreign_keys":[{"from":"correction_quarantine_id","id":0,"match":"NONE","on_delete":"RESTRICT","on_update":"RESTRICT","seq":0,"table":"data_governance_quarantine_records","to":"quarantine_id"},{"from":"original_quarantine_id","id":1,"match":"NONE","on_delete":"RESTRICT","on_update":"RESTRICT","seq":0,"table":"data_governance_quarantine_records","to":"quarantine_id"}],"name":"data_governance_quarantine_correction_links","sql":"CREATE TABLE data_governance_quarantine_correction_links ( link_id TEXT PRIMARY KEY CHECK (length(trim(link_id)) > 0), created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0), original_quarantine_id TEXT NOT NULL REFERENCES data_governance_quarantine_records(quarantine_id) ON UPDATE RESTRICT ON DELETE RESTRICT, correction_quarantine_id TEXT NOT NULL REFERENCES data_governance_quarantine_records(quarantine_id) ON UPDATE RESTRICT ON DELETE RESTRICT, CHECK (original_quarantine_id <> correction_quarantine_id), UNIQUE (original_quarantine_id, correction_quarantine_id) )","table_info":[{"cid":0,"dflt_value":null,"name":"link_id","notnull":false,"pk":1,"type":"TEXT"},{"cid":1,"dflt_value":null,"name":"created_at","notnull":true,"pk":0,"type":"TEXT"},{"cid":2,"dflt_value":null,"name":"original_quarantine_id","notnull":true,"pk":0,"type":"TEXT"},{"cid":3,"dflt_value":null,"name":"correction_quarantine_id","notnull":true,"pk":0,"type":"TEXT"}],"tbl_name":"data_governance_quarantine_correction_links","type":"table"},{"foreign_keys":[{"from":"recovered_observation_id","id":0,"match":"NONE","on_delete":"RESTRICT","on_update":"RESTRICT","seq":0,"table":"market_observations","to":"observation_id"}],"name":"data_governance_quarantine_records","sql":"CREATE TABLE data_governance_quarantine_records ( quarantine_id TEXT PRIMARY KEY CHECK (length(trim(quarantine_id)) > 0), created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0), received_at TEXT NOT NULL CHECK (length(trim(received_at)) > 0), updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0), source_id TEXT NOT NULL CHECK (length(trim(source_id)) > 0), payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'), hash_algorithm_version TEXT NOT NULL DEFAULT 'sha256:dg-cjson-v1' CHECK (hash_algorithm_version = 'sha256:dg-cjson-v1'), failure_code TEXT NOT NULL DEFAULT 'timestamp_invalid' CHECK (failure_code = 'timestamp_invalid'), failure_detail TEXT NOT NULL DEFAULT '' CHECK (length(failure_detail) <= 512), raw_payload TEXT NOT NULL CHECK (length(raw_payload) > 0 AND json_valid(raw_payload)), status TEXT NOT NULL DEFAULT 'quarantined' CHECK (status IN ('quarantined','recovered')), recovered_at TEXT DEFAULT NULL, recovery_payload_hash TEXT DEFAULT NULL CHECK ( recovery_payload_hash IS NULL OR (length(recovery_payload_hash) = 64 AND recovery_payload_hash NOT GLOB '*[^0-9a-f]*') ), recovery_hash_algorithm_version TEXT DEFAULT NULL CHECK ( recovery_hash_algorithm_version IS NULL OR recovery_hash_algorithm_version = 'sha256:dg-cjson-v1' ), recovered_observation_id TEXT DEFAULT NULL REFERENCES market_observations(observation_id) ON UPDATE RESTRICT ON DELETE RESTRICT, recovery_detail TEXT NOT NULL DEFAULT '' CHECK (length(recovery_detail) <= 512), verified_recovery_projection_hash TEXT DEFAULT NULL CHECK ( verified_recovery_projection_hash IS NULL OR (length(verified_recovery_projection_hash) = 64 AND verified_recovery_projection_hash NOT GLOB '*[^0-9a-f]*') ), verified_recovery_hash_algorithm_version TEXT DEFAULT NULL CHECK ( verified_recovery_hash_algorithm_version IS NULL OR verified_recovery_hash_algorithm_version = 'sha256:dg-cjson-v1' ), verified_recovery_contract_version TEXT DEFAULT NULL CHECK ( verified_recovery_contract_version IS NULL OR verified_recovery_contract_version = 'dg-timestamp-recovery-v1' ), verified_original_projection_hash TEXT DEFAULT NULL CHECK ( verified_original_projection_hash IS NULL OR (length(verified_original_projection_hash) = 64 AND verified_original_projection_hash NOT GLOB '*[^0-9a-f]*') ), recovery_raw_before_hash TEXT DEFAULT NULL CHECK ( recovery_raw_before_hash IS NULL OR (length(recovery_raw_before_hash) = 64 AND recovery_raw_before_hash NOT GLOB '*[^0-9a-f]*') ), recovery_raw_after_hash TEXT DEFAULT NULL CHECK ( recovery_raw_after_hash IS NULL OR (length(recovery_raw_after_hash) = 64 AND recovery_raw_after_hash NOT GLOB '*[^0-9a-f]*') ), recovery_observed_at_before TEXT DEFAULT NULL, recovery_observed_at_after TEXT DEFAULT NULL, recovery_raw_time_changes TEXT DEFAULT NULL CHECK ( recovery_raw_time_changes IS NULL OR (json_valid(recovery_raw_time_changes) AND json_type(recovery_raw_time_changes) = 'array') ), UNIQUE (source_id, payload_hash, failure_code), CHECK ( ( status = 'quarantined' AND recovered_at IS NULL AND recovery_payload_hash IS NULL AND recovery_hash_algorithm_version IS NULL AND recovered_observation_id IS NULL AND verified_recovery_projection_hash IS NULL AND verified_recovery_hash_algorithm_version IS NULL AND verified_recovery_contract_version IS NULL AND verified_original_projection_hash IS NULL AND recovery_raw_before_hash IS NULL AND recovery_raw_after_hash IS NULL AND recovery_observed_at_before IS NULL AND recovery_observed_at_after IS NULL AND recovery_raw_time_changes IS NULL ) OR ( status = 'recovered' AND recovered_at IS NOT NULL AND updated_at = recovered_at AND recovery_payload_hash IS NOT NULL AND recovery_hash_algorithm_version = 'sha256:dg-cjson-v1' AND recovered_observation_id IS NOT NULL AND verified_recovery_projection_hash IS NOT NULL AND verified_recovery_hash_algorithm_version = 'sha256:dg-cjson-v1' AND verified_recovery_contract_version = 'dg-timestamp-recovery-v1' AND verified_original_projection_hash IS NOT NULL AND recovery_raw_before_hash IS NOT NULL AND recovery_raw_after_hash IS NOT NULL AND recovery_observed_at_before IS NOT NULL AND recovery_observed_at_after IS NOT NULL AND recovery_observed_at_before <> recovery_observed_at_after AND recovery_raw_time_changes IS NOT NULL ) ) )","table_info":[{"cid":0,"dflt_value":null,"name":"quarantine_id","notnull":false,"pk":1,"type":"TEXT"},{"cid":1,"dflt_value":null,"name":"created_at","notnull":true,"pk":0,"type":"TEXT"},{"cid":2,"dflt_value":null,"name":"received_at","notnull":true,"pk":0,"type":"TEXT"},{"cid":3,"dflt_value":null,"name":"updated_at","notnull":true,"pk":0,"type":"TEXT"},{"cid":4,"dflt_value":null,"name":"source_id","notnull":true,"pk":0,"type":"TEXT"},{"cid":5,"dflt_value":null,"name":"payload_hash","notnull":true,"pk":0,"type":"TEXT"},{"cid":6,"dflt_value":"'sha256:dg-cjson-v1'","name":"hash_algorithm_version","notnull":true,"pk":0,"type":"TEXT"},{"cid":7,"dflt_value":"'timestamp_invalid'","name":"failure_code","notnull":true,"pk":0,"type":"TEXT"},{"cid":8,"dflt_value":"''","name":"failure_detail","notnull":true,"pk":0,"type":"TEXT"},{"cid":9,"dflt_value":null,"name":"raw_payload","notnull":true,"pk":0,"type":"TEXT"},{"cid":10,"dflt_value":"'quarantined'","name":"status","notnull":true,"pk":0,"type":"TEXT"},{"cid":11,"dflt_value":"NULL","name":"recovered_at","notnull":false,"pk":0,"type":"TEXT"},{"cid":12,"dflt_value":"NULL","name":"recovery_payload_hash","notnull":false,"pk":0,"type":"TEXT"},{"cid":13,"dflt_value":"NULL","name":"recovery_hash_algorithm_version","notnull":false,"pk":0,"type":"TEXT"},{"cid":14,"dflt_value":"NULL","name":"recovered_observation_id","notnull":false,"pk":0,"type":"TEXT"},{"cid":15,"dflt_value":"''","name":"recovery_detail","notnull":true,"pk":0,"type":"TEXT"},{"cid":16,"dflt_value":"NULL","name":"verified_recovery_projection_hash","notnull":false,"pk":0,"type":"TEXT"},{"cid":17,"dflt_value":"NULL","name":"verified_recovery_hash_algorithm_version","notnull":false,"pk":0,"type":"TEXT"},{"cid":18,"dflt_value":"NULL","name":"verified_recovery_contract_version","notnull":false,"pk":0,"type":"TEXT"},{"cid":19,"dflt_value":"NULL","name":"verified_original_projection_hash","notnull":false,"pk":0,"type":"TEXT"},{"cid":20,"dflt_value":"NULL","name":"recovery_raw_before_hash","notnull":false,"pk":0,"type":"TEXT"},{"cid":21,"dflt_value":"NULL","name":"recovery_raw_after_hash","notnull":false,"pk":0,"type":"TEXT"},{"cid":22,"dflt_value":"NULL","name":"recovery_observed_at_before","notnull":false,"pk":0,"type":"TEXT"},{"cid":23,"dflt_value":"NULL","name":"recovery_observed_at_after","notnull":false,"pk":0,"type":"TEXT"},{"cid":24,"dflt_value":"NULL","name":"recovery_raw_time_changes","notnull":false,"pk":0,"type":"TEXT"}],"tbl_name":"data_governance_quarantine_records","type":"table"},{"foreign_keys":[],"name":"market_observations","sql":"CREATE TABLE market_observations ( observation_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source_id TEXT NOT NULL, observed_at TEXT NOT NULL, indicator TEXT NOT NULL, product TEXT NOT NULL, value REAL, unit TEXT NOT NULL, frequency TEXT NOT NULL, region TEXT NOT NULL, evidence_url TEXT NOT NULL, notes TEXT NOT NULL, raw TEXT NOT NULL )","table_info":[{"cid":0,"dflt_value":null,"name":"observation_id","notnull":false,"pk":1,"type":"TEXT"},{"cid":1,"dflt_value":null,"name":"created_at","notnull":true,"pk":0,"type":"TEXT"},{"cid":2,"dflt_value":null,"name":"source_id","notnull":true,"pk":0,"type":"TEXT"},{"cid":3,"dflt_value":null,"name":"observed_at","notnull":true,"pk":0,"type":"TEXT"},{"cid":4,"dflt_value":null,"name":"indicator","notnull":true,"pk":0,"type":"TEXT"},{"cid":5,"dflt_value":null,"name":"product","notnull":true,"pk":0,"type":"TEXT"},{"cid":6,"dflt_value":null,"name":"value","notnull":false,"pk":0,"type":"REAL"},{"cid":7,"dflt_value":null,"name":"unit","notnull":true,"pk":0,"type":"TEXT"},{"cid":8,"dflt_value":null,"name":"frequency","notnull":true,"pk":0,"type":"TEXT"},{"cid":9,"dflt_value":null,"name":"region","notnull":true,"pk":0,"type":"TEXT"},{"cid":10,"dflt_value":null,"name":"evidence_url","notnull":true,"pk":0,"type":"TEXT"},{"cid":11,"dflt_value":null,"name":"notes","notnull":true,"pk":0,"type":"TEXT"},{"cid":12,"dflt_value":null,"name":"raw","notnull":true,"pk":0,"type":"TEXT"}],"tbl_name":"market_observations","type":"table"},{"index_list":{"origin":"pk","partial":false,"unique":true},"index_xinfo":[{"cid":0,"coll":"BINARY","desc":false,"key":true,"name":"link_id","seqno":0},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":1}],"name":"auto/data_governance_quarantine_correction_links/pk/469b85d35882dadd2b14ebe58309fd5010c17045523092b0f041a1a62d167759","sql":null,"tbl_name":"data_governance_quarantine_correction_links","type":"index"},{"index_list":{"origin":"u","partial":false,"unique":true},"index_xinfo":[{"cid":2,"coll":"BINARY","desc":false,"key":true,"name":"original_quarantine_id","seqno":0},{"cid":3,"coll":"BINARY","desc":false,"key":true,"name":"correction_quarantine_id","seqno":1},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":2}],"name":"auto/data_governance_quarantine_correction_links/u/36f813de71f780fae4aceba4cc7e4a723ec3176e2bd5c21aeab391071f79656f","sql":null,"tbl_name":"data_governance_quarantine_correction_links","type":"index"},{"index_list":{"origin":"pk","partial":false,"unique":true},"index_xinfo":[{"cid":0,"coll":"BINARY","desc":false,"key":true,"name":"quarantine_id","seqno":0},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":1}],"name":"auto/data_governance_quarantine_records/pk/af9cd7aabaa16e690971bf00befa7ee732c8e985bf33286ce0642b08d5fbf89a","sql":null,"tbl_name":"data_governance_quarantine_records","type":"index"},{"index_list":{"origin":"u","partial":false,"unique":true},"index_xinfo":[{"cid":4,"coll":"BINARY","desc":false,"key":true,"name":"source_id","seqno":0},{"cid":5,"coll":"BINARY","desc":false,"key":true,"name":"payload_hash","seqno":1},{"cid":7,"coll":"BINARY","desc":false,"key":true,"name":"failure_code","seqno":2},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":3}],"name":"auto/data_governance_quarantine_records/u/3b8d3fd7a7eb8433235fc8ae7965da7d3c7e19a4029cf817d15d285b006f6304","sql":null,"tbl_name":"data_governance_quarantine_records","type":"index"},{"index_list":{"origin":"pk","partial":false,"unique":true},"index_xinfo":[{"cid":0,"coll":"BINARY","desc":false,"key":true,"name":"observation_id","seqno":0},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":1}],"name":"auto/market_observations/pk/44bfc8141495b314daf25327d875c29376af17f29abf8f3665af5c22f51f91de","sql":null,"tbl_name":"market_observations","type":"index"},{"index_list":{"origin":"c","partial":false,"unique":false},"index_xinfo":[{"cid":3,"coll":"BINARY","desc":false,"key":true,"name":"correction_quarantine_id","seqno":0},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":1}],"name":"idx_dg_quarantine_correction_reverse","sql":"CREATE INDEX idx_dg_quarantine_correction_reverse ON data_governance_quarantine_correction_links(correction_quarantine_id)","tbl_name":"data_governance_quarantine_correction_links","type":"index"},{"index_list":{"origin":"c","partial":true,"unique":false},"index_xinfo":[{"cid":14,"coll":"BINARY","desc":false,"key":true,"name":"recovered_observation_id","seqno":0},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":1}],"name":"idx_dg_quarantine_recovered_observation","sql":"CREATE INDEX idx_dg_quarantine_recovered_observation ON data_governance_quarantine_records(recovered_observation_id) WHERE recovered_observation_id IS NOT NULL","tbl_name":"data_governance_quarantine_records","type":"index"},{"index_list":{"origin":"c","partial":false,"unique":false},"index_xinfo":[{"cid":4,"coll":"BINARY","desc":false,"key":true,"name":"source_id","seqno":0},{"cid":2,"coll":"BINARY","desc":true,"key":true,"name":"received_at","seqno":1},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":2}],"name":"idx_dg_quarantine_source_received","sql":"CREATE INDEX idx_dg_quarantine_source_received ON data_governance_quarantine_records(source_id, received_at DESC)","tbl_name":"data_governance_quarantine_records","type":"index"},{"index_list":{"origin":"c","partial":false,"unique":false},"index_xinfo":[{"cid":10,"coll":"BINARY","desc":false,"key":true,"name":"status","seqno":0},{"cid":7,"coll":"BINARY","desc":false,"key":true,"name":"failure_code","seqno":1},{"cid":2,"coll":"BINARY","desc":true,"key":true,"name":"received_at","seqno":2},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":3}],"name":"idx_dg_quarantine_status_failure_received","sql":"CREATE INDEX idx_dg_quarantine_status_failure_received ON data_governance_quarantine_records(status, failure_code, received_at DESC)","tbl_name":"data_governance_quarantine_records","type":"index"},{"index_list":{"origin":"c","partial":false,"unique":false},"index_xinfo":[{"cid":2,"coll":"BINARY","desc":false,"key":true,"name":"source_id","seqno":0},{"cid":3,"coll":"BINARY","desc":false,"key":true,"name":"observed_at","seqno":1},{"cid":4,"coll":"BINARY","desc":false,"key":true,"name":"indicator","seqno":2},{"cid":5,"coll":"BINARY","desc":false,"key":true,"name":"product","seqno":3},{"cid":-1,"coll":"BINARY","desc":false,"key":false,"name":null,"seqno":4}],"name":"idx_market_observation_identity","sql":"CREATE INDEX idx_market_observation_identity ON market_observations(source_id, observed_at, indicator, product)","tbl_name":"market_observations","type":"index"},{"name":"trg_dg_quarantine_correction_link_exists","sql":"CREATE TRIGGER trg_dg_quarantine_correction_link_exists BEFORE INSERT ON data_governance_quarantine_correction_links WHEN NOT EXISTS ( SELECT 1 FROM data_governance_quarantine_records WHERE quarantine_id = NEW.original_quarantine_id ) OR NOT EXISTS ( SELECT 1 FROM data_governance_quarantine_records WHERE quarantine_id = NEW.correction_quarantine_id ) BEGIN SELECT RAISE(ABORT, 'correction_quarantine_not_found'); END","tbl_name":"data_governance_quarantine_correction_links","type":"trigger"},{"name":"trg_dg_quarantine_correction_link_no_delete","sql":"CREATE TRIGGER trg_dg_quarantine_correction_link_no_delete BEFORE DELETE ON data_governance_quarantine_correction_links BEGIN SELECT RAISE(ABORT, 'correction_link_immutable'); END","tbl_name":"data_governance_quarantine_correction_links","type":"trigger"},{"name":"trg_dg_quarantine_correction_link_no_update","sql":"CREATE TRIGGER trg_dg_quarantine_correction_link_no_update BEFORE UPDATE ON data_governance_quarantine_correction_links BEGIN SELECT RAISE(ABORT, 'correction_link_immutable'); END","tbl_name":"data_governance_quarantine_correction_links","type":"trigger"},{"name":"trg_dg_quarantine_immutable_fact","sql":"CREATE TRIGGER trg_dg_quarantine_immutable_fact BEFORE UPDATE OF quarantine_id, created_at, received_at, source_id, payload_hash, hash_algorithm_version, failure_code, failure_detail, raw_payload ON data_governance_quarantine_records BEGIN SELECT RAISE(ABORT, 'quarantine_fact_immutable'); END","tbl_name":"data_governance_quarantine_records","type":"trigger"},{"name":"trg_dg_quarantine_no_delete","sql":"CREATE TRIGGER trg_dg_quarantine_no_delete BEFORE DELETE ON data_governance_quarantine_records BEGIN SELECT RAISE(ABORT, 'quarantine_fact_immutable'); END","tbl_name":"data_governance_quarantine_records","type":"trigger"},{"name":"trg_dg_quarantine_recovery_observation_exists","sql":"CREATE TRIGGER trg_dg_quarantine_recovery_observation_exists BEFORE UPDATE OF status, recovered_observation_id ON data_governance_quarantine_records WHEN NEW.status = 'recovered' AND NOT EXISTS ( SELECT 1 FROM market_observations WHERE observation_id = NEW.recovered_observation_id ) BEGIN SELECT RAISE(ABORT, 'recovery_observation_not_found'); END","tbl_name":"data_governance_quarantine_records","type":"trigger"},{"name":"trg_dg_quarantine_recovery_only","sql":"CREATE TRIGGER trg_dg_quarantine_recovery_only BEFORE UPDATE OF status, updated_at, recovered_at, recovery_payload_hash, recovery_hash_algorithm_version, recovered_observation_id, recovery_detail, verified_recovery_projection_hash, verified_recovery_hash_algorithm_version, verified_recovery_contract_version, verified_original_projection_hash, recovery_raw_before_hash, recovery_raw_after_hash, recovery_observed_at_before, recovery_observed_at_after, recovery_raw_time_changes ON data_governance_quarantine_records WHEN NOT ( OLD.status = 'quarantined' AND NEW.status = 'recovered' AND NEW.updated_at = NEW.recovered_at AND NEW.recovered_at IS NOT NULL AND NEW.verified_recovery_contract_version = 'dg-timestamp-recovery-v1' ) BEGIN SELECT CASE WHEN NEW.failure_detail IS NOT OLD.failure_detail THEN RAISE(ABORT, 'quarantine_fact_immutable') END; SELECT RAISE(ABORT, 'invalid_quarantine_state_transition'); END","tbl_name":"data_governance_quarantine_records","type":"trigger"},{"name":"trg_dg_recovered_observation_no_delete","sql":"CREATE TRIGGER trg_dg_recovered_observation_no_delete BEFORE DELETE ON market_observations WHEN EXISTS ( SELECT 1 FROM data_governance_quarantine_records WHERE status = 'recovered' AND recovered_observation_id = OLD.observation_id ) BEGIN SELECT RAISE(ABORT, 'recovered_observation_immutable'); END","tbl_name":"market_observations","type":"trigger"},{"name":"trg_dg_recovered_observation_no_update","sql":"CREATE TRIGGER trg_dg_recovered_observation_no_update BEFORE UPDATE ON market_observations WHEN EXISTS ( SELECT 1 FROM data_governance_quarantine_records WHERE status = 'recovered' AND recovered_observation_id = OLD.observation_id ) AND ( NEW.observation_id IS NOT OLD.observation_id OR NEW.created_at IS NOT OLD.created_at OR NEW.source_id IS NOT OLD.source_id OR NEW.observed_at IS NOT OLD.observed_at OR NEW.indicator IS NOT OLD.indicator OR NEW.product IS NOT OLD.product OR NEW.value IS NOT OLD.value OR NEW.unit IS NOT OLD.unit OR NEW.frequency IS NOT OLD.frequency OR NEW.region IS NOT OLD.region OR NEW.evidence_url IS NOT OLD.evidence_url OR NEW.notes IS NOT OLD.notes OR NEW.raw IS NOT OLD.raw ) BEGIN SELECT RAISE(ABORT, 'recovered_observation_immutable'); END","tbl_name":"market_observations","type":"trigger"}],"runtime_pragmas":{"defer_foreign_keys":false,"foreign_keys":true,"legacy_alter_table":false}}"""
)
EXPECTED_V24_SCHEMA_DIGEST = hashlib.sha256(
    json.dumps(
        EXPECTED_V24_SCHEMA_MANIFEST,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()
EXPECTED_V25_SCHEMA_DIGEST = hashlib.sha256(
    json.dumps(
        EXPECTED_V25_SCHEMA_MANIFEST,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()
TIMESTAMP_FAILURE_CODE = "timestamp_invalid"
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_RFC3339_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)
_NAIVE_DATETIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?")

# Only these tables use `source_id` as an external data-source identifier.
# Other tables may use the same column name for internal graph/memory/agent links.
SOURCE_ATTRIBUTION_TABLES = {
    "source_fetch_audit",
    "source_acquisition_runs",
    "market_observations",
    "industry_observations",
    "intraday_price_observations",
    "forecast_price_points",
    "futures_daily_bars",
    "event_observations",
    "event_intelligence_snapshots",
    "political_event_cases",
    "news_fetch_runs",
    "news_articles",
    "news_event_clusters",
    "llm_event_directions",
    "rag_documents",
    "semantic_documents",
}

UNCONFIGURED_VENDORS = {
    "sci99": {
        "name": "卓创资讯",
        "status": "authorization_unknown_unconfigured",
        "reconciliation": "prohibited_until_authorization_confirmed",
    },
    "oilchem": {
        "name": "隆众资讯",
        "status": "authorization_unknown_unconfigured",
        "reconciliation": "prohibited_until_authorization_confirmed",
    },
}


_GOVERNANCE_V22_SQL = """
        CREATE TABLE IF NOT EXISTS data_source_governance (
          source_id TEXT PRIMARY KEY,
          source_name TEXT NOT NULL DEFAULT '',
          authority_status TEXT NOT NULL,
          authorization_status TEXT NOT NULL,
          comparison_mode TEXT NOT NULL,
          registry_status TEXT NOT NULL,
          tier TEXT NOT NULL DEFAULT '',
          auth_type TEXT NOT NULL DEFAULT '',
          canonical_url TEXT NOT NULL DEFAULT '',
          license_note TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS authoritative_reconciliations (
          reconciliation_id TEXT PRIMARY KEY,
          dataset_name TEXT NOT NULL,
          record_key TEXT NOT NULL,
          source_id TEXT NOT NULL,
          authoritative_source_id TEXT NOT NULL,
          observed_at TEXT NOT NULL,
          actual_value REAL,
          authoritative_value REAL,
          unit TEXT NOT NULL DEFAULT '',
          tolerance_abs REAL,
          delta_abs REAL,
          verdict TEXT NOT NULL CHECK (verdict IN ('matched','mismatched','not_comparable','missing_authority')),
          actual_evidence_url TEXT NOT NULL DEFAULT '',
          authoritative_evidence_url TEXT NOT NULL DEFAULT '',
          checked_at TEXT NOT NULL,
          notes TEXT NOT NULL DEFAULT '',
          UNIQUE(dataset_name, record_key, authoritative_source_id, checked_at)
        );
        CREATE INDEX IF NOT EXISTS idx_authoritative_reconciliations_dataset
          ON authoritative_reconciliations(dataset_name, verdict, checked_at);
        CREATE TABLE IF NOT EXISTS data_governance_audit_runs (
          run_id TEXT PRIMARY KEY,
          generated_at TEXT NOT NULL,
          status TEXT NOT NULL,
          summary_json TEXT NOT NULL,
          report_json_path TEXT NOT NULL DEFAULT '',
          report_markdown_path TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS data_governance_dataset_results (
          result_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          table_name TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          trust_status TEXT NOT NULL,
          verification_status TEXT NOT NULL,
          details_json TEXT NOT NULL,
          FOREIGN KEY(run_id) REFERENCES data_governance_audit_runs(run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_data_governance_results_run
          ON data_governance_dataset_results(run_id, table_name);
        CREATE TABLE IF NOT EXISTS data_governance_source_results (
          result_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          source_id TEXT NOT NULL,
          registry_status TEXT NOT NULL,
          authority_status TEXT NOT NULL,
          authorization_status TEXT NOT NULL,
          observed_rows INTEGER NOT NULL,
          datasets_json TEXT NOT NULL,
          FOREIGN KEY(run_id) REFERENCES data_governance_audit_runs(run_id),
          UNIQUE(run_id, source_id)
        );
        CREATE INDEX IF NOT EXISTS idx_data_governance_source_results_run
          ON data_governance_source_results(run_id, registry_status, source_id);
"""

_TIMESTAMP_QUARANTINE_V24_SQL = """
        CREATE TABLE IF NOT EXISTS data_governance_quarantine_records (
          quarantine_id TEXT PRIMARY KEY
            CHECK (length(trim(quarantine_id)) > 0),
          created_at TEXT NOT NULL
            CHECK (length(trim(created_at)) > 0),
          received_at TEXT NOT NULL
            CHECK (length(trim(received_at)) > 0),
          updated_at TEXT NOT NULL
            CHECK (length(trim(updated_at)) > 0),
          source_id TEXT NOT NULL
            CHECK (length(trim(source_id)) > 0),
          payload_hash TEXT NOT NULL
            CHECK (
              length(payload_hash) = 64
              AND payload_hash NOT GLOB '*[^0-9a-f]*'
            ),
          hash_algorithm_version TEXT NOT NULL
            DEFAULT 'sha256:dg-cjson-v1'
            CHECK (hash_algorithm_version = 'sha256:dg-cjson-v1'),
          failure_code TEXT NOT NULL
            DEFAULT 'timestamp_invalid'
            CHECK (failure_code = 'timestamp_invalid'),
          failure_detail TEXT NOT NULL
            DEFAULT ''
            CHECK (length(failure_detail) <= 512),
          raw_payload TEXT NOT NULL
            CHECK (length(raw_payload) > 0 AND json_valid(raw_payload)),
          status TEXT NOT NULL
            DEFAULT 'quarantined'
            CHECK (status IN ('quarantined', 'recovered')),
          recovered_at TEXT DEFAULT NULL,
          recovery_payload_hash TEXT DEFAULT NULL,
          recovery_hash_algorithm_version TEXT DEFAULT NULL,
          recovered_observation_id TEXT DEFAULT NULL
            REFERENCES market_observations(observation_id),
          recovery_detail TEXT NOT NULL
            DEFAULT ''
            CHECK (length(recovery_detail) <= 512),
          UNIQUE (source_id, payload_hash, failure_code),
          CHECK (
            recovery_payload_hash IS NULL
            OR (
              length(recovery_payload_hash) = 64
              AND recovery_payload_hash NOT GLOB '*[^0-9a-f]*'
            )
          ),
          CHECK (
            (
              status = 'quarantined'
              AND recovered_at IS NULL
              AND recovery_payload_hash IS NULL
              AND recovery_hash_algorithm_version IS NULL
              AND recovered_observation_id IS NULL
            )
            OR
            (
              status = 'recovered'
              AND recovered_at IS NOT NULL
              AND recovery_payload_hash IS NOT NULL
              AND recovery_hash_algorithm_version = 'sha256:dg-cjson-v1'
              AND recovered_observation_id IS NOT NULL
              AND recovery_payload_hash <> payload_hash
            )
          )
        );
        CREATE TABLE IF NOT EXISTS data_governance_quarantine_correction_links (
          link_id TEXT PRIMARY KEY
            CHECK (length(trim(link_id)) > 0),
          created_at TEXT NOT NULL
            CHECK (length(trim(created_at)) > 0),
          original_quarantine_id TEXT NOT NULL
            REFERENCES data_governance_quarantine_records(quarantine_id),
          correction_quarantine_id TEXT NOT NULL
            REFERENCES data_governance_quarantine_records(quarantine_id),
          CHECK (original_quarantine_id <> correction_quarantine_id),
          UNIQUE (original_quarantine_id, correction_quarantine_id)
        );
        CREATE INDEX IF NOT EXISTS idx_dg_quarantine_status_failure_received
          ON data_governance_quarantine_records(status, failure_code, received_at DESC);
        CREATE INDEX IF NOT EXISTS idx_dg_quarantine_source_received
          ON data_governance_quarantine_records(source_id, received_at DESC);
        CREATE INDEX IF NOT EXISTS idx_dg_quarantine_recovered_observation
          ON data_governance_quarantine_records(recovered_observation_id)
          WHERE recovered_observation_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_dg_quarantine_correction_reverse
          ON data_governance_quarantine_correction_links(correction_quarantine_id);
        CREATE TRIGGER IF NOT EXISTS trg_dg_quarantine_immutable_fact
        BEFORE UPDATE OF
          quarantine_id,
          created_at,
          received_at,
          source_id,
          payload_hash,
          hash_algorithm_version,
          failure_code,
          raw_payload
        ON data_governance_quarantine_records
        BEGIN
          SELECT RAISE(ABORT, 'quarantine_fact_immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_dg_quarantine_no_delete
        BEFORE DELETE ON data_governance_quarantine_records
        BEGIN
          SELECT RAISE(ABORT, 'quarantine_fact_immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_dg_quarantine_recovery_observation_exists
        BEFORE UPDATE OF status, recovered_observation_id
        ON data_governance_quarantine_records
        WHEN NEW.status = 'recovered'
         AND NOT EXISTS (
           SELECT 1
           FROM market_observations
           WHERE observation_id = NEW.recovered_observation_id
         )
        BEGIN
          SELECT RAISE(ABORT, 'recovery_observation_not_found');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_dg_quarantine_recovery_only
        BEFORE UPDATE ON data_governance_quarantine_records
        WHEN NOT (
          OLD.status = 'quarantined'
          AND NEW.status = 'recovered'
          AND NEW.updated_at = NEW.recovered_at
        )
        BEGIN
          SELECT RAISE(ABORT, 'invalid_quarantine_state_transition');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_dg_quarantine_correction_link_exists
        BEFORE INSERT ON data_governance_quarantine_correction_links
        WHEN NOT EXISTS (
               SELECT 1
               FROM data_governance_quarantine_records
               WHERE quarantine_id = NEW.original_quarantine_id
             )
          OR NOT EXISTS (
               SELECT 1
               FROM data_governance_quarantine_records
               WHERE quarantine_id = NEW.correction_quarantine_id
             )
        BEGIN
          SELECT RAISE(ABORT, 'correction_quarantine_not_found');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_dg_quarantine_correction_link_no_update
        BEFORE UPDATE ON data_governance_quarantine_correction_links
        BEGIN
          SELECT RAISE(ABORT, 'correction_link_immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS trg_dg_quarantine_correction_link_no_delete
        BEFORE DELETE ON data_governance_quarantine_correction_links
        BEGIN
          SELECT RAISE(ABORT, 'correction_link_immutable');
        END;
"""

_V25_QUARANTINE_TABLE_SQL = """
CREATE TABLE data_governance_quarantine_records (
  quarantine_id TEXT PRIMARY KEY CHECK (length(trim(quarantine_id)) > 0),
  created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
  received_at TEXT NOT NULL CHECK (length(trim(received_at)) > 0),
  updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
  source_id TEXT NOT NULL CHECK (length(trim(source_id)) > 0),
  payload_hash TEXT NOT NULL
    CHECK (length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'),
  hash_algorithm_version TEXT NOT NULL DEFAULT 'sha256:dg-cjson-v1'
    CHECK (hash_algorithm_version = 'sha256:dg-cjson-v1'),
  failure_code TEXT NOT NULL DEFAULT 'timestamp_invalid'
    CHECK (failure_code = 'timestamp_invalid'),
  failure_detail TEXT NOT NULL DEFAULT '' CHECK (length(failure_detail) <= 512),
  raw_payload TEXT NOT NULL CHECK (length(raw_payload) > 0 AND json_valid(raw_payload)),
  status TEXT NOT NULL DEFAULT 'quarantined' CHECK (status IN ('quarantined','recovered')),
  recovered_at TEXT DEFAULT NULL,
  recovery_payload_hash TEXT DEFAULT NULL
    CHECK (
      recovery_payload_hash IS NULL OR
      (length(recovery_payload_hash) = 64 AND recovery_payload_hash NOT GLOB '*[^0-9a-f]*')
    ),
  recovery_hash_algorithm_version TEXT DEFAULT NULL
    CHECK (
      recovery_hash_algorithm_version IS NULL OR
      recovery_hash_algorithm_version = 'sha256:dg-cjson-v1'
    ),
  recovered_observation_id TEXT DEFAULT NULL
    REFERENCES market_observations(observation_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  recovery_detail TEXT NOT NULL DEFAULT '' CHECK (length(recovery_detail) <= 512),
  verified_recovery_projection_hash TEXT DEFAULT NULL
    CHECK (
      verified_recovery_projection_hash IS NULL OR
      (length(verified_recovery_projection_hash) = 64
       AND verified_recovery_projection_hash NOT GLOB '*[^0-9a-f]*')
    ),
  verified_recovery_hash_algorithm_version TEXT DEFAULT NULL
    CHECK (
      verified_recovery_hash_algorithm_version IS NULL OR
      verified_recovery_hash_algorithm_version = 'sha256:dg-cjson-v1'
    ),
  verified_recovery_contract_version TEXT DEFAULT NULL
    CHECK (
      verified_recovery_contract_version IS NULL OR
      verified_recovery_contract_version = 'dg-timestamp-recovery-v1'
    ),
  verified_original_projection_hash TEXT DEFAULT NULL
    CHECK (
      verified_original_projection_hash IS NULL OR
      (length(verified_original_projection_hash) = 64
       AND verified_original_projection_hash NOT GLOB '*[^0-9a-f]*')
    ),
  recovery_raw_before_hash TEXT DEFAULT NULL
    CHECK (
      recovery_raw_before_hash IS NULL OR
      (length(recovery_raw_before_hash) = 64
       AND recovery_raw_before_hash NOT GLOB '*[^0-9a-f]*')
    ),
  recovery_raw_after_hash TEXT DEFAULT NULL
    CHECK (
      recovery_raw_after_hash IS NULL OR
      (length(recovery_raw_after_hash) = 64
       AND recovery_raw_after_hash NOT GLOB '*[^0-9a-f]*')
    ),
  recovery_observed_at_before TEXT DEFAULT NULL,
  recovery_observed_at_after TEXT DEFAULT NULL,
  recovery_raw_time_changes TEXT DEFAULT NULL
    CHECK (
      recovery_raw_time_changes IS NULL OR
      (json_valid(recovery_raw_time_changes) AND json_type(recovery_raw_time_changes) = 'array')
    ),
  UNIQUE (source_id, payload_hash, failure_code),
  CHECK (
    (
      status = 'quarantined'
      AND recovered_at IS NULL
      AND recovery_payload_hash IS NULL
      AND recovery_hash_algorithm_version IS NULL
      AND recovered_observation_id IS NULL
      AND verified_recovery_projection_hash IS NULL
      AND verified_recovery_hash_algorithm_version IS NULL
      AND verified_recovery_contract_version IS NULL
      AND verified_original_projection_hash IS NULL
      AND recovery_raw_before_hash IS NULL
      AND recovery_raw_after_hash IS NULL
      AND recovery_observed_at_before IS NULL
      AND recovery_observed_at_after IS NULL
      AND recovery_raw_time_changes IS NULL
    )
    OR
    (
      status = 'recovered'
      AND recovered_at IS NOT NULL
      AND updated_at = recovered_at
      AND recovery_payload_hash IS NOT NULL
      AND recovery_hash_algorithm_version = 'sha256:dg-cjson-v1'
      AND recovered_observation_id IS NOT NULL
      AND verified_recovery_projection_hash IS NOT NULL
      AND verified_recovery_hash_algorithm_version = 'sha256:dg-cjson-v1'
      AND verified_recovery_contract_version = 'dg-timestamp-recovery-v1'
      AND verified_original_projection_hash IS NOT NULL
      AND recovery_raw_before_hash IS NOT NULL
      AND recovery_raw_after_hash IS NOT NULL
      AND recovery_observed_at_before IS NOT NULL
      AND recovery_observed_at_after IS NOT NULL
      AND recovery_observed_at_before <> recovery_observed_at_after
      AND recovery_raw_time_changes IS NOT NULL
    )
  )
)
"""

_V25_CORRECTION_TABLE_SQL = """
CREATE TABLE data_governance_quarantine_correction_links (
  link_id TEXT PRIMARY KEY CHECK (length(trim(link_id)) > 0),
  created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
  original_quarantine_id TEXT NOT NULL
    REFERENCES data_governance_quarantine_records(quarantine_id)
    ON UPDATE RESTRICT ON DELETE RESTRICT,
  correction_quarantine_id TEXT NOT NULL
    REFERENCES data_governance_quarantine_records(quarantine_id)
    ON UPDATE RESTRICT ON DELETE RESTRICT,
  CHECK (original_quarantine_id <> correction_quarantine_id),
  UNIQUE (original_quarantine_id, correction_quarantine_id)
)
"""

_V25_INDEX_SQL = (
    """
    CREATE INDEX idx_dg_quarantine_status_failure_received
    ON data_governance_quarantine_records(status, failure_code, received_at DESC)
    """,
    """
    CREATE INDEX idx_dg_quarantine_source_received
    ON data_governance_quarantine_records(source_id, received_at DESC)
    """,
    """
    CREATE INDEX idx_dg_quarantine_recovered_observation
    ON data_governance_quarantine_records(recovered_observation_id)
    WHERE recovered_observation_id IS NOT NULL
    """,
    """
    CREATE INDEX idx_dg_quarantine_correction_reverse
    ON data_governance_quarantine_correction_links(correction_quarantine_id)
    """,
)

_V25_TRIGGER_SQL = (
    """
    CREATE TRIGGER trg_dg_quarantine_immutable_fact
    BEFORE UPDATE OF quarantine_id, created_at, received_at, source_id, payload_hash,
      hash_algorithm_version, failure_code, failure_detail, raw_payload
    ON data_governance_quarantine_records
    BEGIN
      SELECT RAISE(ABORT, 'quarantine_fact_immutable');
    END
    """,
    """
    CREATE TRIGGER trg_dg_quarantine_no_delete
    BEFORE DELETE ON data_governance_quarantine_records
    BEGIN
      SELECT RAISE(ABORT, 'quarantine_fact_immutable');
    END
    """,
    """
    CREATE TRIGGER trg_dg_quarantine_recovery_observation_exists
    BEFORE UPDATE OF status, recovered_observation_id
    ON data_governance_quarantine_records
    WHEN NEW.status = 'recovered'
     AND NOT EXISTS (
       SELECT 1 FROM market_observations
       WHERE observation_id = NEW.recovered_observation_id
     )
    BEGIN
      SELECT RAISE(ABORT, 'recovery_observation_not_found');
    END
    """,
    """
    CREATE TRIGGER trg_dg_quarantine_recovery_only
    BEFORE UPDATE OF status, updated_at, recovered_at, recovery_payload_hash,
      recovery_hash_algorithm_version, recovered_observation_id, recovery_detail,
      verified_recovery_projection_hash, verified_recovery_hash_algorithm_version,
      verified_recovery_contract_version, verified_original_projection_hash,
      recovery_raw_before_hash, recovery_raw_after_hash, recovery_observed_at_before,
      recovery_observed_at_after, recovery_raw_time_changes
    ON data_governance_quarantine_records
    WHEN NOT (
      OLD.status = 'quarantined'
      AND NEW.status = 'recovered'
      AND NEW.updated_at = NEW.recovered_at
      AND NEW.recovered_at IS NOT NULL
      AND NEW.verified_recovery_contract_version = 'dg-timestamp-recovery-v1'
    )
    BEGIN
      SELECT CASE
        WHEN NEW.failure_detail IS NOT OLD.failure_detail
        THEN RAISE(ABORT, 'quarantine_fact_immutable')
      END;
      SELECT RAISE(ABORT, 'invalid_quarantine_state_transition');
    END
    """,
    """
    CREATE TRIGGER trg_dg_quarantine_correction_link_exists
    BEFORE INSERT ON data_governance_quarantine_correction_links
    WHEN NOT EXISTS (
           SELECT 1 FROM data_governance_quarantine_records
           WHERE quarantine_id = NEW.original_quarantine_id
         )
      OR NOT EXISTS (
           SELECT 1 FROM data_governance_quarantine_records
           WHERE quarantine_id = NEW.correction_quarantine_id
         )
    BEGIN
      SELECT RAISE(ABORT, 'correction_quarantine_not_found');
    END
    """,
    """
    CREATE TRIGGER trg_dg_quarantine_correction_link_no_update
    BEFORE UPDATE ON data_governance_quarantine_correction_links
    BEGIN
      SELECT RAISE(ABORT, 'correction_link_immutable');
    END
    """,
    """
    CREATE TRIGGER trg_dg_quarantine_correction_link_no_delete
    BEFORE DELETE ON data_governance_quarantine_correction_links
    BEGIN
      SELECT RAISE(ABORT, 'correction_link_immutable');
    END
    """,
    """
    CREATE TRIGGER trg_dg_recovered_observation_no_update
    BEFORE UPDATE ON market_observations
    WHEN EXISTS (
      SELECT 1 FROM data_governance_quarantine_records
      WHERE status = 'recovered' AND recovered_observation_id = OLD.observation_id
    )
    AND (
      NEW.observation_id IS NOT OLD.observation_id
      OR NEW.created_at IS NOT OLD.created_at
      OR NEW.source_id IS NOT OLD.source_id
      OR NEW.observed_at IS NOT OLD.observed_at
      OR NEW.indicator IS NOT OLD.indicator
      OR NEW.product IS NOT OLD.product
      OR NEW.value IS NOT OLD.value
      OR NEW.unit IS NOT OLD.unit
      OR NEW.frequency IS NOT OLD.frequency
      OR NEW.region IS NOT OLD.region
      OR NEW.evidence_url IS NOT OLD.evidence_url
      OR NEW.notes IS NOT OLD.notes
      OR NEW.raw IS NOT OLD.raw
    )
    BEGIN
      SELECT RAISE(ABORT, 'recovered_observation_immutable');
    END
    """,
    """
    CREATE TRIGGER trg_dg_recovered_observation_no_delete
    BEFORE DELETE ON market_observations
    WHEN EXISTS (
      SELECT 1 FROM data_governance_quarantine_records
      WHERE status = 'recovered' AND recovered_observation_id = OLD.observation_id
    )
    BEGIN
      SELECT RAISE(ABORT, 'recovered_observation_immutable');
    END
    """,
)

_SCHEMA_TABLES = {
    "market_observations",
    "data_governance_quarantine_records",
    "data_governance_quarantine_correction_links",
}
_OBSERVATION_FIELDS = (
    "source_id",
    "observed_at",
    "indicator",
    "product",
    "value",
    "unit",
    "frequency",
    "region",
    "evidence_url",
    "notes",
    "raw",
)
_RAW_TIME_LEAF_NAMES = {"observed_at", "date", "timestamp", "datetime"}


def _ensure_governance_v22_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(_GOVERNANCE_V22_SQL)


def _ensure_timestamp_quarantine_v24_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(_TIMESTAMP_QUARANTINE_V24_SQL)


def ensure_governance_schema(connection: sqlite3.Connection) -> None:
    _ensure_governance_v22_schema(connection)
    _ensure_timestamp_quarantine_v24_schema(connection)


def canonicalize_governance_payload(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise TypeError("governance payload must be a JSON object")
    _validate_json_value(payload)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def governance_payload_hash(payload: dict[str, Any]) -> tuple[str, str]:
    raw_payload = canonicalize_governance_payload(payload)
    return hashlib.sha256(raw_payload.encode("utf-8")).hexdigest(), raw_payload


def _json_loads_no_duplicates(value: str) -> Any:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = item
        return result

    return json.loads(value, object_pairs_hook=pairs_hook)


def _canonical_bytes(value: Any) -> bytes:
    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _projection_envelope(projection: dict[str, Any]) -> bytes:
    return _canonical_bytes(
        {
            "contract": VERIFIED_RECOVERY_CONTRACT_VERSION,
            "observation": projection,
        }
    )


def _projection_from_quarantine(raw_payload: str) -> dict[str, Any]:
    payload = _json_loads_no_duplicates(raw_payload)
    if not isinstance(payload, dict) or set(payload) != set(_OBSERVATION_FIELDS):
        raise sqlite3.IntegrityError("recovery_payload_shape_invalid")
    if not isinstance(payload.get("raw"), dict):
        raise sqlite3.IntegrityError("recovery_payload_shape_invalid")
    return {field: payload[field] for field in _OBSERVATION_FIELDS}


def project_market_observation_for_recovery(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    projection = {field: row[field] for field in _OBSERVATION_FIELDS if field != "raw"}
    raw = row["raw"]
    if not isinstance(raw, str):
        raise sqlite3.IntegrityError("recovery_observation_unverifiable")
    try:
        parsed_raw = _json_loads_no_duplicates(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise sqlite3.IntegrityError("recovery_observation_unverifiable") from exc
    if not isinstance(parsed_raw, dict):
        raise sqlite3.IntegrityError("recovery_observation_unverifiable")
    projection["raw"] = parsed_raw
    return {field: projection[field] for field in _OBSERVATION_FIELDS}


def _decode_json_pointer(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise sqlite3.IntegrityError("recovery_raw_time_path_invalid")
    tokens: list[str] = []
    for encoded in path[1:].split("/"):
        if not encoded:
            raise sqlite3.IntegrityError("recovery_raw_time_path_invalid")
        decoded = ""
        index = 0
        while index < len(encoded):
            if encoded[index] != "~":
                decoded += encoded[index]
                index += 1
                continue
            if index + 1 >= len(encoded) or encoded[index + 1] not in {"0", "1"}:
                raise sqlite3.IntegrityError("recovery_raw_time_path_invalid")
            decoded += "~" if encoded[index + 1] == "0" else "/"
            index += 2
        tokens.append(decoded)
    if tokens[-1] not in _RAW_TIME_LEAF_NAMES:
        raise sqlite3.IntegrityError("recovery_raw_time_path_invalid")
    return tuple(tokens)


def _raw_value_at_path(raw: dict[str, Any], tokens: tuple[str, ...]) -> Any:
    current: Any = raw
    for token in tokens:
        if not isinstance(current, dict) or token not in current:
            raise sqlite3.IntegrityError("recovery_raw_time_path_invalid")
        current = current[token]
    if isinstance(current, (dict, list)):
        raise sqlite3.IntegrityError("recovery_raw_time_path_invalid")
    return current


def _replace_raw_path(raw: dict[str, Any], tokens: tuple[str, ...], value: Any) -> None:
    current: Any = raw
    for token in tokens[:-1]:
        if not isinstance(current, dict):
            raise sqlite3.IntegrityError("recovery_raw_time_path_invalid")
        current = current[token]
    if not isinstance(current, dict):
        raise sqlite3.IntegrityError("recovery_raw_time_path_invalid")
    current[tokens[-1]] = value


def _recovery_proof(
    original: dict[str, Any],
    formal: dict[str, Any],
    *,
    raw_time_paths: Sequence[str],
    historical_v24: bool,
) -> dict[str, str]:
    for field in _OBSERVATION_FIELDS:
        if field in {"observed_at", "raw"}:
            continue
        if _canonical_bytes(original[field]) != _canonical_bytes(formal[field]):
            raise sqlite3.IntegrityError("recovery_non_time_field_mismatch")
    before_observed_at = original["observed_at"]
    after_observed_at = formal["observed_at"]
    if (
        not isinstance(before_observed_at, str)
        or not isinstance(after_observed_at, str)
        or before_observed_at == after_observed_at
        or validate_observed_at_syntax(before_observed_at) is None
        or validate_observed_at_syntax(after_observed_at) is not None
    ):
        raise sqlite3.IntegrityError("recovery_payload_mismatch")

    original_raw = original["raw"]
    formal_raw = formal["raw"]
    original_raw_bytes = _canonical_bytes(original_raw)
    formal_raw_bytes = _canonical_bytes(formal_raw)
    changes: list[dict[str, str]] = []
    if historical_v24:
        if raw_time_paths or original_raw_bytes != formal_raw_bytes:
            raise sqlite3.IntegrityError("migration25_recovery_proof_unavailable")
    else:
        paths = list(raw_time_paths)
        if paths != sorted(paths, key=lambda item: item.encode("utf-8")) or len(paths) != len(set(paths)):
            raise sqlite3.IntegrityError("recovery_raw_time_path_invalid")
        transformed = _json_loads_no_duplicates(original_raw_bytes.decode("utf-8"))
        for path in paths:
            tokens = _decode_json_pointer(path)
            before = _raw_value_at_path(original_raw, tokens)
            after = _raw_value_at_path(formal_raw, tokens)
            if (
                not isinstance(before, str)
                or not isinstance(after, str)
                or before == after
                or validate_observed_at_syntax(before) is None
                or validate_observed_at_syntax(after) is not None
            ):
                raise sqlite3.IntegrityError("recovery_raw_time_path_invalid")
            _replace_raw_path(transformed, tokens, after)
            changes.append({"after": after, "before": before, "path": path})
        if _canonical_bytes(transformed) != formal_raw_bytes:
            raise sqlite3.IntegrityError("recovery_non_time_field_mismatch")

    return {
        "legacy_recovery_payload_hash": _sha256_bytes(_canonical_bytes(formal)),
        "verified_recovery_projection_hash": _sha256_bytes(_projection_envelope(formal)),
        "verified_recovery_hash_algorithm_version": HASH_ALGORITHM_VERSION,
        "verified_recovery_contract_version": VERIFIED_RECOVERY_CONTRACT_VERSION,
        "verified_original_projection_hash": _sha256_bytes(_projection_envelope(original)),
        "recovery_raw_before_hash": _sha256_bytes(original_raw_bytes),
        "recovery_raw_after_hash": _sha256_bytes(formal_raw_bytes),
        "recovery_observed_at_before": before_observed_at,
        "recovery_observed_at_after": after_observed_at,
        "recovery_raw_time_changes": _canonical_bytes(changes).decode("utf-8"),
    }


def _validate_recovery_audit(
    quarantine: sqlite3.Row,
    observation: sqlite3.Row,
    *,
    historical_v24: bool,
) -> dict[str, str]:
    if not historical_v24 and quarantine["status"] == "quarantined":
        verified_fields = (
            "verified_recovery_projection_hash",
            "verified_recovery_hash_algorithm_version",
            "verified_recovery_contract_version",
            "verified_original_projection_hash",
            "recovery_raw_before_hash",
            "recovery_raw_after_hash",
            "recovery_observed_at_before",
            "recovery_observed_at_after",
            "recovery_raw_time_changes",
        )
        if any(quarantine[field] is not None for field in verified_fields):
            raise sqlite3.IntegrityError("migration25_reconstruction_mismatch")
        return {}
    original = _projection_from_quarantine(str(quarantine["raw_payload"]))
    formal = project_market_observation_for_recovery(observation)
    if (
        quarantine["hash_algorithm_version"] != HASH_ALGORITHM_VERSION
        or quarantine["payload_hash"] != _sha256_bytes(_canonical_bytes(original))
        or quarantine["source_id"] != original["source_id"]
        or quarantine["failure_code"] != TIMESTAMP_FAILURE_CODE
    ):
        raise sqlite3.IntegrityError("verified_original_projection_hash_mismatch")
    raw_time_paths: tuple[str, ...] = ()
    if not historical_v24:
        try:
            changes = _json_loads_no_duplicates(str(quarantine["recovery_raw_time_changes"]))
            if not isinstance(changes, list) or any(
                not isinstance(change, dict)
                or set(change) != {"after", "before", "path"}
                or not isinstance(change["path"], str)
                for change in changes
            ):
                raise ValueError("invalid recovery raw audit")
            raw_time_paths = tuple(change["path"] for change in changes)
        except (TypeError, ValueError) as exc:
            raise sqlite3.IntegrityError("recovery_raw_audit_mismatch") from exc
    try:
        proof = _recovery_proof(
            original,
            formal,
            raw_time_paths=raw_time_paths,
            historical_v24=historical_v24,
        )
    except (TypeError, ValueError, sqlite3.Error) as exc:
        if not historical_v24:
            raise sqlite3.IntegrityError("recovery_raw_audit_mismatch") from exc
        raise
    if (
        quarantine["recovery_hash_algorithm_version"] != HASH_ALGORITHM_VERSION
        or quarantine["recovery_payload_hash"] != proof["legacy_recovery_payload_hash"]
    ):
        raise sqlite3.IntegrityError("legacy_recovery_payload_hash_mismatch")
    if historical_v24:
        return proof
    if (
        quarantine["verified_recovery_projection_hash"]
        != proof["verified_recovery_projection_hash"]
        or quarantine["verified_recovery_hash_algorithm_version"]
        != proof["verified_recovery_hash_algorithm_version"]
        or quarantine["verified_recovery_contract_version"]
        != proof["verified_recovery_contract_version"]
    ):
        raise sqlite3.IntegrityError("verified_recovery_projection_hash_mismatch")
    if (
        quarantine["verified_original_projection_hash"]
        != proof["verified_original_projection_hash"]
    ):
        raise sqlite3.IntegrityError("verified_original_projection_hash_mismatch")
    if any(
        quarantine[field] != proof[field]
        for field in (
            "recovery_raw_before_hash",
            "recovery_raw_after_hash",
            "recovery_observed_at_before",
            "recovery_observed_at_after",
            "recovery_raw_time_changes",
        )
    ):
        raise sqlite3.IntegrityError("recovery_raw_audit_mismatch")
    if (
        quarantine["status"] != "recovered"
        or quarantine["recovered_observation_id"] != observation["observation_id"]
        or quarantine["recovered_at"] is None
        or quarantine["updated_at"] != quarantine["recovered_at"]
    ):
        raise sqlite3.IntegrityError("migration25_reconstruction_mismatch")
    return proof


def _normalize_schema_sql(value: str | None) -> str | None:
    if value is None:
        return None
    output: list[str] = []
    quote: str | None = None
    pending_space = False
    index = 0
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip(" \t\n\f\v")
    while index < len(value):
        char = value[index]
        if quote is None:
            if char in {"'", '"', "`", "["}:
                if pending_space and output:
                    output.append(" ")
                pending_space = False
                quote = "]" if char == "[" else char
                output.append(char)
            elif char in " \t\n\f\v":
                pending_space = True
            else:
                if pending_space and output:
                    output.append(" ")
                pending_space = False
                output.append(char)
            index += 1
            continue
        output.append(char)
        if char == quote:
            if index + 1 < len(value) and value[index + 1] == quote and quote != "]":
                output.append(value[index + 1])
                index += 2
                continue
            quote = None
        index += 1
    return "".join(output)


def schema_manifest(connection: sqlite3.Connection) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    placeholders = ",".join("?" for _ in _SCHEMA_TABLES)
    master_rows = connection.execute(
        f"""
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE (
          type = 'table' AND name IN ({placeholders})
        ) OR (
          type = 'index' AND tbl_name IN ({placeholders})
        ) OR (
          type = 'trigger' AND tbl_name IN ({placeholders}) AND name LIKE 'trg_dg_%'
        )
        """,
        (*sorted(_SCHEMA_TABLES), *sorted(_SCHEMA_TABLES), *sorted(_SCHEMA_TABLES)),
    ).fetchall()
    index_metadata: dict[str, dict[str, Any]] = {}
    for table in sorted(_SCHEMA_TABLES):
        for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
            index_metadata[str(row[1])] = {
                "unique": bool(row[2]),
                "origin": str(row[3]),
                "partial": bool(row[4]),
            }
    for row in master_rows:
        object_type = str(row[0])
        name = str(row[1])
        table_name = str(row[2])
        item: dict[str, Any] = {
            "type": object_type,
            "name": name,
            "tbl_name": table_name,
            "sql": _normalize_schema_sql(row[3]),
        }
        if object_type == "table":
            item["table_info"] = [
                {
                    "cid": int(info[0]),
                    "name": str(info[1]),
                    "type": str(info[2]),
                    "notnull": bool(info[3]),
                    "dflt_value": info[4],
                    "pk": int(info[5]),
                }
                for info in sorted(
                    connection.execute(f'PRAGMA table_info("{name}")').fetchall(),
                    key=lambda value: int(value[0]),
                )
            ]
            item["foreign_keys"] = [
                {
                    "id": int(foreign_key[0]),
                    "seq": int(foreign_key[1]),
                    "table": str(foreign_key[2]),
                    "from": str(foreign_key[3]),
                    "to": str(foreign_key[4]),
                    "on_update": str(foreign_key[5]),
                    "on_delete": str(foreign_key[6]),
                    "match": str(foreign_key[7]),
                }
                for foreign_key in sorted(
                    connection.execute(f'PRAGMA foreign_key_list("{name}")').fetchall(),
                    key=lambda value: (int(value[0]), int(value[1])),
                )
            ]
        elif object_type == "index":
            xinfo = [
                {
                    "seqno": int(info[0]),
                    "cid": int(info[1]),
                    "name": info[2],
                    "desc": bool(info[3]),
                    "coll": info[4],
                    "key": bool(info[5]),
                }
                for info in sorted(
                    connection.execute(f'PRAGMA index_xinfo("{name}")').fetchall(),
                    key=lambda value: int(value[0]),
                )
            ]
            metadata = index_metadata[name]
            if item["sql"] is None:
                signature = _sha256_bytes(_canonical_bytes({**metadata, "xinfo": xinfo}))
                item["name"] = f"auto/{table_name}/{metadata['origin']}/{signature}"
            item["index_list"] = metadata
            item["index_xinfo"] = xinfo
        objects.append(item)
    ranks = {"table": 0, "index": 1, "trigger": 2}
    objects.sort(key=lambda item: (ranks[item["type"]], str(item["name"]).encode("utf-8")))
    return {
        "algorithm": SCHEMA_MANIFEST_ALGORITHM,
        "manifest_version": SCHEMA_MANIFEST_VERSION,
        "database_pragmas": {
            "application_id": int(connection.execute("PRAGMA application_id").fetchone()[0]),
            "encoding": str(connection.execute("PRAGMA encoding").fetchone()[0]),
            "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        },
        "runtime_pragmas": {
            "defer_foreign_keys": bool(connection.execute("PRAGMA defer_foreign_keys").fetchone()[0]),
            "foreign_keys": bool(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
            "legacy_alter_table": bool(connection.execute("PRAGMA legacy_alter_table").fetchone()[0]),
        },
        "objects": objects,
    }


def schema_manifest_digest(connection: sqlite3.Connection) -> tuple[str, dict[str, Any]]:
    manifest = schema_manifest(connection)
    return _sha256_bytes(_canonical_bytes(manifest)), manifest


def repair_timestamp_recovery_contract_v25(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise sqlite3.OperationalError("migration25_transaction_required")
    current_columns = {
        str(row[1])
        for row in connection.execute(
            'PRAGMA table_info("data_governance_quarantine_records")'
        ).fetchall()
    }
    if "verified_recovery_projection_hash" in current_columns:
        if connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name IN (
              'data_governance_quarantine_records_v24',
              'data_governance_quarantine_correction_links_v24'
            )
            UNION ALL
            SELECT 1 FROM sqlite_temp_master
            WHERE type = 'table' AND name = 'dg_m25_recovery_proof'
            """
        ).fetchone():
            raise sqlite3.IntegrityError("migration25_reconstruction_mismatch")
        for quarantine in connection.execute(
            "SELECT * FROM data_governance_quarantine_records ORDER BY quarantine_id"
        ).fetchall():
            if quarantine["status"] == "quarantined":
                _validate_recovery_audit(quarantine, quarantine, historical_v24=False)
                continue
            observation = connection.execute(
                "SELECT * FROM market_observations WHERE observation_id = ?",
                (quarantine["recovered_observation_id"],),
            ).fetchone()
            if observation is None:
                raise sqlite3.IntegrityError("recovery_observation_not_found")
            _validate_recovery_audit(quarantine, observation, historical_v24=False)
        return
    connection.execute(
        """
        CREATE TEMP TABLE dg_m25_recovery_proof (
          quarantine_id TEXT PRIMARY KEY,
          legacy_recovery_payload_hash TEXT NOT NULL,
          verified_recovery_projection_hash TEXT NOT NULL,
          verified_recovery_hash_algorithm_version TEXT NOT NULL,
          verified_recovery_contract_version TEXT NOT NULL,
          verified_original_projection_hash TEXT NOT NULL,
          recovery_raw_before_hash TEXT NOT NULL,
          recovery_raw_after_hash TEXT NOT NULL,
          recovery_observed_at_before TEXT NOT NULL,
          recovery_observed_at_after TEXT NOT NULL,
          recovery_raw_time_changes TEXT NOT NULL
        )
        """
    )
    try:
        quarantine_columns = (
            "quarantine_id",
            "created_at",
            "received_at",
            "updated_at",
            "source_id",
            "payload_hash",
            "hash_algorithm_version",
            "failure_code",
            "failure_detail",
            "raw_payload",
            "status",
            "recovered_at",
            "recovery_payload_hash",
            "recovery_hash_algorithm_version",
            "recovered_observation_id",
            "recovery_detail",
        )
        link_columns = (
            "link_id",
            "created_at",
            "original_quarantine_id",
            "correction_quarantine_id",
        )
        before_quarantine = connection.execute(
            "SELECT * FROM data_governance_quarantine_records ORDER BY quarantine_id"
        ).fetchall()
        before_links = connection.execute(
            "SELECT * FROM data_governance_quarantine_correction_links ORDER BY link_id"
        ).fetchall()
        before_quarantine_ids = tuple(row["quarantine_id"] for row in before_quarantine)
        before_link_ids = tuple(row["link_id"] for row in before_links)
        before_pairs = tuple(
            (row["original_quarantine_id"], row["correction_quarantine_id"])
            for row in before_links
        )
        quarantine_signature_sql = ", ".join(
            (
                f'typeof("{field}"), CASE '
                f'WHEN typeof("{field}") IN (\'blob\', \'text\') '
                f'THEN hex(CAST("{field}" AS BLOB)) ELSE quote("{field}") END'
            )
            for field in quarantine_columns
        )
        link_signature_sql = ", ".join(
            (
                f'typeof("{field}"), CASE '
                f'WHEN typeof("{field}") IN (\'blob\', \'text\') '
                f'THEN hex(CAST("{field}" AS BLOB)) ELSE quote("{field}") END'
            )
            for field in link_columns
        )
        before_quarantine_rowset = _canonical_bytes(
            [
                list(row)
                for row in connection.execute(
                    f"""
                    SELECT {quarantine_signature_sql}
                    FROM data_governance_quarantine_records
                    ORDER BY quarantine_id
                    """
                ).fetchall()
            ]
        )
        before_link_rowset = _canonical_bytes(
            [
                list(row)
                for row in connection.execute(
                    f"""
                    SELECT {link_signature_sql}
                    FROM data_governance_quarantine_correction_links
                    ORDER BY link_id
                    """
                ).fetchall()
            ]
        )
        before_quarantine_rowset_hash = _sha256_bytes(before_quarantine_rowset)
        before_link_rowset_hash = _sha256_bytes(before_link_rowset)
        before_legacy = {
            row["quarantine_id"]: (
                row["recovery_payload_hash"],
                row["recovery_hash_algorithm_version"],
            )
            for row in before_quarantine
        }
        recovered_rows = [row for row in before_quarantine if row["status"] == "recovered"]
        for quarantine in recovered_rows:
            observation = connection.execute(
                "SELECT * FROM market_observations WHERE observation_id = ?",
                (quarantine["recovered_observation_id"],),
            ).fetchone()
            if observation is None:
                raise sqlite3.IntegrityError("migration25_recovery_proof_unavailable")
            try:
                proof = _validate_recovery_audit(
                    quarantine,
                    observation,
                    historical_v24=True,
                )
            except sqlite3.IntegrityError as exc:
                if str(exc) == "legacy_recovery_payload_hash_mismatch":
                    raise
                raise sqlite3.IntegrityError("migration25_recovery_proof_unavailable") from exc
            connection.execute(
                """
                INSERT INTO temp.dg_m25_recovery_proof VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    quarantine["quarantine_id"],
                    proof["legacy_recovery_payload_hash"],
                    proof["verified_recovery_projection_hash"],
                    proof["verified_recovery_hash_algorithm_version"],
                    proof["verified_recovery_contract_version"],
                    proof["verified_original_projection_hash"],
                    proof["recovery_raw_before_hash"],
                    proof["recovery_raw_after_hash"],
                    proof["recovery_observed_at_before"],
                    proof["recovery_observed_at_after"],
                    proof["recovery_raw_time_changes"],
                ),
            )

        for name in (
            "trg_dg_quarantine_immutable_fact",
            "trg_dg_quarantine_no_delete",
            "trg_dg_quarantine_recovery_observation_exists",
            "trg_dg_quarantine_recovery_only",
            "trg_dg_quarantine_correction_link_exists",
            "trg_dg_quarantine_correction_link_no_update",
            "trg_dg_quarantine_correction_link_no_delete",
        ):
            connection.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        for name in (
            "idx_dg_quarantine_status_failure_received",
            "idx_dg_quarantine_source_received",
            "idx_dg_quarantine_recovered_observation",
            "idx_dg_quarantine_correction_reverse",
        ):
            connection.execute(f'DROP INDEX IF EXISTS "{name}"')
        connection.execute(
            """
            ALTER TABLE data_governance_quarantine_correction_links
            RENAME TO data_governance_quarantine_correction_links_v24
            """
        )
        connection.execute(
            """
            ALTER TABLE data_governance_quarantine_records
            RENAME TO data_governance_quarantine_records_v24
            """
        )
        connection.execute(_V25_QUARANTINE_TABLE_SQL)
        connection.execute(
            """
            INSERT INTO data_governance_quarantine_records (
              quarantine_id, created_at, received_at, updated_at, source_id,
              payload_hash, hash_algorithm_version, failure_code, failure_detail,
              raw_payload, status, recovered_at, recovery_payload_hash,
              recovery_hash_algorithm_version, recovered_observation_id, recovery_detail,
              verified_recovery_projection_hash,
              verified_recovery_hash_algorithm_version,
              verified_recovery_contract_version, verified_original_projection_hash,
              recovery_raw_before_hash, recovery_raw_after_hash,
              recovery_observed_at_before, recovery_observed_at_after,
              recovery_raw_time_changes
            )
            SELECT q.quarantine_id, q.created_at, q.received_at, q.updated_at, q.source_id,
                   q.payload_hash, q.hash_algorithm_version, q.failure_code, q.failure_detail,
                   q.raw_payload, q.status, q.recovered_at, q.recovery_payload_hash,
                   q.recovery_hash_algorithm_version, q.recovered_observation_id, q.recovery_detail,
                   p.verified_recovery_projection_hash,
                   p.verified_recovery_hash_algorithm_version,
                   p.verified_recovery_contract_version, p.verified_original_projection_hash,
                   p.recovery_raw_before_hash, p.recovery_raw_after_hash,
                   p.recovery_observed_at_before, p.recovery_observed_at_after,
                   p.recovery_raw_time_changes
            FROM data_governance_quarantine_records_v24 AS q
            LEFT JOIN temp.dg_m25_recovery_proof AS p
              ON p.quarantine_id = q.quarantine_id
            """
        )
        connection.execute(_V25_CORRECTION_TABLE_SQL)
        connection.execute(
            """
            INSERT INTO data_governance_quarantine_correction_links (
              link_id, created_at, original_quarantine_id, correction_quarantine_id
            )
            SELECT link_id, created_at, original_quarantine_id, correction_quarantine_id
            FROM data_governance_quarantine_correction_links_v24
            """
        )
        after_quarantine = connection.execute(
            "SELECT * FROM data_governance_quarantine_records ORDER BY quarantine_id"
        ).fetchall()
        after_links = connection.execute(
            "SELECT * FROM data_governance_quarantine_correction_links ORDER BY link_id"
        ).fetchall()
        if tuple(row["quarantine_id"] for row in after_quarantine) != before_quarantine_ids:
            raise sqlite3.IntegrityError("migration25_reconstruction_mismatch")
        if tuple(row["link_id"] for row in after_links) != before_link_ids:
            raise sqlite3.IntegrityError("migration25_reconstruction_mismatch")
        if tuple(
            (row["original_quarantine_id"], row["correction_quarantine_id"])
            for row in after_links
        ) != before_pairs:
            raise sqlite3.IntegrityError("migration25_reconstruction_mismatch")
        after_quarantine_rowset = _canonical_bytes(
            [
                list(row)
                for row in connection.execute(
                    f"""
                    SELECT {quarantine_signature_sql}
                    FROM data_governance_quarantine_records
                    ORDER BY quarantine_id
                    """
                ).fetchall()
            ]
        )
        if (
            after_quarantine_rowset != before_quarantine_rowset
            or _sha256_bytes(after_quarantine_rowset) != before_quarantine_rowset_hash
        ):
            raise sqlite3.IntegrityError("migration25_reconstruction_mismatch")
        after_link_rowset = _canonical_bytes(
            [
                list(row)
                for row in connection.execute(
                    f"""
                    SELECT {link_signature_sql}
                    FROM data_governance_quarantine_correction_links
                    ORDER BY link_id
                    """
                ).fetchall()
            ]
        )
        if (
            after_link_rowset != before_link_rowset
            or _sha256_bytes(after_link_rowset) != before_link_rowset_hash
        ):
            raise sqlite3.IntegrityError("migration25_reconstruction_mismatch")
        if {
            row["quarantine_id"]: (
                row["recovery_payload_hash"],
                row["recovery_hash_algorithm_version"],
            )
            for row in after_quarantine
        } != before_legacy:
            raise sqlite3.IntegrityError("migration25_reconstruction_mismatch")
        for quarantine in after_quarantine:
            if quarantine["status"] == "quarantined":
                _validate_recovery_audit(quarantine, quarantine, historical_v24=False)
                continue
            observation = connection.execute(
                "SELECT * FROM market_observations WHERE observation_id = ?",
                (quarantine["recovered_observation_id"],),
            ).fetchone()
            if observation is None:
                raise sqlite3.IntegrityError("migration25_recovery_proof_unavailable")
            _validate_recovery_audit(quarantine, observation, historical_v24=False)
        connection.execute("DROP TABLE data_governance_quarantine_correction_links_v24")
        connection.execute("DROP TABLE data_governance_quarantine_records_v24")
        for statement in _V25_INDEX_SQL:
            connection.execute(statement)
        for statement in _V25_TRIGGER_SQL:
            connection.execute(statement)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("migration25_foreign_key_check_failed")
    except BaseException:
        raise


def validate_observed_at_syntax(value: object) -> str | None:
    if not isinstance(value, str):
        return "timestamp_type_invalid"
    if _DATE_PATTERN.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            return "date_parse_failed"
        return None
    if _NAIVE_DATETIME_PATTERN.fullmatch(value):
        return "timezone_required"
    if not _RFC3339_PATTERN.fullmatch(value):
        return "rfc3339_parse_failed"
    parsed_value = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        datetime.fromisoformat(parsed_value)
    except ValueError:
        return "rfc3339_parse_failed"
    return None


def quarantine_timestamp_invalid(
    connection: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    failure_detail: str,
    received_at: str | None = None,
) -> dict[str, Any]:
    _validate_detail(failure_detail)
    payload_hash, raw_payload = governance_payload_hash(payload)
    timestamp = received_at or datetime.now(UTC).isoformat()
    quarantine_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO data_governance_quarantine_records (
          quarantine_id, created_at, received_at, updated_at, source_id,
          payload_hash, hash_algorithm_version, failure_code, failure_detail,
          raw_payload, status, recovery_detail
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'quarantined', '')
        ON CONFLICT(source_id, payload_hash, failure_code) DO NOTHING
        """,
        (
            quarantine_id,
            timestamp,
            timestamp,
            timestamp,
            str(payload.get("source_id") or ""),
            payload_hash,
            HASH_ALGORITHM_VERSION,
            TIMESTAMP_FAILURE_CODE,
            failure_detail,
            raw_payload,
        ),
    )
    row = connection.execute(
        """
        SELECT *
        FROM data_governance_quarantine_records
        WHERE source_id = ? AND payload_hash = ? AND failure_code = ?
        """,
        (str(payload.get("source_id") or ""), payload_hash, TIMESTAMP_FAILURE_CODE),
    ).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("quarantine_insert_not_visible")
    return dict(row)


def link_quarantine_correction(
    connection: sqlite3.Connection,
    *,
    original_quarantine_id: str,
    correction_quarantine_id: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    timestamp = created_at or datetime.now(UTC).isoformat()
    link_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO data_governance_quarantine_correction_links (
          link_id, created_at, original_quarantine_id, correction_quarantine_id
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(original_quarantine_id, correction_quarantine_id) DO NOTHING
        """,
        (link_id, timestamp, original_quarantine_id, correction_quarantine_id),
    )
    row = connection.execute(
        """
        SELECT *
        FROM data_governance_quarantine_correction_links
        WHERE original_quarantine_id = ? AND correction_quarantine_id = ?
        """,
        (original_quarantine_id, correction_quarantine_id),
    ).fetchone()
    if row is None:
        raise sqlite3.IntegrityError("correction_link_insert_not_visible")
    return dict(row)


def recover_quarantine_record(
    connection: sqlite3.Connection,
    *,
    quarantine_id: str,
    recovery_payload: dict[str, Any],
    recovered_observation_id: str,
    raw_time_paths: Sequence[str] = (),
    recovery_detail: str = "",
) -> dict[str, Any]:
    if connection.in_transaction:
        raise sqlite3.OperationalError("recovery_transaction_active")
    _validate_detail(recovery_detail)
    began = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        began = True
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise sqlite3.OperationalError("foreign_keys_required")
        timestamp = datetime.now(UTC).isoformat()
        quarantine = connection.execute(
            "SELECT * FROM data_governance_quarantine_records WHERE quarantine_id = ?",
            (quarantine_id,),
        ).fetchone()
        if quarantine is None:
            raise sqlite3.IntegrityError("quarantine_record_not_found")
        if quarantine["status"] == "recovered":
            if quarantine["recovered_observation_id"] != recovered_observation_id:
                raise sqlite3.IntegrityError("recovery_conflict")
            persisted_observation = connection.execute(
                "SELECT * FROM market_observations WHERE observation_id = ?",
                (quarantine["recovered_observation_id"],),
            ).fetchone()
            if persisted_observation is None:
                raise sqlite3.IntegrityError("recovery_observation_not_found")
            persisted_proof = _validate_recovery_audit(
                quarantine,
                persisted_observation,
                historical_v24=False,
            )
            persisted_changes = _json_loads_no_duplicates(
                persisted_proof["recovery_raw_time_changes"]
            )
            if (
                tuple(change["path"] for change in persisted_changes)
                != tuple(raw_time_paths)
            ):
                raise sqlite3.IntegrityError("recovery_conflict")
            if not isinstance(recovery_payload, dict) or set(recovery_payload) != set(
                _OBSERVATION_FIELDS
            ):
                raise sqlite3.IntegrityError("recovery_payload_shape_invalid")
            caller_projection = {
                field: recovery_payload[field] for field in _OBSERVATION_FIELDS
            }
            formal_projection = project_market_observation_for_recovery(
                persisted_observation
            )
            if _canonical_bytes(caller_projection) != _canonical_bytes(formal_projection):
                raise sqlite3.IntegrityError("recovery_payload_canonical_mismatch")
            original_projection = _projection_from_quarantine(
                str(quarantine["raw_payload"])
            )
            proof = _recovery_proof(
                original_projection,
                formal_projection,
                raw_time_paths=raw_time_paths,
                historical_v24=False,
            )
            if any(persisted_proof[field] != proof[field] for field in proof):
                raise sqlite3.IntegrityError("recovery_conflict")
            connection.commit()
            began = False
            return dict(quarantine)
        observation = connection.execute(
            "SELECT * FROM market_observations WHERE observation_id = ?",
            (recovered_observation_id,),
        ).fetchone()
        if observation is None:
            raise sqlite3.IntegrityError("recovery_observation_not_found")
        if not isinstance(recovery_payload, dict) or set(recovery_payload) != set(_OBSERVATION_FIELDS):
            raise sqlite3.IntegrityError("recovery_payload_shape_invalid")
        caller_projection = {field: recovery_payload[field] for field in _OBSERVATION_FIELDS}
        formal_projection = project_market_observation_for_recovery(observation)
        if _canonical_bytes(caller_projection) != _canonical_bytes(formal_projection):
            raise sqlite3.IntegrityError("recovery_payload_canonical_mismatch")
        original_projection = _projection_from_quarantine(str(quarantine["raw_payload"]))
        proof = _recovery_proof(
            original_projection,
            formal_projection,
            raw_time_paths=raw_time_paths,
            historical_v24=False,
        )
        cursor = connection.execute(
            """
            UPDATE data_governance_quarantine_records
            SET status = 'recovered',
                updated_at = ?,
                recovered_at = ?,
                recovery_payload_hash = ?,
                recovery_hash_algorithm_version = ?,
                recovered_observation_id = ?,
                recovery_detail = ?,
                verified_recovery_projection_hash = ?,
                verified_recovery_hash_algorithm_version = ?,
                verified_recovery_contract_version = ?,
                verified_original_projection_hash = ?,
                recovery_raw_before_hash = ?,
                recovery_raw_after_hash = ?,
                recovery_observed_at_before = ?,
                recovery_observed_at_after = ?,
                recovery_raw_time_changes = ?
            WHERE quarantine_id = ? AND status = 'quarantined'
            """,
            (
                timestamp,
                timestamp,
                proof["legacy_recovery_payload_hash"],
                HASH_ALGORITHM_VERSION,
                recovered_observation_id,
                recovery_detail,
                proof["verified_recovery_projection_hash"],
                proof["verified_recovery_hash_algorithm_version"],
                proof["verified_recovery_contract_version"],
                proof["verified_original_projection_hash"],
                proof["recovery_raw_before_hash"],
                proof["recovery_raw_after_hash"],
                proof["recovery_observed_at_before"],
                proof["recovery_observed_at_after"],
                proof["recovery_raw_time_changes"],
                quarantine_id,
            ),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("quarantine_record_not_recoverable")
        row = connection.execute(
            "SELECT * FROM data_governance_quarantine_records WHERE quarantine_id = ?",
            (quarantine_id,),
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("quarantine_record_not_found")
        observation = connection.execute(
            "SELECT * FROM market_observations WHERE observation_id = ?",
            (recovered_observation_id,),
        ).fetchone()
        if observation is None:
            raise sqlite3.IntegrityError("recovery_observation_not_found")
        formal_projection = project_market_observation_for_recovery(observation)
        if _canonical_bytes(caller_projection) != _canonical_bytes(formal_projection):
            raise sqlite3.IntegrityError("recovery_payload_canonical_mismatch")
        persisted_proof = _validate_recovery_audit(
            row,
            observation,
            historical_v24=False,
        )
        immutable_fact_fields = (
            "quarantine_id",
            "created_at",
            "received_at",
            "source_id",
            "payload_hash",
            "hash_algorithm_version",
            "failure_code",
            "failure_detail",
            "raw_payload",
        )
        if (
            any(row[field] != quarantine[field] for field in immutable_fact_fields)
            or row["recovered_observation_id"] != recovered_observation_id
            or row["recovery_detail"] != recovery_detail
            or row["recovered_at"] != timestamp
            or row["updated_at"] != timestamp
            or any(persisted_proof[field] != proof[field] for field in proof)
        ):
            raise sqlite3.IntegrityError("recovery_conflict")
        connection.commit()
        began = False
        return dict(row)
    except BaseException:
        if began:
            connection.rollback()
        raise


def _validate_json_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("governance payload numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("governance payload object keys must be strings")
            _validate_json_value(item)
        return
    raise TypeError(f"unsupported governance payload type: {type(value).__name__}")


def _validate_detail(detail: str) -> None:
    if not isinstance(detail, str) or len(detail) > 512:
        raise ValueError("governance detail must be a string of at most 512 characters")


def _source_policy(source: dict[str, Any]) -> dict[str, Any]:
    source_id = str(source.get("source_id") or "")
    tier = str(source.get("tier") or "")
    auth_type = str(source.get("auth_type") or "")
    category = str(source.get("category") or "")
    reference_only = (
        tier in {"C", "D"}
        or "proxy" in category
        or "prototype" in category
        or source_id in {"akshare_prototype", "yahoo_futures_daily_proxy"}
    )
    if auth_type == "vendor_license":
        authority_status = "authorized_vendor_unreconciled"
        authorization_status = "manual_authorized_input_required"
        comparison_mode = "manual_record_level"
    elif reference_only:
        authority_status = "reference_only"
        authorization_status = "registered"
        comparison_mode = "crosscheck_only"
    elif tier == "A":
        authority_status = "official_unreconciled"
        authorization_status = "registered"
        comparison_mode = "record_level_reconciliation_required"
    else:
        authority_status = "registered_secondary_unreconciled"
        authorization_status = "registered"
        comparison_mode = "crosscheck_only"
    return {
        **source,
        "authority_status": authority_status,
        "authorization_status": authorization_status,
        "comparison_mode": comparison_mode,
        "registry_status": "registered",
    }


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if str(row[0]) not in GOVERNANCE_TABLES
    ]


def _columns(connection: sqlite3.Connection, table: str) -> list[tuple[Any, ...]]:
    escaped = table.replace('"', '""')
    return list(connection.execute(f'PRAGMA table_info("{escaped}")').fetchall())


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _record_key_column(columns: list[tuple[Any, ...]]) -> str | None:
    primary = next((str(row[1]) for row in columns if int(row[5] or 0) > 0), None)
    if primary:
        return primary
    return next((str(row[1]) for row in columns if str(row[1]).endswith("_id")), None)


def audit_database(
    connection: sqlite3.Connection,
    *,
    registry: Iterable[dict[str, Any]],
    generated_at: str | None = None,
) -> dict[str, Any]:
    ensure_governance_schema(connection)
    now = generated_at or datetime.now(UTC).isoformat()
    policies = {
        str(item.get("source_id") or ""): _source_policy(dict(item))
        for item in registry
        if str(item.get("source_id") or "")
    }
    datasets: list[dict[str, Any]] = []
    observed_sources: set[str] = set()
    source_occurrences: dict[str, dict[str, int]] = {}
    for table in _tables(connection):
        columns = _columns(connection, table)
        names = {str(row[1]) for row in columns}
        row_count = int(connection.execute(f"SELECT COUNT(*) FROM {_quoted(table)}").fetchone()[0])
        source_rows: dict[str, int] = {}
        missing_source_rows = 0
        has_external_source = "source_id" in names and table in SOURCE_ATTRIBUTION_TABLES
        if has_external_source:
            rows = connection.execute(
                f"SELECT COALESCE(NULLIF(TRIM(source_id), ''), '__missing__'), COUNT(*) "
                f"FROM {_quoted(table)} GROUP BY 1"
            ).fetchall()
            source_rows = {str(row[0]): int(row[1]) for row in rows}
            missing_source_rows = source_rows.pop("__missing__", 0)
            observed_sources.update(source_rows)
            for source_id, count in source_rows.items():
                source_occurrences.setdefault(source_id, {})[table] = count
        registered_source_rows = sum(count for source, count in source_rows.items() if source in policies)
        unknown_source_rows = sum(count for source, count in source_rows.items() if source not in policies)
        key_column = _record_key_column(columns)
        reconciled_rows = matched_rows = mismatched_rows = 0
        if key_column and row_count:
            reconciliation = connection.execute(
                """
                SELECT
                  COUNT(DISTINCT record_key),
                  COUNT(DISTINCT CASE WHEN verdict='matched' THEN record_key END),
                  COUNT(DISTINCT CASE WHEN verdict='mismatched' THEN record_key END)
                FROM authoritative_reconciliations
                WHERE dataset_name = ?
                """,
                (table,),
            ).fetchone()
            reconciled_rows = int(reconciliation[0] or 0)
            matched_rows = int(reconciliation[1] or 0)
            mismatched_rows = int(reconciliation[2] or 0)
        coverage = round(reconciled_rows / row_count, 6) if row_count else 0.0
        if missing_source_rows or unknown_source_rows:
            trust_status = "blocked"
        elif not row_count:
            trust_status = "no_data"
        elif not has_external_source:
            trust_status = "derived_or_internal_not_source_data"
        elif any(policies[source]["authority_status"] == "reference_only" for source in source_rows):
            trust_status = "reference_only"
        else:
            trust_status = "registered_unreconciled"
        verification_status = (
            "fully_reconciled"
            if row_count and reconciled_rows >= row_count and mismatched_rows == 0
            else "partially_reconciled"
            if reconciled_rows
            else "not_reconciled"
        )
        datasets.append(
            {
                "table_name": table,
                "row_count": row_count,
                "has_source_id": has_external_source,
                "record_key_column": key_column,
                "source_ids": sorted(source_rows),
                "source_row_counts": dict(sorted(source_rows.items())),
                "unknown_source_ids": sorted(source for source in source_rows if source not in policies),
                "registered_source_rows": registered_source_rows,
                "unknown_source_rows": unknown_source_rows,
                "missing_source_rows": missing_source_rows,
                "reconciled_rows": reconciled_rows,
                "matched_rows": matched_rows,
                "mismatched_rows": mismatched_rows,
                "reconciliation_coverage": coverage,
                "trust_status": trust_status,
                "verification_status": verification_status,
            }
        )
    sources = dict(policies)
    for source_id in sorted(observed_sources - set(policies)):
        sources[source_id] = {
            "source_id": source_id,
            "source_name": source_id,
            "authority_status": "unregistered_unverified",
            "authorization_status": "unknown",
            "comparison_mode": "prohibited_until_registered",
            "registry_status": "unregistered",
            "tier": "",
            "auth_type": "",
            "url": "",
            "license_note": "",
        }
    summary = {
        "dataset_count": len(datasets),
        "row_count": sum(item["row_count"] for item in datasets),
        "datasets_with_unknown_sources": sum(bool(item["unknown_source_rows"]) for item in datasets),
        "datasets_with_missing_sources": sum(bool(item["missing_source_rows"]) for item in datasets),
        "fully_reconciled_datasets": sum(item["verification_status"] == "fully_reconciled" for item in datasets),
        "registered_sources": len(policies),
        "observed_unregistered_sources": len(observed_sources - set(policies)),
    }
    source_results = [
        {
            "source_id": source_id,
            "source_name": sources[source_id].get("source_name", source_id),
            "registry_status": sources[source_id]["registry_status"],
            "authority_status": sources[source_id]["authority_status"],
            "authorization_status": sources[source_id]["authorization_status"],
            "observed_rows": sum(source_occurrences.get(source_id, {}).values()),
            "datasets": dict(sorted(source_occurrences.get(source_id, {}).items())),
        }
        for source_id in sorted(observed_sources)
    ]
    return {
        "schema_version": 1,
        "run_id": f"data-governance-{uuid4().hex[:16]}",
        "generated_at": now,
        "status": "blocked"
        if summary["datasets_with_unknown_sources"] or summary["datasets_with_missing_sources"]
        else "review_required",
        "summary": summary,
        "datasets": datasets,
        "sources": sources,
        "source_results": source_results,
        "unconfigured_vendors": UNCONFIGURED_VENDORS,
        "limitations": [
            "来源已登记不等于数值已与权威源对账。",
            "只有 authoritative_reconciliations 中具有逐条证据的记录才计入已对账覆盖率。",
            "CCF 仅允许用户授权后的手工输入或导出；不得自动绕过登录或付费限制。",
            "卓创、隆众授权未知且未配置，禁止声明已完成权威对账。",
            "代理行情、公开估值、预测、LLM 判断和派生数据不得标记为权威原始数据。",
        ],
    }


def persist_audit(
    connection: sqlite3.Connection,
    report: dict[str, Any],
    *,
    json_path: str = "",
    markdown_path: str = "",
) -> None:
    ensure_governance_schema(connection)
    with connection:
        for source in report["sources"].values():
            connection.execute(
                """
                INSERT INTO data_source_governance (
                  source_id, source_name, authority_status, authorization_status,
                  comparison_mode, registry_status, tier, auth_type, canonical_url,
                  license_note, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                  source_name=excluded.source_name,
                  authority_status=excluded.authority_status,
                  authorization_status=excluded.authorization_status,
                  comparison_mode=excluded.comparison_mode,
                  registry_status=excluded.registry_status,
                  tier=excluded.tier,
                  auth_type=excluded.auth_type,
                  canonical_url=excluded.canonical_url,
                  license_note=excluded.license_note,
                  updated_at=excluded.updated_at
                """,
                (
                    source["source_id"],
                    source.get("source_name", ""),
                    source["authority_status"],
                    source["authorization_status"],
                    source["comparison_mode"],
                    source["registry_status"],
                    source.get("tier", ""),
                    source.get("auth_type", ""),
                    source.get("url", ""),
                    source.get("license_note", ""),
                    report["generated_at"],
                ),
            )
        connection.execute(
            """
            INSERT INTO data_governance_audit_runs
              (run_id, generated_at, status, summary_json, report_json_path, report_markdown_path)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                report["run_id"],
                report["generated_at"],
                report["status"],
                json.dumps(report["summary"], ensure_ascii=False),
                json_path,
                markdown_path,
            ),
        )
        connection.executemany(
            """
            INSERT INTO data_governance_dataset_results
              (result_id, run_id, table_name, row_count, trust_status, verification_status, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"dgr-{uuid4().hex}",
                    report["run_id"],
                    item["table_name"],
                    item["row_count"],
                    item["trust_status"],
                    item["verification_status"],
                    json.dumps(item, ensure_ascii=False),
                )
                for item in report["datasets"]
            ],
        )
        connection.executemany(
            """
            INSERT INTO data_governance_source_results
              (result_id, run_id, source_id, registry_status, authority_status,
               authorization_status, observed_rows, datasets_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"dgsr-{uuid4().hex}",
                    report["run_id"],
                    item["source_id"],
                    item["registry_status"],
                    item["authority_status"],
                    item["authorization_status"],
                    item["observed_rows"],
                    json.dumps(item["datasets"], ensure_ascii=False),
                )
                for item in report["source_results"]
            ],
        )


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# 全量数据来源与权威对账审计",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 状态：{report['status']}",
        f"- 数据表：{summary['dataset_count']}",
        f"- 已登记来源：{summary['registered_sources']}",
        f"- 实际出现但未登记来源：{summary['observed_unregistered_sources']}",
        f"- 完成全量逐条对账的数据表：{summary['fully_reconciled_datasets']}",
        "",
        "> 来源已登记不等于数值已与权威源对账。",
        "",
        "## 数据表",
        "",
        "| 数据表 | 行数 | 缺来源 | 未登记来源 | 对账覆盖率 | 可信状态 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for item in report["datasets"]:
        lines.append(
            f"| {item['table_name']} | {item['row_count']} | {item['missing_source_rows']} | "
            f"{item['unknown_source_rows']} | {item['reconciliation_coverage']:.2%} | {item['trust_status']} |"
        )
    lines.extend(["", "## 实际出现但未登记的来源", ""])
    unregistered = [item for item in report["source_results"] if item["registry_status"] == "unregistered"]
    if not unregistered:
        lines.append("- 无")
    else:
        lines.extend(
            [
                "| 来源 ID | 出现行数 | 出现的数据表（行数） | 权威状态 | 授权状态 |",
                "|---|---:|---|---|---|",
            ]
        )
        for item in unregistered:
            datasets = "；".join(f"{name}（{count}）" for name, count in item["datasets"].items())
            lines.append(
                f"| {item['source_id']} | {item['observed_rows']} | {datasets} | "
                f"{item['authority_status']} | {item['authorization_status']} |"
            )
    lines.extend(["", "## 未配置商业源", ""])
    for vendor in report["unconfigured_vendors"].values():
        lines.append(f"- {vendor['name']}：授权未知、未配置，禁止声明已对账。")
    lines.extend(["", "## 限制", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"
