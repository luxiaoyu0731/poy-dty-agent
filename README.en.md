<p align="center"><img src="docs/media/project-hero.png" alt="POY/DTY — Evidence-led Multi-Agent Research" width="100%" /></p>

# POY/DTY · Evidence-led Multi-Agent Research

A personal research workbench for the polyester supply chain: collect public information, retrieve relevant evidence, coordinate specialist agents, issue directional forecasts, and review outcomes.

[Live demo](https://app.kaipingrc.com/) · [中文](README.md) · [Local setup](docs/getting-started.md) · [Code review](docs/code-quality-review.md)

## What it connects

The system covers crude oil, naphtha, PX, PTA, MEG, POY and DTY across 1-, 7- and 30-day horizons: 21 forecast cells per business day.

- Source collection with deduplication, allowlisted publishers, original-text retrieval and bounded PDF parsing.
- Hybrid retrieval using SQLite FTS5 and FastEmbed multilingual vectors, with availability-time and product filters.
- Specialist agents for political analysis, historical analogies, product reasoning and skeptical review; structured artifacts, citation checks and request quotas.
- Frozen input context, traceable case memory and retrospective lessons.
- Rule-based decisions, an append-only forecast ledger, settlement and review.
- Evidence browsing, reports, industrial intelligence and read-only research assistance.

<img src="docs/media/workflow-live.png" alt="Actual workflow interface" width="100%" />

## Context, evidence and memory

<img src="docs/media/context-memory.png" alt="Context / Evidence / Memory conceptual illustration" width="100%" />

Keep current inputs separate from retrievable source material and long-term lessons. Semantic similarity alone does not establish directional support. Module integration, evidence coverage and predictive improvement require different forms of validation; this repository does not claim a universal backtest uplift.

## Stack and setup

React / TypeScript / Ant Design / React Flow / MapLibre on the frontend; FastAPI / SQLite / FTS5 / FastEmbed on the backend. Requires Node.js 20+, npm, Python 3.11+ and uv.

```bash
npm ci
npm run check
uv sync --project server
```

Follow the [local setup guide](docs/getting-started.md) before starting services or tests. Production databases, credentials, customer documents and historical Git objects are excluded. Real model requests require your own provider key and incur provider costs.

## Engineering and contribution

See [maintainability findings](docs/code-quality-review.md), [contribution guidance](CONTRIBUTING.md), [security reporting](SECURITY.md) and [distribution scope](docs/public-distribution.md). Screenshots are captured from the live interface; editorial illustrations are generated and identified separately.

Code is licensed under [Apache-2.0](LICENSE). Third-party dependencies and external data retain their original licensing conditions. Forecasts are research outputs, not a guarantee of financial returns.
