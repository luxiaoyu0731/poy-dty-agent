# Prospective shadow forecasting without production model replacement

Status: user confirmed the research direction on2026-09-26. Cloud deployment approved on2026-09-26 with08:00Shanghai schedule; an actual-image security gate remains unresolved.

## Decision

Use a separate private file ledger and optional pinned worker image. Production SQLite is read-only input, never the destination for shadow records. Do not modify production forecast APIs, registry, predictions, outcomes or dependencies.

- Upstream7 signal: naphtha/PX/PTA/MEG/POY/DTY; direct target classifier: DTY only. D1 is primary; next-publication is secondary and scored separately. The crude series is a driver, not an extra shadow target. Production still has seven products and1/7/30 horizons.
- Comparators are frozen robust drift, persistence, own7 momentum, past class frequency and fixed up/down. Do not discard underperforming products or pool duplicate horizon outcomes.
- Capture an export with the current OS time; no live CLI backdate/import option. Fit only on data visible at that cutoff. Persist the prediction payload with exclusive atomic linking and file/directory fsync before reading the issue timestamp. Outcomes must be after this durable timestamp. Crossing midnight or120seconds invalidates issue.
- A separate issue receipt binds the durable prediction. A crash between payload and receipt leaves a visible unissued orphan. Re-running the same date never recomputes or converts that orphan into a historical prediction.
- Outcomes are independently appended once, using frozen series identity and the first eligible arrival. Late correction never rewrites an existing outcome. Pending, unavailable, timeout, failed issue and model abstention remain distinct.
- Freeze code/runtime/protocol in a private manifest. Changes require a new experiment directory and future window. Existing filesystem owners can still alter files/clock; hashes are audit evidence, not an external timestamp authority or tamper-proof trust root.
- Record a durable attempt before any DB query. Missing completion, explicit failure or stale last cycle cannot be reported as healthy. Candidate availability is checked separately from baseline execution. Healthy collection never means proven predictive skill.
- Calibration uses only already-settled non-overlapping prospective episodes, with the existing sample/class/bucket gate. Report Brier/log loss/reliability only when probabilities actually exist. Never reinterpret softmax/vote scores as verified accuracy.

## Reuse and alternatives

Reuse the reviewed vintage exporter, models and settlement contracts. Existing `preregister_boss_holdout.py` has another domain/metric contract and does not provide this file-durability/concurrency protocol; leave it unchanged. [MLflow](https://github.com/mlflow/mlflow) is maintained, Apache-2.0 and Python compatible, but adds unnecessary dependency/operations overhead for this small isolated workbench; no new package/service is introduced.

## Deployment / rollback

An optional daily08:00Shanghai systemd timer launches a dedicated image pinned by ID, network disabled, source volume mounted read-only and only the shadow directory writable. It does not start/restart Compose application services. `Persistent=false` avoids replaying a missed scheduled time as if it were issued earlier; manual runs use their actual time and missing days remain in coverage.

Stop/disable that timer to stop collection, retaining all files. The application requires no rollback or DB restore. Backup/retention policy is unchanged; shadow files are retained. At the current roughly4.55MB export size, daily snapshots start around1.66GB/year before growth and small forecast/report files. Do not silently delete them.

## Validation limits

Local isolated issue→future arrival→settlement, repeat, crash, data identity, clock, stale export, tampering, readonly SQLite and Docker-command isolation are tested. A frozen actual export can exercise the models only in explicit offline preview mode; it is not prospective evidence. Target image build, readonly WAL access, installed timer and first real pre-outcome record require cloud activation checks; no stable accuracy claim until real results arrive.

A bounded read-only connection in the existing backend namespace keeps SQLite WAL locking sidecars available during export. It never changes business rows, runs a checkpoint or copies the database. The separate shadow worker still mounts the source volume read-only. Reader/source-volume mismatch or startup failure prevents execution.
