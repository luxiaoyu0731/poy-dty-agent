"""Read existing, hash-bound original text without changing projected summaries.

Only an already accepted full-text summary validates this fallback. A current
article cannot stand in for a different historical item revision. Matching
quotes are frozen in the new event claim; its evidence edge retains the source
item revision, URL, content hash and original visibility boundary.
"""

from __future__ import annotations

import hashlib
import json

from . import counterevidence as detector
from . import identity


def evidence_texts(connection, body: dict, cutoff_at: str) -> list[tuple[str, str]]:
    result = [("item_excerpt", body.get("excerpt") or "")]
    if body.get("projection_source_type") != "news_article" or not body.get("content_sha256"):
        return result
    from ..news import EVENT_SUMMARY_PROMPT_VERSION

    row = connection.execute(
        """SELECT n.title,n.raw_text,n.content_hash,n.raw,s.generated_at
        FROM news_articles n JOIN event_ai_summaries s ON s.article_id=n.article_id
        WHERE n.article_id=? AND n.content_hash=? AND s.source_hash=n.content_hash
          AND s.summary_status='completed' AND s.fact_summary_status='completed'
          AND s.quality_status='completed' AND s.input_quality='full_text'
          AND s.prompt_version=?""",
        (body["projection_source_id"], body["content_sha256"], EVENT_SUMMARY_PROMPT_VERSION),
    ).fetchone()
    if row is None:
        return result
    # Independently verify bytes; matching stale metadata hashes is insufficient.
    digest = hashlib.sha256(f"{row['title']}\n{row['raw_text']}".encode()).hexdigest()
    kind = "verified_original"
    if digest != body["content_sha256"]:
        # Legacy ingestion hashed the full article then stored only 5,000
        # characters. Keep its denial leads discoverable but never use this
        # unverifiable prefix to create an automatic factual conflict.
        try:
            metadata = json.loads(row["raw"]).get("source_content", {})
            proof_at = identity.parse_iso(metadata.get("stored_text_verified_at", ""))
            prefix_verified = (
                proof_at.tzinfo is not None
                and proof_at <= identity.parse_iso(cutoff_at)
                and metadata.get("stored_text_content_hash") == body["content_sha256"]
                and metadata.get("stored_text_sha256") == hashlib.sha256(row["raw_text"].encode()).hexdigest()
            )
        except (ValueError, TypeError, AttributeError):
            prefix_verified = False
        try:
            metadata = json.loads(row["raw"]).get("source_content", {})
            truncated = len(row["raw_text"]) == 5000 and (
                metadata.get("stored_text_truncated") is True or int(metadata.get("text_chars", 0)) > 5000
            )
        except (ValueError, TypeError, AttributeError):
            truncated = False
        if not prefix_verified and not truncated:
            return result
        if not prefix_verified:
            kind = "legacy_truncated_original"
    try:
        cutoff = identity.parse_iso(cutoff_at)
        generated = identity.parse_iso(row["generated_at"])
        if generated.tzinfo is None or generated > cutoff:
            return result
    except (ValueError, TypeError):
        return result
    from ..event_summary_quality import clean_event_source_text

    original = clean_event_source_text(row["raw_text"])[: detector.MAX_EXCERPT_CHARS]
    try:
        meta = json.loads(row["raw"]).get("source_content", {})
        if len(row["raw_text"]) == 5000 and (
            meta.get("stored_text_truncated") is True or int(meta.get("text_chars", 0)) > 5000
        ):
            parts = detector.sentences(original)
            if parts:
                # A clipped last sentence might omit a negation/qualification.
                original = original[: original.rfind(parts[-1])].rstrip()
    except (ValueError, TypeError, AttributeError):
        pass
    if original and original != result[0][1]:
        result.insert(0, (kind, original))
    return result
