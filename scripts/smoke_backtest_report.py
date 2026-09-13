"""回测「流程报告」：按计划生效日取数 + 与实盘同一个渲染器，全部离线。

不连 Postgres、不连 Wind、不跑 LLM。在**内存 SQLite** 上用真实的 ``app.db`` 打真实的库，
直接造 ``TrustPlan`` / ``BacktestStep`` 行，然后断言 ``backtest.list_plan_reports`` /
``backtest.get_plan_report`` 给的数，以及 ``plan_report.render_plan_report`` 出来的 HTML。

这个功能的验收标准只有一句：「结构和实盘『次日行动报告』一样，但要能按日期看」。
所以这里守两类东西：

  A. **按日期取对了没有**——尤其是"报告里的账户现状是哪一天的账"。计划是研究日 D-1
     盘后产出的，报告必须配**那一天收盘**的账；配成回测结束时的最终持仓，会让 09-09
     那份计划显示"0 持仓"却列着 38 条建仓动作，一份自相矛盾的报告。
     这是本文件里最重要的一组断言（§3）。

  B. **没跑到的那一天不许冒充跑过**——计划落库在前、step 落库在后（研究跑完才存 step），
     所以"有计划、没 step"就是"这一天没执行"。不点破的话它和跑完的那天长得一样（§4）。

另有两条容易悄悄退化的：越权（§6）、以及**没有冻结研究时不许回落实时查库**（§5）——
影子 user 的 EngineRun 跨研究日累积，兜底查库取到的是"最近一次"，那可能是**更晚**的日期，
即未来函数。

用法（在 product/trade_master 下）：
    .venv/bin/python scripts/smoke_backtest_report.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import db  # noqa: E402

# ---------------------------------------------------------------- 内存库
# StaticPool + 单连接：多个 session 必须看到同一个内存库（默认每个连接一个独立内存库）
_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
db.Base.metadata.create_all(_engine)
db.SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)  # type: ignore[assignment]

from app import backtest as bt  # noqa: E402  （必须在 patch 之后）
from app import plan_report  # noqa: E402
from app.errors import ServiceError  # noqa: E402

# ---------------------------------------------------------------- 断言脚手架
_COUNT = 0
_FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _COUNT
    _COUNT += 1
    if cond:
        print(f"  ✅ {name}")
    else:
        _FAILS.append(name)
        print(f"  ❌ {name}{('  →  ' + detail) if detail else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 造数
NOW = time.time()
OWNER_PHONE = "13900000001"
OTHER_PHONE = "13900000002"

#: 回测的三个交易日；计划分别针对后两天 + 一个"没跑到"的第四天
D0708, D0709, D0710, D0711 = "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11"


def fresh_db() -> int:
    """清空所有表，建一个真实用户（含托管配置），返回 owner id。"""
    session = db.get_session()
    try:
        for table in reversed(db.Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
        owner = db.User(phone=OWNER_PHONE, created_at=NOW, is_backtest=False)
        session.add(owner)
        session.flush()
        session.add(db.TrustConfig(
            user_id=owner.id, is_active=True, book_created=True, available_cash=0.0,
            stock_scope=1, updated_at=NOW,
        ))
        session.commit()
        return int(owner.id)
    finally:
        session.close()


def owner_id_by_phone(phone: str) -> int:
    session = db.get_session()
    try:
        return int(session.query(db.User.id).filter(db.User.phone == phone).scalar())
    finally:
        session.close()


def add_user(phone: str) -> int:
    """**不清库**再加一个用户。验"两个 run 之间不串"必须在同一个库里放两个人才算数。"""
    session = db.get_session()
    try:
        u = db.User(phone=phone, created_at=NOW, is_backtest=False)
        session.add(u)
        session.flush()
        uid = int(u.id)
        session.add(db.TrustConfig(
            user_id=uid, is_active=True, book_created=True, available_cash=0.0,
            stock_scope=1, updated_at=NOW,
        ))
        session.commit()
        return uid
    finally:
        session.close()


def shadow_id(owner: int) -> int:
    session = db.get_session()
    try:
        return int(bt.ensure_shadow_user(session, owner))
    finally:
        session.close()


def make_run(owner: int, **kw) -> int:
    rid = bt.create_run(
        owner,
        start_date=kw.pop("start_date", D0708),
        end_date=kw.pop("end_date", D0710),
        init_mode=kw.pop("init_mode", "cash"),
        init_cash=kw.pop("init_cash", 500000.0),
        universe=kw.pop("universe", ["600519.SH"]),
    )
    if kw:
        session = db.get_session()
        try:
            row = session.query(db.BacktestRun).filter(db.BacktestRun.id == rid).first()
            for k, v in kw.items():
                setattr(row, k, v)
            session.commit()
        finally:
            session.close()
    return rid


def put_plan(user_id: int, trade_date: str, *, actions=None, research=None,
             missing=None, summary="思路一句话", created_at=None) -> None:
    """落一份 TrustPlan。``research`` 传 None 表示这份计划**没有冻结研究快照**。"""
    process = {"research": research or {}, "research_missing": list(missing or [])}
    blob = {
        "summary": summary,
        "cash_target": 0.2,
        "risk_notes": "风险提示",
        "actions": actions if actions is not None else [],
        "process": process,
    }
    session = db.get_session()
    try:
        session.add(db.TrustPlan(
            user_id=user_id, trade_date=trade_date,
            plan_json=json.dumps(blob, ensure_ascii=False),
            created_at=created_at if created_at is not None else NOW,
        ))
        session.commit()
    finally:
        session.close()


def put_step(rid: int, trade_date: str, *, cash: float, positions: list[dict],
             trades: list[dict] | None = None, status: str = "ok") -> None:
    mv = round(sum(float(p.get("market_value") or 0.0) for p in positions), 2)
    session = db.get_session()
    try:
        session.add(db.BacktestStep(
            run_id=rid, trade_date=trade_date, cash=cash, market_value=mv,
            total_assets=round(cash + mv, 2), day_pnl=0.0, realized_pnl=0.0, fees=0.0,
            positions_json=json.dumps(positions, ensure_ascii=False),
            trades_json=json.dumps(trades or [], ensure_ascii=False),
            plan_json="{}", status=status, error="", created_at=NOW,
        ))
        session.commit()
    finally:
        session.close()


def pos(code: str, name: str, qty: int, price: float, cost: float) -> dict:
    return {"stock_code": code, "stock_name": name, "hold_qty": qty,
            "available_qty": qty, "cost_price": cost, "price": price,
            "market_value": round(qty * price, 2), "pnl": round((price - cost) * qty, 2)}


def action(code: str, name: str, act: str = "buy", tw: float = 0.05) -> dict:
    return {"code": code, "name": name, "action": act, "target_weight": tw,
            "trigger_type": "price_below", "trigger_price": 9.5, "reason": "理由"}


def research_of(code: str, rating: str = "Hold") -> dict:
    return {"rating": rating, "report": {"market": f"{code} 行情段落"}, "trade_date": D0708}


def build_fixture() -> tuple[int, int, int]:
    """标准场景：owner + 影子 + 一条回测，三个交易日两份计划 + 一天没跑到。

    账户轨迹刻意做成**每步都不同**，这样"取错了哪一天"必然暴露：
      step 07-08 = 空仓 500000（研究日，计划 07-09 就是在这一天的账上做的）
      step 07-09 = 买了 161129，现金 490037.55（计划 07-10 的研究日）
      step 07-10 = 又涨了（回测**结束时**的账——报告绝不该用它）
    """
    owner = fresh_db()
    sh = shadow_id(owner)
    # 窗口到 D0711：D0711 那份计划的用途是「计划有、step 没有」，它必须在窗口**之内**
    # （计划生效日按构造必然落在 [start, end] 里），否则测的就不是「没跑到」而是「串味」了。
    rid = make_run(owner, end_date=D0711)

    a1 = [action("161129.SZ", "原油LOF易方达"), action("518880.SH", "黄金ETF华安")]
    a2 = [action("600519.SH", "贵州茅台", tw=0.08)]
    put_plan(sh, D0709, actions=a1,
             research={"161129.SZ": research_of("161129.SZ"),
                       "518880.SH": research_of("518880.SH")},
             missing=[], created_at=NOW - 300)
    put_plan(sh, D0710, actions=a2,
             research={"600519.SH": research_of("600519.SH")},
             missing=["159842.SZ"], created_at=NOW - 200)
    # 计划有、step 没有 ⇒ 这一天没跑到（回测在它之前就结束了）
    put_plan(sh, D0711, actions=[action("000001.SZ", "平安银行")],
             research={}, missing=[], created_at=NOW - 100)

    put_step(rid, D0708, cash=500000.0, positions=[])
    put_step(rid, D0709, cash=490037.55,
             positions=[pos("161129.SZ", "原油LOF易方达", 4900, 1.97, 2.033)],
             trades=[{"stock_code": "161129.SZ", "price": 2.033, "quantity": 4900}])
    put_step(rid, D0710, cash=490037.55,
             positions=[pos("161129.SZ", "原油LOF易方达", 4900, 1.99, 2.033)])
    return owner, sh, rid


# ================================================================ 断言

def main() -> int:
    owner, sh, rid = build_fixture()

    # ---- 1. 日期清单 ----
    section("1. 日期清单：骨架是这个 run 跑过的交易日 ∪ 窗口内的计划生效日")
    data = bt.list_plan_reports(rid, owner)
    dates = data["dates"]
    check("列出全部交易日与计划生效日且按日期升序",
          [d["trade_date"] for d in dates] == [D0708, D0709, D0710, D0711],
          str([d["trade_date"] for d in dates]))
    check("★ 首日（有 step、无计划）**也在列表里**——它以前整个消失，用户看到的是少一天",
          dates[0]["trade_date"] == D0708 and dates[0]["executed"] is True,
          json.dumps(dates[0], ensure_ascii=False))
    check("★ 首日标为 first_day：无计划是设计（没有前置研究日），不是缺陷",
          dates[0]["has_plan"] is False and dates[0]["plan_missing_reason"] == "first_day",
          json.dumps(dates[0], ensure_ascii=False))
    check("每日带上动作条数与研究覆盖数",
          dates[1]["action_count"] == 2 and dates[1]["research_count"] == 2
          and dates[2]["action_count"] == 1 and dates[2]["research_count"] == 1,
          json.dumps(dates, ensure_ascii=False))
    check("有计划的日子 has_plan=True 且不标缺失原因",
          dates[1]["has_plan"] is True and dates[1]["plan_missing_reason"] == "",
          json.dumps(dates[1], ensure_ascii=False))
    check("研究缺口原样带出（不吞）", dates[2]["research_missing"] == ["159842.SZ"],
          str(dates[2]["research_missing"]))
    check("★ 有计划、没 step ⇒ executed=False（这一天没跑到）",
          [d["executed"] for d in dates] == [True, True, True, False],
          str([d["executed"] for d in dates]))
    check("成交笔数取自同日 step", dates[1]["trade_count"] == 1 and dates[2]["trade_count"] == 0,
          str([d["trade_count"] for d in dates]))
    check("status/error 原样带出（缺计划日可从 error 看出原因）",
          isinstance(dates[0]["status"], str) and isinstance(dates[0]["error"], str))

    # 别人的计划不能混进来
    session = db.get_session()
    try:
        other = db.User(phone=OTHER_PHONE, created_at=NOW, is_backtest=False)
        session.add(other)
        session.flush()
        other_id = int(other.id)
        session.add(db.TrustPlan(user_id=other_id, trade_date="2026-07-06",
                                 plan_json="{}", created_at=NOW))
        # 同一个影子名下、但落在**这个 run 窗口之外**的计划：影子账户跨 run 复用，
        # 不带窗口过滤的话它会被当成本次回测的计划渲染出来（而它可能来自更晚的运行）。
        session.add(db.TrustPlan(user_id=sh, trade_date="2026-07-20",
                                 plan_json=json.dumps({"actions": [action("600519.SH", "贵州茅台")]}),
                                 created_at=NOW))
        session.commit()
    finally:
        session.close()
    check("★ 只列自己的计划：别的用户的 TrustPlan（哪怕日期更早）不出现",
          [d["trade_date"] for d in bt.list_plan_reports(rid, owner)["dates"]]
          == [D0708, D0709, D0710, D0711],
          str([d["trade_date"] for d in bt.list_plan_reports(rid, owner)["dates"]]))
    check("★ 窗口外的计划不混进来（影子跨 run 复用，靠窗口收口）",
          "2026-07-20" not in [d["trade_date"] for d in bt.list_plan_reports(rid, owner)["dates"]])

    # ---- 2. 不存在的日期 ----
    section("2. 取一个没有计划的日期 → None（路由据此报「该日没有生成计划」）")
    check("该 run 的日期范围之内但没计划的那天也返回 None",
          bt.get_plan_report(rid, owner, D0708) is None)
    check("★ 窗口外的计划也取不到（与清单同一口径）",
          bt.get_plan_report(rid, owner, "2026-07-20") is None)
    check("范围之外的日期返回 None", bt.get_plan_report(rid, owner, "2026-01-01") is None)

    # ---- 3. 账户快照取自哪一天（本文件最重要的一组） ----
    section("3. 报告里的账户快照 = 计划生成那一刻（研究日收盘），不是回测结束时的账")
    r9 = bt.get_plan_report(rid, owner, D0709)
    check("★ 取的是生效日**之前**最后一个 step（07-08），不是生效日当天",
          r9["as_of"] == D0708 and r9["as_of_source"] == "step",
          f"{r9['as_of']} / {r9['as_of_source']}")
    check("★ 现金是研究日那天的现金（500000），不是回测结束时的 490037.55",
          r9["cash"] == 500000.0, str(r9["cash"]))
    check("★ 持仓是研究日那天的持仓（空仓）——否则会是「0 持仓却 2 条建仓动作」的自相矛盾",
          r9["positions"] == [], json.dumps(r9["positions"], ensure_ascii=False))
    check("总资产 = 现金 + 持仓市值", r9["total_assets"] == 500000.0, str(r9["total_assets"]))

    r10 = bt.get_plan_report(rid, owner, D0710)
    check("★ 换一天，快照跟着换到那一天的研究日（07-09，已持仓）",
          r10["as_of"] == D0709 and r10["cash"] == 490037.55
          and len(r10["positions"]) == 1,
          f"{r10['as_of']} / {r10['cash']} / {len(r10['positions'])}")
    check("★ 持仓不是最终状态：07-10 的价是 1.97，回测结束那天是 1.99",
          r10["positions"][0]["price"] == 1.97,
          str(r10["positions"][0].get("price")))

    # ---- 4. 渲染器要的 assets_ratio ----
    section("4. step 快照里没有 assets_ratio，取数时按同一口径补上（否则报告的「占比」列恒空）")
    p0 = r10["positions"][0]
    check("★ 占比 = 市值 / 总资产",
          abs(p0["assets_ratio"] - round(9653.0 / 499690.55, 6)) < 1e-9,
          str(p0.get("assets_ratio")))
    check("空仓时不会除零",
          bt.get_plan_report(rid, owner, D0709)["positions"] == [])

    # ---- 5. 研究快照 ----
    section("5. 研究：用计划里冻结的那份；没有冻结就如实说没有，**绝不回落实时查库**")
    check("有冻结快照时原样带出，并标明是冻结的",
          r9["research_frozen"] is True and set(r9["research"]) == {"161129.SZ", "518880.SH"},
          str(sorted(r9["research"])))
    check("冻结快照里的研究日一并带出（报告据此说「哪天的研究」）",
          r9["research"]["161129.SZ"]["trade_date"] == D0708)

    # 影子名下**有** EngineRun（跨研究日累积），计划里却没有冻结快照 —— 最危险的组合
    session = db.get_session()
    try:
        session.add(db.EngineRun(
            user_id=sh, ticker="000001.SZ", market="SZ",
            trade_date=D0711, report_json=json.dumps({"market": "更晚的研究"}, ensure_ascii=False),
            created_at=NOW,
        ))
        session.commit()
    except Exception as exc:  # EngineRun 的列很多，缺列就跳过这条（不让它变成假绿）
        print(f"  （跳过：造 EngineRun 失败 {type(exc).__name__}: {exc}）")
    finally:
        session.close()

    r11 = bt.get_plan_report(rid, owner, D0711)
    check("★ 没有冻结快照时 research 为空——不回落到「最近一次」EngineRun（那是未来函数）",
          r11["research"] == {} and r11["research_frozen"] is False,
          json.dumps(r11["research"], ensure_ascii=False))
    check("★ 且把这份计划涉及的标的全部列为「无研究依据」（如实说缺，不装作有）",
          r11["research_missing"] == ["000001.SZ"], str(r11["research_missing"]))

    # ---- 6. 越权 ----
    section("6. 越权：别人的 run 一律 ServiceError（回测表按 user_id 分区）")
    try:
        bt.list_plan_reports(rid, other_id)
        check("★ 别人的 run 取日期清单被拦下「, False, 」没抛异常")
    except ServiceError as exc:
        check("★ 别人的 run 取日期清单被拦下", "回测不存在" in str(exc), str(exc))
    try:
        bt.get_plan_report(rid, other_id, D0709)
        check("★ 别人的 run 取报告被拦下「, False, 」没抛异常")
    except ServiceError as exc:
        check("★ 别人的 run 取报告被拦下", "回测不存在" in str(exc), str(exc))
    try:
        bt.list_plan_reports(999999, owner)
        check("不存在的 run 报「回测不存在」「, False, 」没抛异常")
    except ServiceError as exc:
        check("不存在的 run 报「回测不存在」", "回测不存在" in str(exc), str(exc))

    # ---- 7. 渲染器：与实盘同一份 HTML ----
    section("7. 报告正文走实盘那个渲染器（格式一致靠「同一个函数」，不靠两边各写一遍）")
    html = plan_report.render_plan_report(
        r10["plan"], r10["positions"], r10["research"], r10["cash"],
        r10["trade_date"], r10["created_at"],
        research_missing=r10["research_missing"], research_frozen=r10["research_frozen"],
        banner="本报告来自历史回测 run #1（模拟撮合，非实盘成交）。",
    )
    check("标题与实盘报告逐字相同", "AI 托管 · 次日行动计划报告" in html)
    check("写的是计划自身的生效日", f"计划生效日：{D0710}" in html)
    check("★ 回测横幅在标题下方（没有它，回测报告落到人手里看不出不是实盘）",
          "本报告来自历史回测 run #1" in html and html.index("本报告来自历史回测")
          < html.index("一、所有持仓"), "横幅位置不对或缺失")
    check("缺口标的写进报告", "159842.SZ" in html)
    check("研究结论写进报告（冻结快照里的原文）", "600519.SH" in html)
    check("行动表里带上了动作与目标占比", "买入" in html and "8.0%" in html)

    # banner 必须是**纯叠加**：空串时实盘输出与加这个参数之前逐字相同
    base = plan_report.render_plan_report(
        r10["plan"], r10["positions"], r10["research"], r10["cash"],
        r10["trade_date"], r10["created_at"],
        research_missing=r10["research_missing"], research_frozen=r10["research_frozen"])
    check("★ banner 为空时输出与不带横幅完全一致（实盘路径零回归）",
          "<div class='warnbox'" not in base.split("一、所有持仓")[0],
          base[:400])
    check("★ 带 banner 的输出 == 空 banner 输出只多插了那一段（不夹带别的差异）",
          html == base.replace(
              "</header>\n",
              "</header>\n<div class='warnbox' style='margin:0 0 20px'>"
              "本报告来自历史回测 run #1（模拟撮合，非实盘成交）。</div>", 1),
          "横幅不是纯叠加")

    # ---- 8. 没有计划的回测 + 两个 run 之间不串 ----
    section("8. 一条计划都没有的回测：清单**不空**——列出它跑过的交易日并标明无计划")
    fresh_db()
    owner2 = owner_id_by_phone(OWNER_PHONE)
    sh2 = shadow_id(owner2)
    rid2 = make_run(owner2)
    put_step(rid2, D0708, cash=100000.0, positions=[])
    d2 = bt.list_plan_reports(rid2, owner2)["dates"]
    # 骨架是 step（这个 run 真实跑过的交易日），所以"跑了但一条计划都没有"不再是**空白**
    # 而是**一条明确的无计划记录**。空白让人以为页面坏了；标注无计划才说明环断在哪。
    check("★ 列出该 run 跑过的交易日（不是空列表、不是 None、不是报错）",
          [d["trade_date"] for d in d2] == [D0708], str(d2))
    check("★ 标为无计划，且原因如实（首日 = 没有前置研究日，属设计）",
          d2[0]["has_plan"] is False and d2[0]["plan_missing_reason"] == "first_day",
          json.dumps(d2[0], ensure_ascii=False))
    check("该 run 的任一日期取报告都是 None", bt.get_plan_report(rid2, owner2, D0708) is None)

    # 同一个库里再放一个**有计划的**别人，而且刻意让两份 run 撞在**同一个日期**上：
    # rid2 那天必须仍然是无计划。若按「影子 user + 日期」而非「run」取计划，这一条会立刻挂。
    owner3 = add_user(OTHER_PHONE)
    sh3 = shadow_id(owner3)
    rid3 = make_run(owner3, init_cash=300000.0)
    put_plan(sh3, D0708, actions=[action("600519.SH", "贵州茅台")], research={})
    d2b = bt.list_plan_reports(rid2, owner2)["dates"]
    check("★ 同一天、别人的 run 有计划，不会串进我的清单（回测按 user_id/影子分区）",
          [d["trade_date"] for d in d2b] == [D0708] and d2b[0]["has_plan"] is False,
          json.dumps(d2b, ensure_ascii=False))
    check("而那份计划在自己的 run 里看得见",
          [d["trade_date"] for d in bt.list_plan_reports(rid3, owner3)["dates"]] == [D0708]
          and bt.list_plan_reports(rid3, owner3)["dates"][0]["has_plan"] is True)

    # ---- 9. 起始状态兜底 ----
    section("9. 生效日之前一天都没跑过时，兜底用 run 的起始状态并如实标注来源")
    r0 = bt.get_plan_report(rid3, owner3, D0708)
    check("★ 没有前序 step 时 as_of_source=initial（前端据此不假装那是某天的收盘账）",
          r0["as_of_source"] == "initial" and r0["as_of"] == "",
          f"{r0['as_of_source']} / {r0['as_of']}")
    # init_basis 要起跑第一步才算得出来（未起跑是 0），所以兜底还得再退到 init_cash
    check("★ 兜底现金不会因为 init_basis 尚未算出就变成 0（退到 init_cash）",
          r0["cash"] == 300000.0, str(r0["cash"]))
    check("兜底持仓为空时不炸（init_positions 为空）", r0["positions"] == [])

    print("\n" + "=" * 60)
    if _FAILS:
        print(f"❌ {len(_FAILS)}/{_COUNT} 项未通过：")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {_COUNT} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
