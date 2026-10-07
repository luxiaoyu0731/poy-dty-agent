# POY/DTY · Multi-Agent Supply Chain Research

Collect public news and prices, retrieve traceable evidence, coordinate specialist agents, and review issued forecasts. Seven products, three horizons: 21 directional forecast cells per business day.

[Live demo](https://app.kaipingrc.com/) · [中文](README.md) · [Local setup](docs/getting-started.md)

<img src="docs/media/workflow-live.png" alt="Actual Agent workflow interface" width="100%" />

## Features

- Source collection, original-text retrieval, bounded PDF parsing and provenance tracking.
- Hybrid FTS5 / FastEmbed retrieval with product and availability-time filters.
- Specialist reasoning, historical analogies and skeptical review via structured artifacts.
- Rule-based decisions, frozen ledgers, settlement and retrospective memory.

## Run locally

Node.js 20+, Python 3.11+ and uv are required.

```sh
git clone https://github.com/luxiaoyu0731/poy-dty-agent.git
cd poy-dty-agent
npm ci
uv sync --project server
```

Follow the [setup guide](docs/getting-started.md) to start isolated services. Production data is not distributed; provider keys and fees are your responsibility. Integration is distinct from measured predictive improvement.

[Architecture](docs/architecture.md) · [OpenAPI](docs/openapi.yaml) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

[Code review](docs/code-quality-review.md) and [validation status](docs/github-release-evidence.md) record remaining engineering issues. Screenshots show a captured live state; [asset credits](docs/media/README.md) distinguish generated illustrations.

[Apache-2.0](LICENSE). External data and dependencies retain their own licenses. Forecasts are research outputs, not financial guarantees.
