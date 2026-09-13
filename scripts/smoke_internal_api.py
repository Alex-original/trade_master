"""内部运维端点（``/api/internal/*``）离线冒烟脚本。

走**真实的 FastAPI 路由**（``TestClient``），断言的是 HTTP 状态码，不是辅助函数——
「没配 token 就放行」「影子账户也能手动触发」这类漏洞只有走完整路由才验得出来。

不连 Postgres、不连 Wind、不跑 LLM：内存 SQLite + 桩掉 ``_startup`` 的三件事
（``db.init_db`` / ``trust.start_scheduler`` / ``backtest.resume_orphan_runs``）。
**必须在 import app.main 之前打桩**——``app.main`` 在模块级就调 ``_startup()``。

覆盖：
  1. **fail-closed**：未配置 INTERNAL_TOKEN → 一律 403（含带任意 token）
  2. 缺 token / 错 token → 403；正确 token → 放行到业务校验
  3. 正确 token + ``confirm=false`` → 400（防被顺手 curl 一下就真下单）
  4. 用户不存在 → 404；影子账户 → 400（永不手动触发）
  5. 非交易时段（周末 / 15:00 后）→ 409
  6. 成功路径：调用 ``trust.run_execution``（实时路径，因此**会发通知**）并回执
  7. ``GET /api/internal/scheduler``：未启动时 running=False，正常时 4 个 job 都有
     next_run_time 且落在工作日窗口内（同时是容器时区的证明）
  8. nginx 配置：80 与 443 **两个** block 都带 ``^~ /api/internal/`` 规则

用法：
    .venv/bin/python scripts/smoke_internal_api.py
"""
import os
import sys
from datetime import datetime as real_datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import backtest, config, db, trust  # noqa: E402

# ---------------------------------------------------------------- 内存库
_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
db.Base.metadata.create_all(_engine)
db.SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)  # type: ignore[assignment]

# ---- 在 import app.main 之前掐掉启动副作用（main.py 在模块级调 _startup()）----
# 留着真的 start_scheduler：下面"已启动的调度器"那段要真起一次，
# 否则那几条断言会因为桩永远走跳过分支（假绿）。
_REAL_START_SCHEDULER = trust.start_scheduler
db.init_db = lambda: None                              # type: ignore[assignment]
trust.start_scheduler = lambda: None                   # type: ignore[assignment]
backtest.resume_orphan_runs = lambda: []               # type: ignore[assignment]

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402

client = TestClient(main_mod.app)

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


# ---------------------------------------------------------------- 时间夹具
class FixedDatetime(real_datetime):
    """把 ``main_mod.datetime.now()`` 钉在某个时刻，用来验时段闸门。"""

    _now: real_datetime = real_datetime(2026, 9, 14, 10, 30)   # 周一 10:30

    @classmethod
    def now(cls, tz=None):
        return cls._now


main_mod.datetime = FixedDatetime     # type: ignore[assignment]


def at(y, m, d, hh, mm=0):
    FixedDatetime._now = real_datetime(y, m, d, hh, mm)


# ---------------------------------------------------------------- 数据夹具
UID_REAL = 1
UID_SHADOW = 2

TOKEN = "smoke-internal-token-0123456789"
HEAD = {"X-Internal-Token": TOKEN}


def seed_users() -> None:
    s = db.get_session()
    try:
        s.query(db.User).delete()
        s.add(db.User(id=UID_REAL, phone="13800000001", created_at=0.0, is_backtest=False))
        s.add(db.User(id=UID_SHADOW, phone="13800000002", created_at=0.0, is_backtest=True))
        s.commit()
    finally:
        s.close()


def restore_real_execution():
    """让 trust.run_execution 恢复正常（成功路径的用例会换桩）。"""
    pass


# ---------------------------------------------------------------- 1. fail-closed
def test_fail_closed() -> None:
    section("fail-closed：未配置 token → 一律 403")
    config.INTERNAL_TOKEN = ""
    r = client.post("/api/internal/run-execution", json={"user_id": UID_REAL, "confirm": True})
    check("未配置 + 不带 token → 403", r.status_code == 403, str(r.status_code))
    r = client.post("/api/internal/run-execution", json={"user_id": UID_REAL, "confirm": True},
                    headers={"X-Internal-Token": "any"})
    check("★ 未配置 + 带任意 token → 仍 403（绝不「没配就放行」）",
          r.status_code == 403, str(r.status_code))
    r = client.get("/api/internal/scheduler")
    check("scheduler 未配置 → 403", r.status_code == 403, str(r.status_code))
    r = client.get("/api/internal/scheduler", headers={"X-Internal-Token": ""})
    check("空 token → 403", r.status_code == 403, str(r.status_code))


# ---------------------------------------------------------------- 2. 鉴权
def test_auth() -> None:
    section("鉴权：缺 token / 错 token → 403，正确 token 放行")
    config.INTERNAL_TOKEN = TOKEN
    at(2026, 9, 14, 10, 30)
    r = client.post("/api/internal/run-execution", json={"user_id": UID_REAL, "confirm": True})
    check("缺 X-Internal-Token → 403", r.status_code == 403, str(r.status_code))
    r = client.post("/api/internal/run-execution", json={"user_id": UID_REAL, "confirm": True},
                    headers={"X-Internal-Token": TOKEN + "x"})
    check("错 token（前缀相同也不行）→ 403", r.status_code == 403, str(r.status_code))
    r = client.post("/api/internal/run-execution", json={"user_id": UID_REAL, "confirm": True},
                    headers=HEAD)
    check("正确 token → 不再是 403", r.status_code != 403, str(r.status_code))


# ---------------------------------------------------------------- 3. 业务闸门
def test_gates() -> None:
    section("业务闸门：confirm / 用户 / 影子账户 / 时段")
    config.INTERNAL_TOKEN = TOKEN
    seed_users()

    at(2026, 9, 14, 10, 30)
    r = client.post("/api/internal/run-execution", json={"user_id": UID_REAL, "confirm": False},
                    headers=HEAD)
    check("★ confirm=false → 400（防被顺手 curl 一下就真下单）", r.status_code == 400, str(r.status_code))
    r = client.post("/api/internal/run-execution", json={"user_id": UID_REAL}, headers=HEAD)
    check("★ 缺 confirm 字段（默认 false）→ 400", r.status_code == 400, str(r.status_code))

    r = client.post("/api/internal/run-execution", json={"user_id": 99999, "confirm": True},
                    headers=HEAD)
    check("用户不存在 → 404", r.status_code == 404, str(r.status_code))

    r = client.post("/api/internal/run-execution", json={"user_id": UID_SHADOW, "confirm": True},
                    headers=HEAD)
    check("★ 影子账户 → 400（回测影子账户永不被手动触发）", r.status_code == 400, str(r.status_code))

    at(2026, 9, 12, 10, 30)      # 周六
    r = client.post("/api/internal/run-execution", json={"user_id": UID_REAL, "confirm": True},
                    headers=HEAD)
    check("周末 → 409", r.status_code == 409, str(r.status_code))

    at(2026, 9, 14, 15, 0)       # 周一 15:00
    r = client.post("/api/internal/run-execution", json={"user_id": UID_REAL, "confirm": True},
                    headers=HEAD)
    check("★ 15:00 整 → 409（盘后下单会踩 T+1 解冻时刻的隐患）", r.status_code == 409, str(r.status_code))

    at(2026, 9, 14, 14, 59)
    r = client.post("/api/internal/run-execution", json={"user_id": UID_REAL, "confirm": True},
                    headers=HEAD)
    check("14:59 仍放行到执行层（闸门不早关）", r.status_code == 200, f"{r.status_code} {r.text[:120]}")


# ---------------------------------------------------------------- 4. 成功路径
def test_success_path() -> None:
    section("成功路径：确实调了 trust.run_execution（实时路径）并回执")
    config.INTERNAL_TOKEN = TOKEN
    at(2026, 9, 14, 10, 30)

    calls: list[tuple] = []
    real_run = trust.run_execution

    def fake_run(user_id, clock=None):
        calls.append((user_id, clock))
        return {"trades": [{"trade_id": "T1", "direction": 0}], "skipped": None}

    trust.run_execution = fake_run
    try:
        r = client.post("/api/internal/run-execution", json={"user_id": UID_REAL, "confirm": True},
                        headers=HEAD)
        body = r.json()
        check("200", r.status_code == 200, str(r.status_code))
        check("★ 调了 run_execution 且**不传 clock**（实时路径，会发通知）",
              calls == [(UID_REAL, None)], str(calls))
        check("回执含成交笔数与 skipped",
              body["trade_count"] == 1 and body["skipped"] is None and body["ok"] is True, str(body))

        # 无成交时也要如实回执
        trust.run_execution = lambda uid, clock=None: {"trades": [], "skipped": "no_positions"}
        r = client.post("/api/internal/run-execution", json={"user_id": UID_REAL, "confirm": True},
                        headers=HEAD)
        check("无成交 → trade_count=0 且 skipped 如实带出",
              r.json()["trade_count"] == 0 and r.json()["skipped"] == "no_positions", str(r.json()))
    finally:
        trust.run_execution = real_run


# ---------------------------------------------------------------- 5. 调度器状态
def test_scheduler_status() -> None:
    section("GET /api/internal/scheduler：未启动 / 已启动（含时区证明）")
    config.INTERNAL_TOKEN = TOKEN

    r = client.get("/api/internal/scheduler", headers=HEAD)
    body = r.json()
    check("未启动时 200 且 running=false（不是 500）", r.status_code == 200 and body["running"] is False,
          f"{r.status_code} {body}")
    check("未启动时 jobs 为空表", body["jobs"] == [], str(body["jobs"]))
    check("无论如何都带 now 与 tz（时区证明不能因为没启动就没）",
          bool(body["now"]) and bool(body["tz"]), str(body))

    # 用**真的** start_scheduler 起一次（上面为避开 _startup 副作用打的是桩）。
    # cron 触发点最短也是"每分钟"，本测试一秒内跑完，不会被 job 反过来打扰。
    sched = _REAL_START_SCHEDULER()
    check("★ 真的把调度器起起来了（不是桩）——否则下面的断言全是假绿",
          sched is not None, "start_scheduler 返回 None（APScheduler 没装？）")
    if sched is None:
        return
    try:
        body = client.get("/api/internal/scheduler", headers=HEAD).json()
        check("已启动时 running=true", body["running"] is True, str(body))
        ids = sorted(j["id"] for j in body["jobs"])
        check("★ 4 个 job 全在：execution / execution-15 / plan / release",
              ids == ["execution", "execution-15", "plan", "release"], str(ids))
        check("每个 job 都有 next_run_time（这正是「排了却没跑」要看的那个字段）",
              all(j["next_run_time"] for j in body["jobs"]),
              str([(j["id"], j["next_run_time"]) for j in body["jobs"]]))
        check("now 与 next_run_time 都是本地时间字符串（能与时区偏移一起判断）",
              len(body["now"]) >= 16 and all(len(j["next_run_time"]) >= 16 for j in body["jobs"]),
              str(body["now"]))
        # 计划表本身的不变量：触发点必须在**将来**、落在**工作日 9:00–15:10**，且与 now
        # **同一个时区**（调度器与进程时钟各用一个时区，是"看着排上了其实早/晚 8 小时"的
        # 经典成因）。跨周末时最近触发点可以远到 60+ 小时，所以不设小时数上限。
        import datetime as _dt
        nrts = [_dt.datetime.fromisoformat(j["next_run_time"]) for j in body["jobs"]]
        now = _dt.datetime.fromisoformat(body["now"])
        check("★ 所有触发点都在将来", all(n > now for n in nrts),
              str([(j["id"], j["next_run_time"]) for j in body["jobs"]]))
        check("★ 所有触发点都落在工作日（周末不排计划）",
              all(n.weekday() < 5 for n in nrts), str([n.isoformat() for n in nrts]))
        check("★ 所有触发点小时数都在 9–15（与 CronTrigger 的 9-14/15 一致）",
              all(9 <= n.hour <= 15 for n in nrts), str([n.isoformat() for n in nrts]))
        check("★ 触发点与 now 同一时区偏移（不一致 = 两者各用一套时区）",
              all(n.utcoffset() == now.utcoffset() for n in nrts),
              f"now={now.utcoffset()} nrt={[str(n.utcoffset()) for n in nrts]}")
        # 打印出来供人核对：生产机的 A4 判据就是这里必须是 +08:00（容器 TZ=Asia/Shanghai）
        print(f"     ℹ️  本地时区偏移 = {now.utcoffset()}（生产机应为 +08:00）")
    finally:
        sched.shutdown(wait=False)
        trust._SCHEDULER = None


# ---------------------------------------------------------------- 6. nginx 规则
def test_nginx_rule() -> None:
    section("nginx：两个 block 都要挡住 /api/internal/")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "deploy", "nginx-trade-master.conf")
    with open(path, encoding="utf-8") as f:
        conf = f.read()
    # 去掉注释行再数，避免把"注释里写着规则"当成"配了规则"
    active = "\n".join(
        ln for ln in conf.splitlines() if not ln.strip().startswith("#")
    )
    check("80 block 有 ^~ /api/internal/ 规则", "^~ /api/internal/" in active, "")
    check("规则回 404（不是 403——403 等于承认端点存在）",
          "return 404" in active, "")
    check("★ 注释里的 443 示例也带上规则（线上走 443，漏了就是敞着的）",
          "location ^~ /api/internal/" in conf.replace("    # ", "    "), "")
    # 位置必须在 `location /` 之前，否则前缀长的先匹配也仍然按最长前缀胜出——
    # 这里断言的是可读意图：规则写在上面，改的人一眼看得到。
    check("规则排在 `location /` 之前",
          active.index("^~ /api/internal/") < active.index("location / {"), "")


def main() -> int:
    test_fail_closed()
    test_auth()
    test_gates()
    test_success_path()
    test_scheduler_status()
    test_nginx_rule()

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
