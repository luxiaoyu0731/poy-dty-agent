# Polyester Chain Import

Use `polyester_chain_observations.csv` for POY/DTY/PX/PTA/MEG/NAPHTHA manual or Computer Use assisted observations that are not already normalized as forecast price points.

- Use `forecast_price_points.csv` for price sequences used by `/forecast/backtest`, `/forecast/signals`, and CCF/peer price replay.
- Use `polyester_chain_observations.csv` for operating rate, inventory, profit, spread, processing fee, device status, and other industry metrics.
- Preserve `source_id`, `source_url`, `captured_at`, original unit, and notes.
- For authorized CCF data, prefer `server/scripts/import_ccf_authorized_csvs.py` after dry-run validation.
- Do not bypass login, CAPTCHA, paywall, license, robots.txt, or anti-bot controls.
