"""CLI Demo：自然语言策略解析（本地规则版）。

用法（项目根目录）：
    python scripts/demo_parse.py [可选策略文本]
不带参数时跑内置示例列表。

依赖：仅 Python 标准库。演示 NL → 结构化 StrategyConfig（local 命中链路），
LLM 兜底在 Sprint 1 接入。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让脚本可直接在 scripts/ 下运行：把项目根加入 import path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.nlparse import parse_strategy  # noqa: E402

EXAMPLES = [
    "5日均线上穿20日线",
    "MA20 和 MA60 金叉买入，死叉卖出",
    "放量突破年线买入",
    "股价跌破 60 日线就卖出",
    "收盘价站上20日均线买入，跌破20日均线卖出",
    "MACD 金叉买入，死叉卖出",
    "连续三天跌停后今天放量",
    "缩量回踩20日线",
]


def main() -> int:
    args = sys.argv[1:]
    texts = [args[0]] if args else EXAMPLES
    for t in texts:
        r = parse_strategy(t)
        print("=" * 72)
        print(f"原文：{t!r}")
        print(f"结果：source={r.source}" + (f"  规则={r.rule_name}" if r.rule_name else ""))
        print(f"说明：{r.message}")
        if r.strategy is not None:
            print(r.strategy.to_json())
        else:
            print("（未产出策略 ——> 该走 LLM 兜底）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
