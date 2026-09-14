"""Run the L3 closed-loop benchmark — EVALUATION.md §7.

Produces a reproducible report (report to stdout + optional JSON) over the
B0/B1/B2 baselines and the full BookMind system, using the deterministic
learner simulator. Fully offline; no live model needed.

Usage::

    python scripts/run_benchmark.py
    python scripts/run_benchmark.py --budget 80 --json eval/results/run.json

Run from the BookMind project root.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

from bookmind.evaluation.benchmark import BenchmarkConfig, run_benchmark
from bookmind.evaluation.learner_simulator import default_profiles


def main() -> int:
    p = argparse.ArgumentParser(description="BookMind L3 closed-loop benchmark")
    p.add_argument("--budget", type=int, default=60, help="interaction steps per (system, profile)")
    p.add_argument("--window", type=int, default=20, help="FMR verification window (last N steps)")
    p.add_argument("--systems", nargs="+",
                   default=["bookmind", "b0_basic_tutor", "b1_pdf_rag", "b2_fixed_flow"])
    p.add_argument("--json", type=str, default="", help="write JSON report to this path")
    p.add_argument("--profiles", type=int, default=0,
                   help="limit to the first N profiles (0 = all)")
    args = p.parse_args()

    print("=" * 64)
    print("学迹 / BookMind — L3 闭环 Benchmark（确定性模拟器）")
    print("=" * 64)

    profiles = default_profiles()
    if args.profiles > 0:
        profiles = profiles[: args.profiles]

    cfg = BenchmarkConfig(budget=args.budget, verification_window=args.window,
                          systems=args.systems)
    report = run_benchmark(profiles=profiles, config=cfg)

    print(f"\nconfig: budget={cfg.budget} window={cfg.verification_window} "
          f"systems={cfg.systems} concepts={report['config']['n_concepts']} "
          f"profiles={report['config']['n_profiles']}")

    # Per-system summary.
    print("\n--- 汇总（各系统跨 profile 均值）---")
    header = f"{'system':<18}{'strict_acc':>12}{'±1_acc':>10}{'FMR':>8}{'coverage':>10}{'under':>8}{'interv':>8}"
    print(header)
    print("-" * len(header))
    for sys_name, s in report["summary"].items():
        print(f"{sys_name:<18}{s['strict_accuracy']:>12.3f}{s['pm1_accuracy']:>10.3f}"
              f"{s['fmr']:>8.3f}{s['coverage']:>10.3f}{s['underestimation']:>8.3f}"
              f"{s['intervention_rate']:>8.3f}")

    # Honest boundary (EVALUATION §9): print what the simulator does NOT prove.
    print("\n--- 诚实边界 ---")
    print("模拟器只代表受控合成行为，不证明真实学生教学效果；")
    print("evidence_score 不是概率；FSRS-inspired 参数未经真实学生校准；")
    print("合成模拟器结果不得与人工标注集指标合并成一个总分。")

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\nJSON report → {args.json}")

    print("\n" + "=" * 64)
    print("benchmark 完成。结果可复现（同 profile + seed + 预算 → 同结果）。")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
