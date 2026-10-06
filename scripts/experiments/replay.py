"""Isolated replay experiments over the immutable seven-product ledger.

Runs read-only against the production database (or a copy). No writes to the
ledger, no model invocations, no scheduler interaction. Every candidate is a
pure re-scoring of already-settled cells: the issued point forecast, neutral
band and actual outcome are immutable ledger facts, so candidate direction
rules are evaluated on exactly the samples the production OOS evaluation
used — no waiting for natural days.

Usage (inside the backend container):
  python scripts/experiments/replay.py summary              # per-cell sample map
  python scripts/experiments/replay.py rules --target crude --horizon 30
  python scripts/experiments/replay.py rules --all          # every cell with n>=5
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict


def load_samples(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """
        SELECT c.target, c.horizon_days, c.point_forecast, c.neutral_band_pct,
               c.predicted_direction, c.origin_observed_at, c.origin_visible_at,
               c.label_series_id,
               o.outcome_id, o.settled_at, o.actual_value, o.absolute_error,
               o.predicted_direction AS outcome_predicted_direction,
               o.actual_direction, o.direction_hit, o.outcome_payload
        FROM seven_product_forecast_outcomes o
        JOIN seven_product_forecast_cells c ON c.cell_id = o.cell_id
        ORDER BY c.target, c.horizon_days, o.settled_at
        """
    ).fetchall()
    # 原点观测值：按 label_series_id（=source_capture_revisions.semantic_series_id）
    # + origin_observed_at 精确取发行时点该序列的值。value 在 canonical_payload 里
    # （settle 或 value 字段，与采集器字段映射一致）。
    series_ids = {row["label_series_id"] for row in rows if row["label_series_id"]}
    origin_by_series_day: dict[tuple, float] = {}
    for series_id in series_ids:
        for observed_at, payload_json in connection.execute(
            "SELECT observed_at, canonical_payload FROM source_capture_revisions WHERE semantic_series_id=?",
            (series_id,),
        ):
            try:
                payload = json.loads(payload_json)
                # 字段映射与采集器一致：郑商所=settle/close，现货=last/value
                value = payload.get("settle") or payload.get("close") or payload.get("value") or payload.get("last")
            except Exception:
                continue
            if value is not None:
                origin_by_series_day[(series_id, observed_at)] = float(value)
    samples = []
    for row in rows:
        sample = dict(row)
        origin_value = origin_by_series_day.get(
            (row["label_series_id"], row["origin_observed_at"])
        )
        # 预测变动% 由账本不可变字段直接计算：发行点位相对原点观测。
        if origin_value not in (None, 0) and row["point_forecast"] is not None:
            sample["predicted_change_pct"] = (row["point_forecast"] - origin_value) / origin_value
        else:
            sample["predicted_change_pct"] = None
        sample["origin_value"] = origin_value
        samples.append(sample)
    return samples


def hit_rate(pairs: list[tuple[str | None, str | None]]) -> tuple[float, int]:
    valid = [
        (p, a)
        for p, a in pairs
        if p in ("up", "down", "neutral") and a in ("up", "down", "neutral")
    ]
    if not valid:
        return float("nan"), 0
    hits = sum(1 for p, a in valid if p == a)
    return hits / len(valid), len(valid)


def bootstrap_ci(pairs: list[tuple[str | None, str | None]], draws: int = 1000) -> tuple[float, float]:
    import random

    valid = [(p, a) for p, a in pairs if p is not None and a is not None]
    if len(valid) < 5:
        return (float("nan"), float("nan"))
    rng = random.Random(20260930)
    rates = []
    for _ in range(draws):
        sample = [valid[rng.randrange(len(valid))] for _ in range(len(valid))]
        rates.append(sum(1 for p, a in sample if p == a) / len(sample))
    rates.sort()
    return (rates[int(0.025 * len(rates))], rates[int(0.975 * len(rates))])


def mae_improvement(samples: list[dict]) -> tuple[float | None, int]:
    """Error improvement vs the carried-flat naive baseline within each cell."""
    by_cell: dict[tuple, list[dict]] = defaultdict(list)
    for s in samples:
        by_cell[(s["target"], s["horizon_days"])].append(s)
    paired = []
    for _, group in by_cell.items():
        group.sort(key=lambda s: s["settled_at"])
        for index, sample in enumerate(group):
            if index == 0:
                continue
            previous = group[index - 1]["actual_value"]
            if previous is None or sample["actual_value"] is None or sample["absolute_error"] is None:
                continue
            naive_error = abs(sample["actual_value"] - previous)
            paired.append((sample["absolute_error"], naive_error))
    if not paired:
        return None, 0
    model_mae = statistics.fmean(e for e, _ in paired)
    naive_mae = statistics.fmean(e for _, e in paired)
    if naive_mae == 0:
        return None, len(paired)
    return (naive_mae - model_mae) / naive_mae, len(paired)


def cmd_summary(connection: sqlite3.Connection, _args: argparse.Namespace) -> None:
    samples = load_samples(connection)
    by_cell: dict[tuple, list[dict]] = defaultdict(list)
    for s in samples:
        by_cell[(s["target"], s["horizon_days"])].append(s)
    print(f"{'品种':<8} {'天':>3} {'结算':>4} {'方向命中':>10} {'95%CI':>14} {'误差提升(配对n)':>12}")
    for (target, horizon), group in sorted(by_cell.items()):
        pairs = [(s["outcome_predicted_direction"], s["actual_direction"]) for s in group]
        rate, n = hit_rate(pairs)
        low, high = bootstrap_ci(pairs)
        improvement, paired_n = mae_improvement(group)
        rate_s = f"{rate*100:.0f}%(n={n})" if n else "—"
        ci_s = f"[{low*100:.0f},{high*100:.0f}]" if n >= 5 else "—"
        imp_s = f"{improvement*100:+.0f}%({paired_n})" if improvement is not None else "—"
        print(f"{target:<8} {horizon:>3} {len(group):>4} {rate_s:>10} {ci_s:>14} {imp_s:>12}")


def cmd_rules(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    samples = load_samples(connection)
    selected: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        if args.target and s["target"] != args.target:
            continue
        if args.horizon and s["horizon_days"] != args.horizon:
            continue
        selected[f"{s['target']} {s['horizon_days']}d"].append(s)
    if args.min_n:
        selected = {k: v for k, v in selected.items() if len(v) >= args.min_n}

    print(f"{'样本组':<12} {'n':>4} {'基线命中':>9} {'中性带断点扫描（|chg|<=band 判中性，否则取符号）'}")
    for key, group in selected.items():
        base_pairs = [(s["outcome_predicted_direction"], s["actual_direction"]) for s in group]
        base_rate, _ = hit_rate(base_pairs)
        low, high = bootstrap_ci(base_pairs)
        scan = []
        for band_bp in (25, 50, 100, 150, 200, 300, 500):
            pairs = []
            for s in group:
                change = s.get("predicted_change_pct")
                actual = s["actual_direction"]
                if change is None:
                    pairs.append((None, actual))
                elif abs(change) <= band_bp / 10000:
                    pairs.append(("neutral", actual))
                else:
                    pairs.append(("up" if change > 0 else "down", actual))
            rate, n = hit_rate(pairs)
            scan.append(f"±{band_bp/100:.2f}%:{rate*100:.0f}%")
        print(
            f"{key:<12} {len(group):>4} {base_rate*100:>8.0f}%  {'  '.join(scan)}"
            f"   CI[{low*100:.0f},{high*100:.0f}]"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="/data/agent.db")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("summary", help="per-cell sample map with bootstrap CI")
    rules = sub.add_parser("rules", help="direction-rule comparison with neutral-band sweep")
    rules.add_argument("--target")
    rules.add_argument("--horizon", type=int)
    rules.add_argument("--all", action="store_true", help="every cell group with n>=min-n")
    rules.add_argument("--min-n", type=int, default=5)
    args = parser.parse_args()

    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        if args.command == "summary":
            cmd_summary(connection, args)
        elif args.command == "rules":
            cmd_rules(connection, args)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
