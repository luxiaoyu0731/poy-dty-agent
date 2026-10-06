# Explicit event counterevidence

Status: approved and activated on 2026-09-27 (Asia/Shanghai), release `20260926T160509Z-36396a1666090e0f`. Production read-only trial, backup integrity, exact artifacts and public Chrome checks passed; the scoped data-operation approval is retained in the external task pack.

The event detail previously exposed an always-empty counterevidence field. The
independent daily counter scan targets different judgement/factor identities and
cannot be transplanted into an arbitrary industrial event.

Use the existing verified item excerpts, event revisions, Claim schema,
Inference.counterevidence_claim_ids and counterevidence evidence-edge type.
The initial detector recognizes an explicit Chinese/English denial of the same
fully dated affirmative proposition. It keeps both quoted statements and the
denial's source link, marks the relevant direction unclear, and caps its
observation confidence at 0.49 pending verification. This is source disagreement,
not a determination of truth or a calibrated probability. Different dates,
entities, units, qualifications, speculative/negative originals and later
recovery are rejected. Denials require an A/B source and an available verified
excerpt. Reposts within one origin group do not add counterclaims.

The live refresh performs one bounded scan after projection/clustering, including
cycles with no new clusters: latest 200 event heads and 500 item heads in seven
days, at most 12 counterclaims per event. Counts and truncation are in its JSON
result. Existing associated sources are explicitly rechecked even if they fall
outside the candidate limit; a limited scan alone cannot erase an association.
All inputs must have been visible and created by the cutoff. Source withdrawal
removes only the current derived association via a new event revision. Old
revisions and frozen reports/forecasts remain unchanged. No new model calls,
network retrieval, schema migration or dependency is added.

Identity preparation excludes derived link IDs for both facts and counterclaims
(the links depend on the allocated revision ID). Complete payload hashes still
include them. Associations only append when their content changes; all edges
are rewired to the new revision atomically. A historical cutoff cannot overwrite
a more recent event head. On a failure the whole revision and its edges roll back.

This intentionally favors precision over recall. It does not implement general
semantic contradiction, cross-language paraphrase, numerical conflict resolution
or open-world counterevidence retrieval. No match means no qualifying explicit
denial was linked within the scanned set, not proof that no counterevidence exists.

Reuse considered: https://github.com/kadubon/cgt-marker (Apache-2.0; small project,
two commits observed during review). It retains structured conflicts/provenance
but provides no natural-language extraction. Adding its state layer would
duplicate the existing immutable ledger, increase integration/security review
cost, and would not solve extraction. No third-party code or dependency copied.

Rollback uses the preceding image and scoped source files. Already appended
counterclaim revisions remain valid for the previous reader, including evidence
links; there is no database downgrade or removal of historical records.
