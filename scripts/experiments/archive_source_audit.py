"""Offline integrity audit of manually reviewed historical body receipts.

Hashes do not authenticate a publisher or a timestamp. This audits the cached
acquisition receipts and their exact original bodies; it cannot grant mechanism
verification, posterior qualification, or voting rights.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from scripts.experiments.cached_replay_port import digest

REVIEW_KEYS = (
    "http200",
    "exact_snapshot_not_current_redirect",
    "source_url_and_event_identity_matched",
    "article_body_not_navigation",
    "no_direction_or_vote_inferred",
)


def _clock(value):
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("aware_source_clock_required")
    return at.astimezone(timezone.utc)


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


class _MetaCharset(HTMLParser):
    def __init__(self):
        super().__init__()
        self.encodings = set()

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        values = dict(attrs)
        if values.get("charset"):
            self.encodings.add(values["charset"].strip().lower())
        if (values.get("http-equiv") or "").lower() == "content-type":
            match = re.search(
                r"\bcharset\s*=\s*([a-z0-9._-]+)", values.get("content") or "", re.I
            )
            if match:
                self.encodings.add(match[1].lower())


def decode_original_html(raw: bytes, document: dict) -> str:
    """Strict decoding; legacy opt-in must match the actual publisher markup.

    Existing UTF-8 receipts retain their interpretation. Never guess a codec,
    replace undecodable bytes or transcode the sealed acquisition artifact.
    """
    encoding = document.get("source_encoding", "utf-8")
    if encoding == "utf-8":
        return raw.decode("utf-8", errors="strict")
    if encoding != "iso-8859-1":
        raise ValueError("unsupported_source_encoding")
    meta = _MetaCharset()
    meta.feed(raw[:16384].decode("latin-1", errors="strict"))
    if meta.encodings != {encoding}:
        raise ValueError("source_encoding_not_bound_to_markup")
    return raw.decode(encoding, errors="strict")


def validate_document(document: dict, *, artifact_root: Path) -> str:
    sealed = {key: value for key, value in document.items() if key != "receipt_sha256"}
    if document.get("receipt_sha256") != digest(sealed):
        raise ValueError("source_receipt_hash_mismatch")
    if (
        document.get("schema_version") != "strict-pit-source-document.v1"
        or document.get("qualification_scope")
        != "original_body_available_before_issuance_only"
        or document.get("counts_as_evidence") is not False
        or document.get("posterior_or_price_outcome_verified") is not False
        or any(
            (document.get("review") or {}).get(key) is not True for key in REVIEW_KEYS
        )
    ):
        raise ValueError("reviewed_availability_only_receipt_required")
    original = document["original_url"]
    parsed = urlsplit(original)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("invalid_original_source_url")
    archived = urlsplit(document["archived_url"])
    stamp = document["snapshot_timestamp"]
    if not re.fullmatch(r"\d{14}", stamp):
        raise ValueError("invalid_snapshot_timestamp")
    prefix = f"/web/{stamp}/"
    # Query characters belong to the embedded original URL, not the archive.
    recorded_original = document["archived_url"].partition(prefix)[2]
    if (
        archived.scheme != "https"
        or archived.netloc != "web.archive.org"
        or not archived.path.startswith(prefix)
        or recorded_original != original
    ):
        raise ValueError("archive_original_identity_mismatch")
    witnessed = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    if (
        _clock(document["available_at_upper_bound"]) != witnessed
        or witnessed > _clock(document["cutoff_checked"])
        or _clock(document["acquired_at"]) < witnessed
    ):
        raise ValueError("source_availability_clock_mismatch")
    root = artifact_root.resolve(strict=True)
    raw_path = Path(document["raw_body_path"])
    if raw_path.is_symlink() or raw_path.resolve(strict=True).parent != root:
        raise ValueError("source_body_outside_artifact_root")
    raw = raw_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != document["raw_content_sha256"]:
        raise ValueError("raw_source_body_hash_mismatch")
    body = document["body_text"]
    if (
        not isinstance(body, str)
        or not body.strip()
        or hashlib.sha256(body.encode()).hexdigest() != document["body_text_sha256"]
    ):
        raise ValueError("extracted_body_hash_mismatch")
    parser = _VisibleText()
    parser.feed(decode_original_html(raw, document))
    text = re.sub(r"\s+", "", "".join(parser.parts))
    cursor = 0
    for line in body.splitlines():
        quote = re.sub(r"\s+", "", line)
        if not quote:
            continue
        found = text.find(quote, cursor)
        if found < 0:
            raise ValueError("body_not_in_original_source_sequence")
        cursor = found + len(quote)
    ids = document.get("event_revision_ids")
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(identity, str) or not identity for identity in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("unique_source_revision_bindings_required")
    return document["receipt_sha256"]


def audit_cohort_sources(
    *,
    documents: list[dict],
    inventory: list[dict],
    coverage: list[dict],
    artifact_root: Path,
    cohort_receipt: dict | None = None,
) -> dict:
    by_revision = {row["event_revision_id"]: row for row in inventory}
    if len(by_revision) != len(inventory):
        raise ValueError("ambiguous_event_inventory")
    bindings = {}
    seen = set()
    for document in documents:
        validate_document(document, artifact_root=artifact_root)
        identity = (document["original_url"], document["snapshot_timestamp"])
        if identity in seen:
            raise ValueError("duplicate_archive_document")
        seen.add(identity)
        for revision in document["event_revision_ids"]:
            original = by_revision.get(revision)
            if (
                not original
                or original.get("canonical_url") != document["original_url"]
            ):
                raise ValueError("source_candidate_binding_mismatch")
            bindings.setdefault(revision, []).append(document)
    rows = []
    days = set()
    for cohort_day in coverage:
        day = cohort_day["business_date"]
        if day in days:
            raise ValueError("duplicate_cohort_day")
        days.add(day)
        at = _clock(cohort_day["as_of_time"])
        ids = cohort_day["event_revision_ids"]
        if len(ids) != len(set(ids)) or any(
            identity not in by_revision for identity in ids
        ):
            raise ValueError("cohort_revision_not_in_inventory")
        proved = {
            identity
            for identity in ids
            if any(
                _clock(doc["available_at_upper_bound"]) <= at
                for doc in bindings.get(identity, [])
            )
        }
        rows.append(
            {
                "business_date": day,
                "revision_count": len(ids),
                "body_proved_revision_ids": sorted(proved),
                "missing_revision_ids": sorted(set(ids) - proved),
                "availability_complete": len(proved) == len(ids),
            }
        )
    cohort_bound = False
    if cohort_receipt is not None:
        from app.replay_cohort import validate_cohort

        selected = validate_cohort(
            cohort_receipt,
            data_sha256=cohort_receipt["data_sha256"],
            sample_sha256=cohort_receipt["sample_sha256"],
        )
        if days != selected:
            raise ValueError("source_audit_frozen_cohort_dates_mismatch")
        pool = {row["business_date"]: row for row in cohort_receipt["eligible_pool"]}
        for row in coverage:
            frozen_ids = {
                event["event_revision_id"]
                for event in pool[row["business_date"]]["events"]
            }
            if set(row["event_revision_ids"]) != frozen_ids:
                raise ValueError("source_audit_frozen_candidate_ids_mismatch")
            expected_clock = _clock(row["business_date"] + "T08:00:00+08:00")
            if _clock(row["as_of_time"]) != expected_clock:
                raise ValueError("source_audit_frozen_issuance_clock_mismatch")
        cohort_bound = True
    result = {
        "schema_version": "cached-archive-source-audit.v1",
        "scope": "cached_original_body_integrity_and_availability_only",
        "document_receipts": sorted(d["receipt_sha256"] for d in documents),
        "rows": rows,
        "complete_days": sum(row["availability_complete"] for row in rows),
        "cohort_binding_verified": cohort_bound,
        "cohort_receipt_sha256": cohort_receipt.get("receipt_sha256")
        if cohort_bound
        else None,
        "source_availability_complete": cohort_bound
        and bool(rows)
        and all(row["availability_complete"] for row in rows),
        "mechanism_or_posterior_certified": False,
        "paid_llm_calls": 0,
    }
    return {**result, "audit_sha256": digest(result)}
