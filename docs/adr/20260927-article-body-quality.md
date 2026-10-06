# Article body quality and first-visible time

Status: full-body storage and full model-input revision implemented; production unchanged.
Earlier capped candidates are superseded and must not be activated.

## Context

The approved article diagnostic and four public SunSirs pages show three independent failures:
`public_personal_reuse` was treated as restricted authentication; longer page/feed text could
win over the actual body; and extraction/storage clipped at 8,000/5,000 characters while
the grounded-summary prompt separately sliced input at 12,000. An unchanged full-source hash
could also hide the later arrival of a previously missing stored suffix.

## Decision

- Accept both existing public authentication classifications, with the same outbound host,
  access-barrier and extraction restrictions. Do not accept ambiguous license or login types.
- Prefer semantic article containers and verified publisher boundaries. Reject a clearly
  unrelated heading; mark unlocated, polluted, unreadable and truncated input explicitly.
- Per the user's subsequent request, remove extraction and storage character caps. Persist
  the entire acquired, cleaned article body in SQLite TEXT, with a hash of that exact text.
  This preserves extracted content, not raw HTML or unavailable publisher content.
- Following the user's request to remove and test the model limit, also retire the fixed
  12,000-character input gate. A complete longer body is eligible for the ordinary summary
  queue; fact extraction and source-based repair requests receive all cleaned source text.
  Legacy metadata from the unshipped length gate must not keep articles blocked.
- Provider context limits still apply. Do not silently truncate or repeat a smaller-prefix
  request after a provider rejection. Responses reporting a length cutoff, content filtering
  or interruption are incomplete even if their content parses as JSON; they cannot become a
  completed summary. Existing timeout/retry and factual gates remain in effect. No automatic
  chunking, new dependency, or production model-budget change is introduced.
- Retain actual old truncation flags and legacy-boundary uncertainty until real recovery;
  removing the cap does not retroactively supply missing text or validate old summaries.
- Bind stored text hash, truncation, extraction method and policy in source metadata; pass
  them into summary consumption. Do not silently rewrite queued source versions.
- An expanded stored body advances `content_visible_at` even when the old full-source hash
  is unchanged. Projection must notice that later visibility behind its cursor. Original
  discovery times and earlier immutable evidence revisions remain intact.
- Journal old article and summary/attempt state before a bounded repair, and verify the
  persisted body before reporting completion. Manual pilot runs with relevant writers paused.

## Compatibility and alternatives

No schema, dependency, forecast schedule/weights, retention or budget change. Existing event
status/label fields describe the new blocked case without a new API field. Unknown
publisher layouts stay pending; 394 legacy exact-boundary rows require further verification,
not blanket invalidation. Existing summaries are not bulk reset.

Trafilatura (Python, Apache-2.0) and Mozilla Readability (JavaScript, Apache-2.0) were checked
as reusable alternatives. For the verified defects, extending the existing parser avoids a
new dependency or cross-runtime boundary. General extraction quality remains a future corpus
evaluation question; this decision does not claim universal publisher coverage.

## Evidence and rollback

Earlier capped candidate evidence (not evidence for deploying the revised code):
`/path/to/project-context/article-body-quality-20260927/`.
Earlier full-storage-only revision evidence:
`/path/to/project-context/article-full-body-20260927/`.
Current removal of the model gate, long-input regressions and bounded live trial evidence:
`/path/to/project-context/article-unlimited-model-20260927/`.
This revision is included in the later reviewed summary-context candidate; it has not been deployed.
See `20260927-summary-context-recovery.md` and the matching task directory for the current
artifact and consumer scope. The earlier five-file capped candidate remains superseded.
Before production repair, require an integrity-checked online backup, preserve source/image
metadata and exact prior rows, and compare expected hashes. Restore only this batch when
hashes still match; do not overwrite concurrent later revisions or restore the whole database
as a routine rollback. No production records have been changed by this ADR's implementation.
