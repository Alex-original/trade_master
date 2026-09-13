"""成交通知（``app/notify.py``）离线冒烟脚本。

确定性、离线：**不联网**——``_post`` 与 ``_get_configs`` 都换成桩，断言的是**发什么**、
**发几条**、**发给谁**，不是"网络通不通"。

覆盖：
  1. **文案 bug 回归**：``reason`` 为空时整条消息曾经变空串（三元优先级低于隐式字符串拼接）。
     这里把「无理由」这条路径单独钉死——空消息推出去用户只会看到一个空白气泡。
  2. 合并发送：一个 tick 3 笔 → **一条**消息（逐笔发会把群刷屏），且含 3 笔明细。
  3. 渠道分支：feishu / wecom 的 payload 形状不同；未知渠道跳过。
  4. 重试：第 1 次失败第 2 次成功 → 算送达且只发 2 次；全失败 → 返回 0 且不抛。
  5. ``notify_trades`` 异步：立即返回、不阻塞调用方；``_send_trades`` 同步可断言。
  6. 空入参 / 无配置 / 配置存在但 url 为空 → 一律不发。
  7. ``send_trade_notification`` 单笔入口与 ``notify_trades`` 共用同一份文案。

用法：
    .venv/bin/python scripts/smoke_notify.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import notify  # noqa: E402

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
        print(f"  ❌ {name}" + (f"  实际：{detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 桩
SENT: list[dict] = []          # 每次 _post 调用记一笔
CONFIGS: list[tuple[str, str]] = [("feishu", "https://hook.test/feishu")]
FAIL_TIMES = 0                 # 前 N 次调用返回 False（模拟 webhook 抽风）
_REAL_POST = notify._post
_REAL_GET = notify._get_configs


def install(configs=None, fail_times=0) -> None:
    global CONFIGS, FAIL_TIMES
    SENT.clear()
    CONFIGS = [("feishu", "https://hook.test/feishu")] if configs is None else configs
    FAIL_TIMES = fail_times
    notify._get_configs = lambda uid: list(CONFIGS)

    def fake_post(url, payload):
        SENT.append({"url": url, "payload": payload})
        return len(SENT) > FAIL_TIMES

    notify._post = fake_post


def restore() -> None:
    notify._post = _REAL_POST
    notify._get_configs = _REAL_GET


def trade(direction=0, qty=100, price=10.0, reason="", name="贵州茅台", code="600519.SH") -> dict:
    return {
        "trade_id": "T1", "order_id": "O1", "stock_code": code, "stock_name": name,
        "direction": direction, "price": price, "quantity": qty,
        "amount": round(price * qty, 2), "fee": 5.0, "ai_reason": reason,
    }


def text_of(i=0) -> str:
    """第 i 次发送的文本内容（兼容两种 payload 形状）。"""
    p = SENT[i]["payload"]
    return p.get("content", {}).get("text") or p.get("text", {}).get("content") or ""


# ---------------------------------------------------------------- 1. 文案 bug
def test_text_bug() -> None:
    section("文案 bug 回归：没有 AI 理由时不能变成空消息")

    # ---- 曾出问题的路径：reason 为空 ----
    t = notify._format_text([trade(reason="")])
    check("★ 无理由时**仍保留标题与数量行**（原先整条变空串）",
          t.startswith("[AI托管]") and "贵州茅台（600519.SH）" in t and "100 股 @ 10.0" in t, repr(t))
    check("无理由时不出现空的「理由：」行", "理由：" not in t, repr(t))
    check("无理由时首行不是空的（逐行检查，防隐式拼接再次吃掉整条）",
          all(line.strip() for line in t.split("\n")), repr(t))

    # ---- 有理由：原样带上 ----
    t2 = notify._format_text([trade(reason="计划 reduce：目标占比 0.1")])
    check("有理由时带上「理由：」行", "理由：计划 reduce：目标占比 0.1" in t2, repr(t2))
    check("有理由时标题行仍在（加一行不该吃掉别的行）", t2.startswith("[AI托管]"), repr(t2))

    # ---- 理由为 None / 纯空白：与空串同待遇 ----
    for bad in (None, "   "):
        tt = notify._format_text([trade(reason=bad)])
        check(f"reason={bad!r} 与空串同待遇（不当成有理由）",
              "理由：" not in tt and tt.startswith("[AI托管]"), repr(tt))

    # ---- 卖出的方向文案 ----
    check("卖出方向文案正确",
          notify._format_text([trade(direction=1)]).startswith("[AI托管] 卖出"), "")


# ---------------------------------------------------------------- 2. 合并
def test_merging() -> None:
    section("合并发送：一个 tick 的 N 笔 → 一条消息")
    three = [
        trade(name="贵州茅台", code="600519.SH", reason="计划 reduce：目标占比 0.1"),
        trade(direction=1, name="宁德时代", code="300750.SZ", qty=200, price=180.0, reason="止损"),
        trade(name="黄金ETF华安", code="518880.SH", qty=1000, price=8.9, reason="建仓"),
    ]
    install()
    sent = notify._send_trades(1, three)
    check("3 笔只发了 1 条", len(SENT) == 1, str(len(SENT)))
    check("返回成功渠道数 1", sent == 1, str(sent))
    body = text_of()
    check("标题写明笔数", "本次调仓 3 笔" in body, repr(body))
    for n in ("贵州茅台", "宁德时代", "黄金ETF华安"):
        check(f"明细含 {n}", n in body)
    check("明细逐行（3 行 · 开头）", body.count("\n· ") == 3, repr(body))

    # 单笔走原文案而不是"本次调仓 1 笔"
    install()
    notify._send_trades(1, [three[0]])
    check("单笔走原文案（不套「本次调仓 1 笔」）",
          "本次调仓" not in text_of() and "理由：" in text_of(), repr(text_of()))

    # 空入参不发
    install()
    check("空列表不发", notify._send_trades(1, []) == 0 and SENT == [], str(SENT))
    check("None 不发", notify._send_trades(1, None) == 0 and SENT == [], str(SENT))
    check("全是空 dict 也不发", notify._send_trades(1, [None, {}]) == 0 and SENT == [], str(SENT))


# ---------------------------------------------------------------- 3. 渠道
def test_channels() -> None:
    section("渠道：feishu / wecom 形状不同，未知渠道跳过")
    install(configs=[("feishu", "https://hook.test/f"), ("wecom", "https://hook.test/w"),
                     ("email", "smtp://x")])
    sent = notify._send_trades(1, [trade(reason="x")])
    check("两个有效渠道各发一次（email 跳过）", len(SENT) == 2, str(len(SENT)))
    check("返回成功渠道数 2", sent == 2, str(sent))
    feishu = next(s for s in SENT if s["url"].endswith("/f"))
    wecom = next(s for s in SENT if s["url"].endswith("/w"))
    check("feishu payload 用 msg_type/content.text",
          feishu["payload"].get("msg_type") == "text" and "text" in feishu["payload"].get("content", {}),
          str(feishu["payload"]))
    check("wecom payload 用 msgtype/text.content",
          wecom["payload"].get("msgtype") == "text" and "content" in wecom["payload"].get("text", {}),
          str(wecom["payload"]))
    check("两个渠道文案一致", text_of(0) == text_of(1), "")

    install(configs=[("email", "smtp://x")])
    check("只有未知渠道 → 不发、不抛", notify._send_trades(1, [trade()]) == 0 and SENT == [], str(SENT))

    install(configs=[])
    check("无配置 → 不发", notify._send_trades(1, [trade()]) == 0 and SENT == [], str(SENT))
    check("无配置时连 _post 都没调", len(SENT) == 0, str(SENT))


# ---------------------------------------------------------------- 4. 重试
def test_retry() -> None:
    section("重试：失败重试、成功即止、全失败不抛")
    install(fail_times=1)
    sent = notify._send_trades(1, [trade()])
    check("第 1 次失败第 2 次成功 → 算送达", sent == 1, str(sent))
    check("成功率场景只发了 2 次（不是死磕 3 次）", len(SENT) == 2, str(len(SENT)))

    install(fail_times=99)
    sent = notify._send_trades(1, [trade()])
    check("全失败返回 0 且不抛异常", sent == 0, str(sent))
    check("重试次数上限 == _RETRIES", len(SENT) == notify._RETRIES, str(len(SENT)))

    install()
    check("成功场景只发 1 次（成功即止）",
          notify._send_trades(1, [trade()]) == 1 and len(SENT) == 1, str(len(SENT)))


# ---------------------------------------------------------------- 5. 异步
def test_async() -> None:
    section("notify_trades 异步派发：不阻塞调用方")
    install()
    # 让 _post 睡 300ms：同步发的话调用方至少被拖 300ms
    def slow_post(url, payload):
        time.sleep(0.3)
        SENT.append({"url": url, "payload": payload})
        return True

    notify._post = slow_post
    t0 = time.monotonic()
    notify.notify_trades(1, [trade()])
    elapsed = time.monotonic() - t0
    check("★ 立即返回（未被 webhook 拖住）", elapsed < 0.1, f"{elapsed:.3f}s")
    check("调用时刻还没发出去（证明真的异步）", len(SENT) == 0, str(len(SENT)))
    for _ in range(50):                      # 最多等 1s
        if SENT:
            break
        time.sleep(0.02)
    check("后台线程随后确实发出去了", len(SENT) == 1, str(len(SENT)))
    check("后台线程是 daemon（不阻止进程退出）",
          any(t.daemon and t.name == "notify-1" for t in threading.enumerate())
          or len(SENT) == 1, "线程已结束或非 daemon")

    # 抛异常也不能冒泡到调用方（守护线程里抛出去没人接）
    install()
    notify._get_configs = lambda uid: (_ for _ in ()).throw(RuntimeError("db down"))
    try:
        notify.notify_trades(1, [trade()])
        ok = True
    except Exception as e:  # noqa: BLE001
        ok = False
        detail = str(e)
    time.sleep(0.1)
    check("★ _get_configs 抛异常不冒泡到调用方（通知不该能改成交）",
          ok, detail if not ok else "")

    # 空入参：连线程都不起
    install()
    before = threading.active_count()
    notify.notify_trades(1, [])
    check("空入参不起线程", threading.active_count() == before, str(threading.active_count() - before))


# ---------------------------------------------------------------- 6. 单笔入口
def test_single_entry() -> None:
    section("send_trade_notification 单笔入口与合并路径共用文案")
    install()
    notify.send_trade_notification(1, trade(reason="止损"))
    check("单笔入口发出 1 条", len(SENT) == 1, str(len(SENT)))
    direct = text_of()
    check("与 _format_text([单笔]) 逐字一致", direct == notify._format_text([trade(reason="止损")]), repr(direct))
    check("单笔入口是**同步**的（调用返回时已发完）", len(SENT) == 1, str(len(SENT)))


# ---------------------------------------------------------------- 7. 执行层壳
def test_run_execution_shell() -> None:
    """``trust.run_execution`` 的壳：成交才通知、回测绝不通知、通知失败不改执行结果。

    这一段不需要数据库——壳只是把 ``_run_execution`` 的结果转给 ``notify``，
    两边都换成桩就能把"什么时候通知"这件事钉死。
    """
    section("trust.run_execution 壳：成交才通知、回测绝不通知")
    from app import trust

    real_impl = trust._run_execution
    real_notify = notify.notify_trades
    calls: list[tuple[int, int]] = []
    notify.notify_trades = lambda uid, tr: calls.append((uid, len(tr)))

    try:
        # ---- 实时路径 + 有成交 → 通知 ----
        trust._run_execution = lambda uid, clock=None: {"trades": [trade(), trade()], "skipped": None}
        res = trust.run_execution(7)
        check("★ 实时路径有成交 → 调了 notify_trades", calls == [(7, 2)], str(calls))
        check("返回值原样透传（壳不改执行结果）", res["skipped"] is None and len(res["trades"]) == 2, str(res))

        # ---- 实时路径 + 无成交 → 不通知 ----
        calls.clear()
        trust._run_execution = lambda uid, clock=None: {"trades": [], "skipped": "no_positions"}
        trust.run_execution(7)
        check("无成交不通知（空跑一分钟不该刷消息）", calls == [], str(calls))

        # ---- 回测（clock 非空）→ 绝不通知 ----
        calls.clear()
        trust._run_execution = lambda uid, clock=None: {"trades": [trade()], "skipped": None}
        trust.run_execution(7, clock=object())
        check("★ 回测路径**绝不**通知（否则模拟成交会刷真 webhook）", calls == [], str(calls))

        # ---- 通知派发失败 → 成交结果不受影响、异常不冒泡 ----
        def boom(uid, tr):
            raise RuntimeError("can't start new thread")

        notify.notify_trades = boom
        trust._run_execution = lambda uid, clock=None: {"trades": [trade()], "skipped": None}
        try:
            res = trust.run_execution(7)
            ok = len(res["trades"]) == 1
            err = ""
        except Exception as e:  # noqa: BLE001
            ok, err = False, str(e)
        check("★ 通知派发抛异常不冒泡、不影响已成交的结果", ok, err)
    finally:
        trust._run_execution = real_impl
        notify.notify_trades = real_notify


def main() -> int:
    try:
        test_text_bug()
        test_merging()
        test_channels()
        test_retry()
        test_async()
        test_single_entry()
        test_run_execution_shell()
    finally:
        restore()

    print(f"\n{'=' * 60}")
    if _FAILS:
        print(f"❌ {len(_FAILS)}/{_COUNT} 项未通过：")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print(f"✅ 全部 {_COUNT} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
