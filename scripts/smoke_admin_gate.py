"""管理员门禁离线冒烟脚本（历史回测只对管理员开放）。

确定性、离线：**不连 Postgres、不连 Wind、不跑 LLM**——内存 SQLite + TestClient，
只验「谁能进、谁不能进」这一件事。

**为什么值得单独一个脚本**：这是仓里**第一处「准入」鉴权**。此前只有两种：
``auth.get_current_user``（登录就行）与 ``backtest._require_own``（只能碰自己的 run）。
新增的 ``auth.require_admin`` 是第三层，而它最怕的不是"拦不住"，是**漏掉一两条路由**
——11 条回测路由里漏一条，那条就是敞开的。所以下面有一条**逐条枚举**的断言，
路由清单变了就会红。

覆盖：
  1. 管理员放行 / 非管理员 403 / 没登录 401（顺序：先 401 后 403）
  2. **fail-closed**：ADMIN_PHONES 为空 ⇒ 谁都不是管理员 ⇒ 全 403
  3. 影子账户（is_backtest=True）进不来
  4. **回测路由逐条枚举**，没有一条漏网（清单在脚本里，加路由要同步）
  5. /api/backtest/capability 只对管理员开，且**不被 /{run_id} 吃掉**（真实返回 HTML）
  6. 两份 capability.html（docs/ 与 app_frontend/）逐字节相同
  7. /api/account 的 is_admin 真假两态

用法：
    .venv/bin/python scripts/smoke_admin_gate.py
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import auth, backtest, config, db, trust  # noqa: E402

# ---------------------------------------------------------------- 内存库
_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
db.Base.metadata.create_all(_engine)
db.SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)  # type: ignore[assignment]

# 掐掉 import app.main 时的启动副作用（main.py 在模块级调 _startup()）
db.init_db = lambda: None                              # type: ignore[assignment]
trust.start_scheduler = lambda: None                   # type: ignore[assignment]
backtest.resume_orphan_runs = lambda: []               # type: ignore[assignment]

from fastapi.testclient import TestClient  # noqa: E402

from app import main as main_mod  # noqa: E402

client = TestClient(main_mod.app)

# ---------------------------------------------------------------- 断言脚手架
_FAILS: list[str] = []
_COUNT = 0


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


# ---------------------------------------------------------------- 夹具
ADMIN_PHONE = "13800000001"   # 假号：断言的是"谁能进"，与真实号码无关；别把真号写进公开仓库
OTHER_PHONE = "13900000001"
SHADOW_PHONE = "bt-13800000001"   # 故意**包含**管理员手机号，证明拦的是 is_backtest 而不是"看不出来"

_TOKENS: dict[str, str] = {}


def seed() -> None:
    """建三个用户：管理员 / 普通用户 / 影子账户（手机号里嵌着管理员号）。"""
    session = db.get_session()
    try:
        for phone, is_bt in ((ADMIN_PHONE, False), (OTHER_PHONE, False), (SHADOW_PHONE, True)):
            u = db.User(phone=phone, created_at=0.0, is_backtest=is_bt)
            session.add(u)
            session.flush()
            session.add(db.TrustConfig(user_id=u.id, is_active=False, book_created=True,
                                       available_cash=0.0, stock_scope=1, updated_at=0.0))
            session.commit()
            _TOKENS[phone] = auth.create_session(u.id)
    finally:
        session.close()


def hdr(phone: str) -> dict:
    return {"Authorization": "Bearer " + _TOKENS[phone]}


# 11 条回测路由，逐条枚举（method, path, 是否需要 body）。
# ⚠️ 这张表就是"有没有漏"的判据本身 —— 新增回测路由时必须同步加进来。
BACKTEST_ROUTES = [
    ("get",    "/api/backtest/list", False),
    ("get",    "/api/backtest/preview", False),
    ("get",    "/api/backtest/capability", False),
    ("post",   "/api/backtest/run", True),
    ("get",    "/api/backtest/1", False),
    ("post",   "/api/backtest/1/cancel", False),
    ("get",    "/api/backtest/1/result", False),
    ("get",    "/api/backtest/1/trades", False),
    ("get",    "/api/backtest/1/monitor", False),
    ("get",    "/api/backtest/1/reports", False),
    ("get",    "/api/backtest/1/report", False),
    ("delete", "/api/backtest/1", False),
]


def call(method: str, path: str, headers: dict, body: bool = False):
    kw = {"headers": headers}
    if body:
        kw["json"] = {"start_date": "2026-03-02", "end_date": "2026-03-06"}
    return getattr(client, method)(path, **kw)


# ---------------------------------------------------------------- 1-3. 身份三态
def t_identity() -> None:
    section("1-3. 三类身份：管理员 / 普通用户 / 影子账户")
    config.ADMIN_PHONES = frozenset({ADMIN_PHONE})

    r = call("get", "/api/backtest/list", hdr(ADMIN_PHONE))
    check("★ 管理员 → 放行（200，不是 403）", r.status_code == 200, f"HTTP {r.status_code}")

    r = call("get", "/api/backtest/list", hdr(OTHER_PHONE))
    check("★ 普通用户 → 403", r.status_code == 403, f"HTTP {r.status_code}")
    check("  且 403 的说明是人话（不是裸 Forbidden）",
          "管理员" in (r.json().get("detail") or ""), str(r.json()))

    r = call("get", "/api/backtest/list", hdr(SHADOW_PHONE))
    check("★ 影子账户（is_backtest=True）→ 403，哪怕它的手机号里嵌着管理员号",
          r.status_code == 403, f"HTTP {r.status_code}")

    r = call("get", "/api/backtest/list", {})
    check("★ 没登录 → 401（**先判登录、后判准入**，不是 403）",
          r.status_code == 401, f"HTTP {r.status_code}")

    r = call("get", "/api/backtest/list", {"Authorization": "Bearer not-a-real-token"})
    check("★ 乱 token → 401", r.status_code == 401, f"HTTP {r.status_code}")


# ---------------------------------------------------------------- 4. fail-closed
def t_fail_closed() -> None:
    section("4. fail-closed：白名单为空 = 谁都不是管理员")
    config.ADMIN_PHONES = frozenset()
    r = call("get", "/api/backtest/list", hdr(ADMIN_PHONE))
    check("★ ADMIN_PHONES 为空 → 连这个手机号也 403（绝不「没配就放行」）",
          r.status_code == 403, f"HTTP {r.status_code}")

    r = call("get", "/api/account", hdr(ADMIN_PHONE))
    check("  /api/account 此时也报 is_admin=False（前端入口不会露出来）",
          r.status_code == 200 and r.json().get("is_admin") is False, str(r.status_code))

    config.ADMIN_PHONES = frozenset({ADMIN_PHONE})
    check("  白名单可动态生效（测试直接改 config，所以 is_admin 必须实时读它）",
          call("get", "/api/backtest/list", hdr(ADMIN_PHONE)).status_code == 200)


# ---------------------------------------------------------------- 5. 逐条枚举
def t_every_route() -> None:
    section("5. 逐条枚举：回测路由没有一条漏网（新增路由必须同步进这张表）")
    config.ADMIN_PHONES = frozenset({ADMIN_PHONE})
    leaked = []
    for method, path, body in BACKTEST_ROUTES:
        code = call(method, path, hdr(OTHER_PHONE), body).status_code
        if code != 403:
            leaked.append(f"{method.upper()} {path} → {code}")
    check(f"★ 非管理员访问全部 {len(BACKTEST_ROUTES)} 条回测路由都是 403",
          not leaked, "；".join(leaked))

    # 管理员那边不能一律 200（有的路由本就该 404/400），但**绝不能是 403**
    forbidden = []
    for method, path, body in BACKTEST_ROUTES:
        code = call(method, path, hdr(ADMIN_PHONE), body).status_code
        if code == 403:
            forbidden.append(f"{method.upper()} {path}")
    check("  管理员访问同样这些路由时**没有一条**是 403（准入放行了）",
          not forbidden, "；".join(forbidden))


# ---------------------------------------------------------------- 6. 报告资源
def t_capability() -> None:
    section("6. 能力评估报告：只对管理员开，且没被 /{run_id} 吃掉")
    config.ADMIN_PHONES = frozenset({ADMIN_PHONE})

    r = call("get", "/api/backtest/capability", hdr(OTHER_PHONE))
    check("★ 非管理员取报告 → 403", r.status_code == 403, f"HTTP {r.status_code}")

    r = call("get", "/api/backtest/capability", hdr(ADMIN_PHONE))
    check("★ 管理员取报告 → 200（不是被 /{run_id} 吃成 404/422）",
          r.status_code == 200, f"HTTP {r.status_code}")
    check("  且 Content-Type 是 text/html",
          "text/html" in (r.headers.get("content-type") or ""), r.headers.get("content-type"))
    check("  返回的确实是那份报告（不是别的页面）",
          "托管团队能力评估" in r.text or "价值评估" in r.text
          or "值不值得托付" in r.text, r.text[:80])

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    doc = os.path.join(root, "docs", "托管团队能力评估_v1.0.html")
    served = os.path.join(root, "app_frontend", "capability.html")
    check("两份 capability.html 都在", os.path.exists(doc) and os.path.exists(served),
          f"docs={os.path.exists(doc)} served={os.path.exists(served)}")
    if os.path.exists(doc) and os.path.exists(served):
        h1 = hashlib.md5(open(doc, "rb").read()).hexdigest()
        h2 = hashlib.md5(open(served, "rb").read()).hexdigest()
        check("★ 两份**逐字节相同**（改了 docs 那份而应用内不更新，这条就会红）",
              h1 == h2, f"{h1[:8]} vs {h2[:8]}")


# ---------------------------------------------------------------- 7. is_admin 两态
def t_account_flag() -> None:
    section("7. /api/account 的 is_admin 两态")
    config.ADMIN_PHONES = frozenset({ADMIN_PHONE})
    a = call("get", "/api/account", hdr(ADMIN_PHONE))
    b = call("get", "/api/account", hdr(OTHER_PHONE))
    check("★ 管理员 → is_admin=True", a.status_code == 200 and a.json().get("is_admin") is True,
          str(a.json().get("is_admin")))
    check("★ 普通用户 → is_admin=False", b.status_code == 200 and b.json().get("is_admin") is False,
          str(b.json().get("is_admin")))
    check("  该字段不影响原有口径（mirror / trust 仍在）",
          "mirror" in a.json() and "trust" in a.json(), str(sorted(a.json().keys()))[:120])


def main() -> int:
    print("管理员门禁离线冒烟（内存 SQLite + TestClient，不连 PG/Wind）")
    seed()
    t_identity()
    t_fail_closed()
    t_every_route()
    t_capability()
    t_account_flag()

    print("\n" + "=" * 60)
    if _FAILS:
        print(f"❌ {len(_FAILS)}/{_COUNT} 项未通过：")
        for f in _FAILS:
            print("   - " + f)
        return 1
    print(f"✅ 全部 {_COUNT} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
