import type { EvidenceDossier, EvidenceSemanticReview } from "./types";

/** Render server-validated conditional receipts consistently in every view.
 * Source/quote/clock validation belongs to the server; this does not extract
 * facts again or grant any review evidence votes.
 */
export function conditionalReviews(dossier: EvidenceDossier | null | undefined): EvidenceSemanticReview[] {
  if (!dossier || dossier.view !== "current") return [];
  const seen = new Set<string>();
  return (dossier.semantic_reviews ?? []).filter(review => {
    if (review.target !== dossier.target || review.counts_as_evidence !== false ||
        review.assessment !== "ai_semantic_review_not_verified_outcome" ||
        !review.quote || !review.rationale || !review.conditions.length || seen.has(review.review_id)) return false;
    seen.add(review.review_id);
    return true;
  });
}
