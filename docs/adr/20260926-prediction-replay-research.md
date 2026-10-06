# Prediction quality, vintage replay and comparable direction research

Status: accepted for isolated research implementation by the user's 2026-09-26 instruction. Not approval to replace production forecast contracts or promote a model.

## Decision

Build a pure offline path before changing the live immutable forecast ledger. The path consumes a bounded read-only export of **all** retained revisions for each current label identity, with original timestamps and evidence hashes. No new dependency, provider, database migration or automatic promotion.

1. Validate identity, unit, finite positive value, provenance and capture integrity. Resolve each day's latest revision available at the historical cutoff, **then** apply causal suspicious-jump quarantine. Do not delete raw records, interpolate labels, or use future neighbours. A suspicious most-recent price blocks issuing a research prediction. A persistent new price level remains unresolved rather than silently disappearing into an old-level forecast.
2. Availability is conservatively the maximum of recorded visibility, capture and ledger creation times. Keep all three. Replay daily at 09:30 Asia/Shanghai; retain each selected input hash. This is retrospective shadow execution, not a claim that these models or the current v5 label contract operated then.
3. Compare two explicit research families: issue-day plus 1/7/30 calendar days, and 1/5/20 subsequent published observation dates **strictly after** the issue business date. Next-publication targets describe observation steps, not guaranteed elapsed days. Calendar targets settle on the first eligible quote actually received, observed no more than four days after target, and received within seven days after target. Publication-step tasks have a conservative maximum wait of `4 * steps + 7` days. Missing inputs/outcomes remain visible in coverage denominators.
4. Settlement freezes the first qualifying arrival cohort, not a final revised value selected with hindsight. Later corrections are reported separately. The calibration learner can use an outcome only after that arrival. Neutral truth uses existing per-product fixed floors, identical for every model and contract; this is a frozen research convention, not an empirically optimized threshold or adopted business contract. Neutral is not abstention.
5. Fixed model candidates: persistence, existing robust drift, calendar median drift, damped Theil–Sen (20 points, damping 0.5). Retain a smoothed past-class-frequency baseline. No hyperparameter sweep or winner promotion on these already-inspected dates.
6. Estimate directional probabilities from past matured non-overlapping episodes using a smoothed confusion matrix; minimum 30 episodes, at least five of each truth class, and at least ten for the predicted class. Otherwise no calibrated probability is published. Evaluate probabilities prequentially, never on their fitting outcomes. Report raw accuracy, class recall, balanced accuracy, Brier score, log loss, calibration error, and abstention risk/coverage. These empirical estimates do not guarantee individual correctness or conformal coverage under drift.

## Alternatives and reuse

- Next-quote/irregular-publication prediction aligns sparse quotes with the source's actual cadence; it must remain distinct from a daily future-return forecast and delayed-price nowcasting.
- Calendar horizons preserve intuitive short/medium/long planning views. Their reliability varies with source cadence and observed maturity, not merely the labels 1/7/30.
- Event-triggered threshold/barrier and supply-chain conditional scenarios are useful complementary tasks, but require independently frozen event definitions, complete paths and time-available structured inputs. Current price replay cannot validate them or establish a causal market impact.
- [StatsForecast](https://github.com/Nixtla/statsforecast) (Apache-2.0) offers Python statistical baselines and rolling validation. [sktime](https://github.com/sktime/sktime) (BSD-3-Clause) offers a broader forecasting interface. Both add integration/version/supply-chain work; this phase reuses existing NumPy predictors instead.
- [Chronos](https://github.com/amazon-science/chronos-forecasting) is an actively maintained pretrained time-series model family. Its dependency/runtime/weights and pretraining-data overlap must be assessed before a frozen out-of-sample benchmark. It cannot repair incorrect labels or missing historical vintages. No model weights downloaded here.
- [Rolling-origin evaluation](https://otexts.com/fpp3/tscv.html), [oil forecasting and information availability](https://www.federalreserve.gov/pubs/ifdp/2011/1022/ifdp1022.htm), [calibrated selective classification](https://arxiv.org/abs/2208.12084) and [adaptive conformal inference](https://arxiv.org/abs/2106.00170) inform the design. Their published results are not evidence of commodity prediction gains in this project.

## Consequences and rollback

No live caller imports this research path; public API, registry and historical ledger keep their contracts. Remove the research entry points to roll back; no data restore is required. Existing evaluation-v4 fixes from the preceding audit remain separate. Production adoption requires reviewing target semantics, actual coverage, temporal evidence and a release gate. Large exports and evidence remain repository-external.
