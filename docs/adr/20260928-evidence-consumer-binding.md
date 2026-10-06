# Evidence consumer binding
Status: accepted for implementation by user request “补齐所有接口（不止全球雷达这一处）”.

All active business consumers share the dossier projector and endpoint. Product-wide
context, event revision, frozen report, frozen answer, and issued batch are distinct
scopes. Scope may narrow evidence, never increase its trust. Event direct membership
requires exact sources and cutoff; answer anchors additionally match complete quotes
in the material actually retrieved. History is chosen by pre-outcome conditions.
Four mechanism/history families do not replace factual-denial links or RAG entailment.

Report and answer generation freeze their own bounded projection in existing records;
GET never creates one for a historical record. No new DB schema, service or dependency.
Historical absence and read failure remain visible. Dossier numerical features do not
bypass the sole forecast's existing model promotion gate. Generated prose must retain
citation quality checks. Additional capture latency is measured in isolated/real probes.

Reuse search: https://github.com/inflexa-ai/tsprov (Apache-2.0, TypeScript PROV serialization,
active commits/security policy, adds Luxon) and https://github.com/Inarus/provgraf
(Postgres proof of concept) do not implement our source/version/time/forecast contracts.
No security claim was inferred merely from project activity. Reuse local pure builder
and projector: lower integration cost, existing tests, no dependency migration.

Rollback: restore reviewed previous image and frontend; new optional report/context
metadata is additive and may remain. Never revert issued history or backdate a capture.
