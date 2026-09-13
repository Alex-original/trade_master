"""标定：当前生产配置下单次 LLM 调用的真实延迟。

**为什么需要它**：回测耗时的乘数已经数清楚了——一次 Stage1 深度分析 ≈ 16-22 次 LLM
调用（中位 19：固定节点 8 + market 3-5 + sentiment 1 + news 2-4 + fundamentals 2-5；
``SignalProcessor`` 已改为纯正则解析、不调 LLM）。但乘数另一边的「单次延迟」仓库里没有
当前数据：唯一一次真实计时是 ``docs/测试报告_模拟交易AI托管_v1.0.md`` 记的
6862.HK @2026-09-08 约 12 分钟，那次**发生在切换模型之前**，且只有一个样本。
代码注释里的「10-15 分钟」全是写死的文案，没有任何测量支撑。

本脚本用 ``app.analysis.build_engine_config()``（即生产真实配置）实测单次延迟，
把回测耗时从推算收敛成实测。

**不要用 ``scripts/smoke_engine.py`` 做这件事**——它在 :30-31 硬编码了已下线的
``deepseek-v4-pro`` / ``deepseek-v4-flash``，测出的数字没有代表性。

两个测点，给的是**区间**而不是一个数：

  A. 极短单轮   —— 延迟**下界**（短 prompt、极短输出）
  B. 长输入长输出 —— 接近真实分析调用（长 prompt + max_tokens 预算内的中文长报告）

真实分析的单次延迟落在两者之间、更靠近 B（真实调用还要带工具定义与多轮历史）。

用法：
    .venv/bin/python scripts/probe_llm_latency.py

成本：两次 LLM 调用，秒级到分钟级。不写库、不改任何状态。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 真实分析里的 prompt 大致形态：一份带行情数据的中文研究简报。
# 不必精确——目的是让输入长度与真实调用同量级。
_LONG_PROMPT = (
    "你是一名 A 股市场技术分析师。以下是某标的最新一段日线行情与技术指标数据：\n"
    + "\n".join(
        f"2026-08-{d:02d} 开{10 + d * 0.1:.2f} 高{10.5 + d * 0.1:.2f} "
        f"低{9.8 + d * 0.1:.2f} 收{10.2 + d * 0.1:.2f} 量{1_200_000 + d * 31_000}"
        for d in range(1, 41)
    )
    + "\n\n技术指标：MACD DIF=0.42 DEA=0.31 RSI6=58.3 RSI14=54.1 "
    "KDJ_K=62.7 KDJ_D=58.9 BOLL上轨=11.24 中轨=10.41 下轨=9.58 "
    "MA5=10.38 MA10=10.21 MA20=9.97 MA60=9.42 量比=1.34 换手率=2.17%\n\n"
    "请给出一份完整的技术面分析报告，包含：趋势判断、关键支撑与压力位、"
    "量价关系解读、指标共振情况、未来 5 个交易日的可能走势与应对策略。"
    "请用中文，分点论述，不少于 600 字。"
)


def _one_call(llm, prompt: str, label: str) -> float:
    """跑一次调用并打印耗时，返回秒数。"""
    t0 = time.time()
    resp = llm.invoke(prompt)
    elapsed = time.time() - t0

    text = getattr(resp, "content", "") or ""
    usage = (getattr(resp, "response_metadata", {}) or {}).get("token_usage", {}) or {}
    out_tokens = usage.get("completion_tokens")
    detail = f"，输出 {len(text)} 字"
    if out_tokens:
        detail += f" / {out_tokens} tokens"
    print(f"   {label}: {elapsed:.1f}s{detail}")
    return elapsed


def main() -> int:
    # 导入 app.analysis 会连带 import tradingagents，后者负责 load_dotenv，
    # 因此 .env 里的 DEEPSEEK_API_KEY 在此之前就位。
    from app.analysis import build_engine_config
    from tradingagents.llm_clients import create_llm_client

    config = build_engine_config()
    model = config["deep_think_llm"]
    quick = config["quick_think_llm"]
    max_tokens = config["max_tokens"]

    print("== 当前生产配置 ==")
    print(f"   deep_think_llm  = {model}")
    print(f"   quick_think_llm = {quick}")
    print(f"   max_tokens      = {max_tokens}")

    if not config.get("llm_provider"):
        print("!! 未配置 llm_provider")
        return 1

    try:
        llm = create_llm_client(
            config["llm_provider"], model, config.get("backend_url"), max_tokens=max_tokens
        ).get_llm()
    except Exception as e:  # noqa: BLE001
        print(f"!! 构造 LLM 客户端失败：{e}")
        print("   （.env 里是否配了 DEEPSEEK_API_KEY？）")
        return 1

    print("\n== A) 极短单轮（延迟下界）==")
    short = _one_call(llm, "Reply with exactly: OK", "短调用")

    print("\n== B) 长输入 + 长输出（接近真实分析调用）==")
    long = _one_call(llm, _LONG_PROMPT, "长调用")

    # 乘数来自图结构统计（见模块 docstring）。给区间而不是单点：
    # 真实调用既不会全是短交互，也不会次次顶满输出预算。
    lo_calls, hi_calls = 16, 22
    print("\n== 重估一次 Stage1 深度分析的耗时 ==")
    print(f"   单次延迟区间：{min(short, long):.1f}s ～ {max(short, long):.1f}s")
    print(f"   LLM 调用次数：{lo_calls} ～ {hi_calls} 次（中位 19）")
    for name, per_call in (("取区间下沿（短调用×16）", short * lo_calls),
                           ("取区间上沿（长调用×22）", long * hi_calls)):
        print(f"   {name}: {per_call / 60:.1f} 分钟")
    mid = (short + long) / 2 * 19
    print(f"   中位估计（两测点均值 × 19）：{mid / 60:.1f} 分钟")

    print("\n   参考：仓库唯一实测是 6862.HK @2026-09-08 的 12 分钟（切换模型之前）。")
    print("   注意真实调用带工具定义与多轮历史，会比 B 测点更重，上述估计偏乐观。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
