"""运行一次完整多智能体分析，把决策 + 完整报告保存为 JSON（前端演示数据）。

用法（约 10-15 分钟）：
    TICKER=600519.SS DATE=2026-08-15 .venv/bin/python scripts/dump_report.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.analysis import run_analysis  # noqa: E402

ticker = os.environ.get("TICKER", "600519.SS")
date = os.environ.get("DATE", "2026-08-15")

print(f"分析 {ticker} @ {date} …（约 10-15 分钟）")
result = run_analysis(ticker, date)

out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(out_dir, exist_ok=True)
out = os.path.join(out_dir, "sample_report.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(f"已保存: {out}")
print(f"决策: {result['decision']} → {result['decision_zh']}")
