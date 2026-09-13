"""一轮回测的**验收**：四表自洽 + 净值恒等式 + 「没有静默空转的一天」。

这个脚本回答的是"这一轮跑出来的历史，能不能信"。它**不是**单元测试——不造数、不打桩，
直接读生产库里已经跑完的那个 run，用生产代码自己的定义去核对（``_trades_on`` 划当日成交、
``list_plan_reports`` 出报告清单），所以它验的是"库里那份记录自不自洽"。

六条判据（与方案 §4 阶段 C 的每轮验收一一对应）：

  A. **四表自洽**：step 里记的成交 == trades 表里当日的那些（逐 trade_id 比，不是只比条数）；
     每笔成交都有对应的 order；持仓满足 ``hold = available + frozen``。
  B. **净值恒等式**：每一天 ``cash + Σ市值 == total_assets``；且 Σ(positions_json 的市值) 与
     step.market_value 相等——后者证明盯市那一行是**当日持仓**算出来的，不是抄来的。
  C. **计划非空**（首日除外）：首日没有前置研究日是**设计**，豁免；其余任何一天
     ``plan_json`` 为空都说明计划层断了。
  D. **没有静默空转的一天**：非首日 + 无计划 + status 仍是 ok —— 这正是修复前 09-08 的形态
     （step 报 ok、整轮报"完成：N 个交易日"，实际团队那天没有可执行的东西）。单独列出来，
     因为它是"结论不可信"的根因，不是普通的缺字段。
  E. **报告页每个交易日都有条目**：清单日期集合 == step 日期集合，且 ``has_plan`` 与库里一致。
     修复前清单**只列有 plan 行的日期**，缺计划的日子直接从页面上消失。
  F. **监控卡片对得上成交**：当天成交过的标的，在当天的监控卡片里必须有**已触发**的档。
     卡片与撮合同判据（执行口径）就是为了这一条；对不上就说明口径又分叉了。

用法（在 product/trade_master 下；容器里跑见 §部署）：
    .venv/bin/python scripts/verify_backtest_run.py --run-id 2 --owner-user-id 1
    .venv/bin/python scripts/verify_backtest_run.py --run-id 2 --owner-user-id 1 --quick

``--quick`` 跳过 F（监控卡片要按日取行情，慢；只在排查时关上）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import backtest as bt  # noqa: E402
from app import db  # noqa: E402

_TOL = 0.02  # 分位舍入：盯市处处 round(,2)，累计误差给两分钱


class Reporter:
    def __init__(self) -> None:
        self.fails: list[str] = []
        self.n = 0
        self.warns: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        self.n += 1
        if cond:
            print(f"  ✅ {name}")
        else:
            self.fails.append(name)
            print(f"  ❌ {name}{('  →  ' + detail) if detail else ''}")
        return bool(cond)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)
        print(f"  ⚠️  {msg}")


def _step_rows(run_id: int) -> list[db.BacktestStep]:
    session = db.get_session()
    try:
        return (
            session.query(db.BacktestStep)
            .filter(db.BacktestStep.run_id == run_id)
            .order_by(db.BacktestStep.trade_date)
            .all()
        )
    finally:
        session.close()


def _shadow_id(run_id: int) -> int:
    session = db.get_session()
    try:
        r = session.query(db.BacktestRun).filter(db.BacktestRun.id == run_id).first()
        return int(r.shadow_user_id or 0) if r else 0
    finally:
        session.close()


def _newer_runs_on_same_shadow(run_id: int, sh: int) -> list[int]:
    """比这个 run 更晚起跑、且共用同一个影子账户的 run id。

    影子账户是**刻意跨 run 复用**的（``ensure_shadow_user``：``EngineRun`` 研究缓存最贵），
    而 ``reset_shadow_book`` 虽然现在保留计划（``plans=False``），**委托与成交仍然会清**
    （``_clear_book_runtime`` 本就是"清空运行痕迹"）。于是后起的 run 一开跑，前一个 run 的
    ``orders``/``trades`` 两表就没了——它们的 step 快照还在，但"step 与表逐笔相等"这条
    判据对**非最新**的 run 已经无从谈起。

    所以 A 条只对影子上的最新 run 有判别力：对更早的 run 报失败是把"存储设计"误报成"缺陷"。
    """
    session = db.get_session()
    try:
        rows = (
            session.query(db.BacktestRun.id)
            .filter(db.BacktestRun.shadow_user_id == sh, db.BacktestRun.id > run_id)
            .all()
        )
        return [int(x[0]) for x in rows]
    finally:
        session.close()


def main() -> int:  # noqa: C901 —— 验收脚本，线性罗列六条判据
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--owner-user-id", type=int, required=True)
    ap.add_argument("--quick", action="store_true", help="跳过 F（监控卡片，较慢）")
    a = ap.parse_args()

    rep = Reporter()
    steps = _step_rows(a.run_id)
    sh = _shadow_id(a.run_id)
    print("=" * 68)
    print(f"回测验收 run={a.run_id}  影子账户={sh}  交易日 {len(steps)} 天")
    print("=" * 68)
    if not steps:
        print("❌ 这个 run 一个 step 都没有，无法验收")
        return 1

    days = [s.trade_date for s in steps]
    first_day = days[0]

    # ------------------------------------------------------------ A 四表自洽
    print("\n=== A. 四表自洽（orders / trades / positions / steps）===")
    newer = _newer_runs_on_same_shadow(a.run_id, sh)
    if newer:
        rep.warn(
            f"影子上还有更晚的 run {newer}——它的起跑清掉了本 run 的 orders/trades 两表"
            "（step 快照仍在，故 B/C/D/E/F 照验）。A 条的『step 与表逐笔相等』对非最新 run "
            "无从谈起，已跳过；要验 A 就对影子上**最新**的那个 run 跑。"
        )
    session = db.get_session()
    try:
        orphan_orders: list[str] = []
        mismatched_days: list[str] = []
        total_recorded = 0
        for s in steps:
            rec = json.loads(s.trades_json or "[]")
            total_recorded += len(rec)
            # 用生产代码自己的划分口径，而不是另写一份 traded_at 区间判断
            actual = bt._trades_on(sh, s.trade_date)
            rec_ids = sorted(t.get("trade_id") or "" for t in rec)
            act_ids = sorted(t.get("trade_id") or "" for t in actual)
            if rec_ids != act_ids:
                mismatched_days.append(
                    f"{s.trade_date}: step={len(rec_ids)} 表={len(act_ids)}"
                )
            for t in actual:
                oid = t.get("order_id") or ""
                hit = session.query(db.Order).filter(db.Order.order_id == oid).first()
                if hit is None:
                    orphan_orders.append(f"{s.trade_date}/{oid}")
        if not newer:
            rep.check(
                "★ step.trades_json 与 trades 表**逐 trade_id** 相等（不是只比条数）",
                not mismatched_days,
                "；".join(mismatched_days[:5]),
            )
            rep.check("每笔成交都有对应的委托单", not orphan_orders,
                      "；".join(orphan_orders[:5]))

        # step 内部自洽（不依赖两表，故对任何 run 都成立）：成交条数 == trade_count
        bad_cnt = [
            f"{s.trade_date}: {len(json.loads(s.trades_json or '[]'))} != {s.trade_count}"
            for s in steps
            if len(json.loads(s.trades_json or "[]")) != int(s.trade_count or 0)
        ]
        rep.check("step.trade_count 与 trades_json 条数一致", not bad_cnt,
                  "；".join(bad_cnt[:5]))

        pos = session.query(db.Position).filter(
            db.Position.user_id == sh, db.Position.book == 1
        ).all()
        bad_pos = [
            f"{p.stock_code} hold={p.hold_qty} avail={p.available_qty} frozen={p.frozen_qty}"
            for p in pos
            if abs(p.hold_qty - (p.available_qty + p.frozen_qty)) > 0
        ]
        rep.check(
            f"持仓恒等式 hold = available + frozen（{len(pos)} 只）",
            not bad_pos, "；".join(bad_pos[:5]),
        )
        print(f"  ℹ️  全区间录得成交 {total_recorded} 笔")
        neg = [f"{p.stock_code} hold={p.hold_qty}" for p in pos if p.hold_qty < 0]
        rep.check("没有负持仓", not neg, "；".join(neg[:5]))
    finally:
        session.close()

    # ------------------------------------------------------------ B 净值恒等式
    print("\n=== B. 净值恒等式 ===")
    bad_ident: list[str] = []
    bad_mv: list[str] = []
    for s in steps:
        lhs = float(s.cash or 0.0) + float(s.market_value or 0.0)
        if abs(lhs - float(s.total_assets or 0.0)) > _TOL:
            bad_ident.append(
                f"{s.trade_date}: {lhs:.2f} != {s.total_assets:.2f}"
            )
        pj = json.loads(s.positions_json or "[]")
        mv = round(sum(float(p.get("market_value") or 0.0) for p in pj), 2)
        if abs(mv - float(s.market_value or 0.0)) > _TOL:
            bad_mv.append(
                f"{s.trade_date}: Σ持仓市值 {mv:.2f} != step {s.market_value:.2f}"
            )
    rep.check("★ 每天 cash + Σ市值 == total_assets", not bad_ident,
              "；".join(bad_ident[:5]))
    rep.check("★ step.market_value == Σ(当日 positions_json 市值)", not bad_mv,
              "；".join(bad_mv[:5]))

    # 逐日累计口径：day_pnl 累加应回到 (末日总资产 − 起始基准)
    acc = round(sum(float(s.day_pnl or 0.0) for s in steps), 2)
    span = round(float(steps[-1].total_assets or 0.0) - float(steps[0].total_assets or 0.0)
                 + float(steps[0].day_pnl or 0.0), 2)
    # day_pnl 首日相对 init_basis，故首日已含起跑那一段，直接全加即为区间变动
    rep.check(f"day_pnl 逐日累加 = 末日−起始基准（{acc:.2f}）",
              abs(acc - span) <= 0.05, f"累加 {acc:.2f} vs 口径 {span:.2f}")

    # ------------------------------------------------------------ C/D 计划
    print("\n=== C/D. 计划非空 + 没有静默空转的一天 ===")
    empty_plan = [
        s.trade_date for s in steps
        if s.trade_date != first_day and not (json.loads(s.plan_json or "{}") or {}).get("actions")
    ]
    rep.check("★ 首日之外每天都有非空计划（首日无前置研究日，属设计，豁免）",
              not empty_plan, "；".join(empty_plan))
    # D 是 C 的**另一列**读法：C 看 plan_json 空不空，D 看 status 有没有如实标出来。
    # 只查 C 会漏掉"计划其实生成了、但那天压根没执行层可用的东西"这一类；只查 D 会漏掉
    # "计划没生成、status 却因为别的原因被标脏了"。两者读的是不同的列，都得查。
    silent = [
        s.trade_date for s in steps
        if s.trade_date != first_day
        and not (json.loads(s.plan_json or "{}") or {}).get("actions")
        and s.status == "ok"
    ]
    rep.check("★ 没有「计划缺失但 status 仍为 ok」的静默空转日", not silent,
              "；".join(silent))
    not_ok = [f"{s.trade_date}:{s.status}" for s in steps if s.status != "ok"]
    if not_ok:
        rep.warn(f"有 {len(not_ok)} 天 status != ok（degraded/error 是响亮的，这里只作提示）：{not_ok}")

    # ------------------------------------------------------------ E 报告清单
    print("\n=== E. 报告页每个交易日都有条目 ===")
    try:
        lst = bt.list_plan_reports(a.run_id, a.owner_user_id)
        listed = {x["trade_date"]: x for x in lst.get("dates", [])}
        rep.check("★ 清单日期集合 == step 日期集合",
                  set(listed) == set(days),
                  f"缺 {sorted(set(days) - set(listed))} / 多 {sorted(set(listed) - set(days))}")
        wrong = [
            d for d in days
            if d in listed and bool(listed[d].get("has_plan")) != (d != first_day)
        ]
        rep.check(
            "★ 每天的 has_plan 与库里一致（首日 False，其余 True）",
            not wrong, "；".join(f"{d}: {listed.get(d, {}).get('has_plan')}" for d in wrong[:5]),
        )
        rep.check("首日被显式标成 first_day 而不是笼统缺失",
                  listed.get(first_day, {}).get("plan_missing_reason") == "first_day",
                  str(listed.get(first_day, {})))
    except Exception as e:  # noqa: BLE001
        rep.check("报告清单可读", False, f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------ F 监控对成交
    if a.quick:
        print("\n=== F. 监控卡片与成交一致（--quick 已跳过）===")
    else:
        print("\n=== F. 监控卡片与当天成交对得上 ===")
        mismatch = 0
        checked_days = 0
        for s in steps:
            rec = json.loads(s.trades_json or "[]")
            if not rec:
                continue
            checked_days += 1
            try:
                mon = bt.get_day_monitor_conditions(a.run_id, a.owner_user_id, s.trade_date)
            except Exception as e:  # noqa: BLE001
                print(f"  ❌ {s.trade_date} 取监控条件失败：{type(e).__name__}: {e}")
                rep.n += 1
                rep.fails.append(f"{s.trade_date} 监控条件")
                continue
            fired = {x["code"] for x in mon.get("actions", []) if x.get("triggered")}
            traded = {t.get("stock_code") for t in rec}
            miss = traded - fired
            if miss:
                mismatch += 1
                print(f"  ❌ {s.trade_date} 成交了却没有已触发档：{sorted(miss)}")
                print(f"      卡片里这些票的档："
                      f"{[{k: x.get(k) for k in ('code','kind','triggered','price','trigger_price')} for x in mon.get('actions', []) if x['code'] in miss]}")
        rep.n += 1
        if mismatch:
            rep.fails.append("监控卡片与成交一致")
        else:
            print(f"  ✅ ★ {checked_days} 个有成交的日子，每一笔都能在当天卡片里找到已触发档")
        if checked_days == 0:
            rep.warn("这个 run 一天成交都没有——F 条无判别力（空仓起步 + 只跌不涨时正常，"
                     "但连续多轮都如此就该去看计划层的目标权重）")

    # ------------------------------------------------------------ 汇总
    print("\n" + "=" * 68)
    if rep.fails:
        print(f"❌ {len(rep.fails)}/{rep.n} 条未通过：")
        for f in rep.fails:
            print(f"   - {f}")
        return 1
    print(f"✅ {rep.n} 条判据全部通过")
    if rep.warns:
        print(f"（另有 {len(rep.warns)} 条提示）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
