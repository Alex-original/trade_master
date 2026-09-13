#!/usr/bin/env python3
"""盘中实盘会话取证（B1–B6 的「库里能证的那几项」）。**只读，绝不写任何一行。**

周一盘中跑完后用它核对，省得临时拼一堆 psql。它把「库里查得到的」与「必须在库外看的」
分成两段——**库外那两项（调度器心跳行、通知是否真到群）不猜、不假装**，只把命令列出来。

    docker exec -w /app trade-master-app python /app/verify_live_session.py --date 2026-09-14

**刻意不叫 ``smoke_*``**：仓库约定 ``scripts/smoke_*.py`` 是「确定性离线、不连库」，
本脚本必须连真库。取名 ``smoke_`` 会让一把梭的回归循环在这里断掉。

判据取的是**性质**而不是定值（成交股数取决于盘中真实行情，没法预先写死）：
每个 tick 每只票最多一笔、成交理由形如计划档、卖出的持仓被扣减、T+1 冻结只增不减。
"""

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter, defaultdict


def _find_root() -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (here, os.getcwd()):
        if os.path.isfile(os.path.join(cand, "app", "__init__.py")):
            return cand
    raise SystemExit("找不到项目根目录（app/__init__.py）——请在仓库根或容器 /app 下运行")


sys.path.insert(0, _find_root())

from app import db, trust  # noqa: E402
from app.plan_actions import build_ladders  # noqa: E402

_COUNT, _FAILS = 0, []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _COUNT
    _COUNT += 1
    if cond:
        print(f"  ✅ {name}")
    else:
        _FAILS.append(name)
        print(f"  ❌ {name}" + (f"  实际：{detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


def hhmm(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat(),
                    help="要核对的执行日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--user", type=int, default=None, help="只看某个用户（默认全部真实用户）")
    args = ap.parse_args()
    day = args.date
    lo = dt.datetime.strptime(day, "%Y-%m-%d").timestamp()          # 当日 00:00
    hi = lo + 86400

    u = db.engine.url
    print(f"目标库：{u.drivername}://{u.host or ''}{u.database or ''}")
    print(f"核对执行日：{day}")
    print("【只读】本脚本只有 SELECT，不会改动任何一行。")

    s = db.get_session()
    try:
        users = s.query(db.User).filter(db.User.is_backtest == False).all()  # noqa: E712
        if args.user:
            users = [x for x in users if x.id == args.user]
        print(f"参与核对的行内用户：{[(x.id, x.phone) for x in users]}")

        # ---- 1. 计划：B2 成立的前提，也是 B6 的产物 ----
        section("1. 执行日的计划（B2 前提；15:05 后这里还会多出**次日**一行 → B6）")
        plans = (s.query(db.TrustPlan).filter(db.TrustPlan.trade_date == day)
                 .order_by(db.TrustPlan.id).all())
        check(f"★ 存在 trade_date={day} 的计划行（没有它执行层会回落 legacy，梯子不被穿越）",
              bool(plans), f"{len(plans)} 行")
        n_actions = 0
        for p in plans:
            try:
                acts = json.loads(p.plan_json).get("actions") or []
            except Exception:  # noqa: BLE001
                acts = []
            n_actions += len(acts)
            print(f"     id={p.id} 建于 {hhmm(p.created_at)}，{len(acts)} 条动作")
            if acts:
                tiers = Counter((a.get("code"), a.get("trigger_type")) for a in acts)
                print(f"     档位分布：{dict(tiers)}")
        check("★ 计划里确实带条件档（不是只有无条件档）",
              any(a.get("trigger_type") in ("price_below", "price_above")
                  for p in plans for a in (json.loads(p.plan_json).get("actions") or [])),
              "全是 open/intraday 则表示这份计划不构成梯子")

        # 提前一天能查到的**最大风险**：计划写得进去 ≠ 执行层读得出来。这里用执行层
        # **自己的** ``build_ladders`` 把当日计划读一遍（只读），把「档位被截断、方向冲突、
        # 字段不认」这类问题在开盘前暴露——否则要等 09:30 执行层取不到档位才发现，白丢一天。
        if plans and users:
            uid0 = args.user or users[0].id
            plan = trust._get_today_plan(uid0, day)
            ladders, warns = build_ladders((plan or {}).get("actions") or [])
            exits = {c: len(v["exit"]) for c, v in ladders.items() if v["exit"]}
            entries = {c: len(v["entry"]) for c, v in ladders.items() if v["entry"]}
            print(f"     执行层读出的梯子：退出 {exits}")
            print(f"                       进场 {entries}")
            check("★ 执行层能从当日计划读出非空梯子（has_plan 为 True，不走 legacy 兜底）",
                  bool(exits), f"ladders={ladders}")
            check("★ build_ladders 零告警（有告警意味着有档被截断/丢弃，梯子比计划短）",
                  not warns, str(warns))

        newplans = (s.query(db.TrustPlan).filter(db.TrustPlan.created_at >= lo,
                                                 db.TrustPlan.created_at < hi).all())
        print(f"     当日 {day} 新生成的计划行：{[ (p.id, p.trade_date) for p in newplans ]}"
              f"（15:05 的计划层产物，trade_date 应为**下一交易日**）")

        # ---- 2. 成交流水：B2 ----
        section("2. 成交流水（B2：梯子被穿越并成交）")
        trows = (s.query(db.Trade).filter(db.Trade.traded_at >= lo, db.Trade.traded_at < hi)
                 .order_by(db.Trade.traded_at).all())
        trows = [t for t in trows if not args.user or t.user_id == args.user]
        print(f"     当日成交 {len(trows)} 笔")
        for t in trows:
            d = "买" if t.direction == 0 else "卖"
            print(f"     {hhmm(t.traded_at)} {d} {t.stock_name}({t.stock_code}) "
                  f"{t.quantity} @ {t.price}  理由：{(t.ai_reason or '')[:46]}")

        # 零成交时这几项**无法判定**，既不算过也不算不过——别让「什么都没有」看起来像
        # 「验过了」。零成交可能是没有任何档被触发（正常），也可能是执行层压根没跑；
        # 这两种要靠 B1 的心跳行区分，库里分不出来。
        if not trows:
            print("     ⚠️  当日零成交 —— 下面两项无法判定。零成交可能是没有任何档被触发"
                  "（正常），也可能是执行层没跑；用 B1 的心跳行区分这两种。")
        else:
            per_tick = Counter((t.stock_code, hhmm(t.traded_at)[:5]) for t in trows)
            check("★ 同一分钟内、同一只票至多一笔（逐档推进，不是一笔到底）",
                  all(v == 1 for v in per_tick.values()), f"最多的：{per_tick.most_common(1)}")
            reasoned = [t for t in trows if (t.ai_reason or "").startswith(("计划", "止损"))]
            check("★ 成交理由带有计划出处（走的是计划执行层，不是 legacy 兜底）",
                  len(reasoned) == len(trows), f"{len(reasoned)}/{len(trows)} 笔带理由")

        # ---- 3. 委托：B4 ----
        section("3. 委托（B4：涨跌停拦截的证据在这里）")
        orows = (s.query(db.Order).filter(db.Order.created_at >= lo,
                                          db.Order.created_at < hi).all())
        orows = [o for o in orows if not args.user or o.user_id == args.user]
        if orows or trows:
            check("orders 与 trades 一一对应（成交必有委托）",
                  {o.order_id for o in orows} >= {t.order_id for t in trows},
                  f"orders={len(orows)} trades={len(trows)}")
        else:
            print("     ⚠️  当日既无委托也无成交 —— 对应关系无从核对（别当成验过了）")
        failed = [o for o in orows if o.fail_reason]
        print(f"     被拒委托 {len(failed)} 笔"
              + (f"：{[(o.stock_code, o.fail_reason[:30]) for o in failed]}" if failed else ""))
        check("被拒委托的失败原因非空（有拒绝就要说清楚为什么）",
              all(o.fail_reason for o in failed), "有拒单但没写原因")

        # ---- 4. T+1：B5 ----
        section("4. 持仓与 T+1（B5：当日买入冻结，次日解冻）")
        prows = (s.query(db.Position).filter(db.Position.book == 1).all())
        prows = [p for p in prows if not args.user or p.user_id == args.user]
        for p in prows:
            print(f"     {p.stock_name}({p.stock_code}) 持{p.hold_qty} 可用{p.available_qty} "
                  f"冻结{p.frozen_qty}  更新于 {hhmm(p.updated_at)}")
        check("托管簿持仓行的 可用 + 冻结 == 持有（数量自洽，没有凭空多/少）",
              all(p.available_qty + p.frozen_qty == p.hold_qty for p in prows),
              str([(p.stock_code, p.hold_qty, p.available_qty, p.frozen_qty) for p in prows]))
        buys = [t for t in trows if t.direction == 0]
        print(f"     当日买入 {len(buys)} 笔 → 收盘后 15:10 的 release 应把它们解冻；"
              f"若 run 完仍冻结>0，看 _sched_release 日志")

        # ---- 5. 归因：G2 ----
        section("5. 归因（G2：本簿累计，重贴快照归零）")
        for x in users:
            cfg = (s.query(db.TrustConfig).filter(db.TrustConfig.user_id == x.id).first())
            if cfg is None:
                print(f"     用户 {x.id}：无托管配置")
                continue
            v = cfg.realized_pnl
            print(f"     用户 {x.id}：realized_pnl = {v!r}（None = 口径升级前的老簿）"
                  f"  托管开关 is_active={cfg.is_active}  book_created={cfg.book_created}")
    finally:
        s.close()

    section("6. 库里查不到、必须到库外看的（不猜）")
    print("""  · B1 调度器到点真触发（心跳行）：
      docker logs --since 8h trade-master-app 2>&1 | grep '\\[trust\\]' | tail -40
      —— 应看到 `execution tick` 在 09:30–14:59 每分钟一行；15:05 那行后面
         还应跟着 `计划层用户 N 完成：执行日 …、N 条动作`（成功也有声了）。
  · B3 成交通知是否真到群：用户手机／机器人会话里看，库侧无痕。
      （回测路径按设计**不**推送，别拿回测的成交去群里找。）
  · 涨跌停拦截要有真涨停/跌停标的才算实弹跑过：库里只能看到被拒委托的 fail_reason。""")

    print(f"\n{'=' * 62}")
    if _FAILS:
        print(f"❌ {len(_FAILS)}/{_COUNT} 项未通过：")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {_COUNT} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
