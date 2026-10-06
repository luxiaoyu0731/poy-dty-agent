# Contributing

Small, focused changes are easier to verify. Start from an issue describing the behavior, affected inputs and expected result.

1. Use npm for the frontend and uv with Python 3.11+ for the backend.
2. Preserve source dates, citations and issued forecast records. Do not invent evidence to fill a page.
3. Document API changes in `docs/openapi.yaml` and add positive and negative behavior tests.
4. Run `npm run check`, affected E2E tests and isolated backend tests. See [local setup](docs/getting-started.md).
5. Keep credentials, databases, provider caches, private documents and generated test outputs out of commits.
6. Explain what changed, why, how it was checked and any remaining limitations in the pull request.

Do not bypass logins, CAPTCHAs or paywalls when adding a source. Source discovery is not permission to redistribute its content.

Contributions are provided under Apache-2.0 unless explicitly agreed otherwise. See [LICENSE](LICENSE).
