"""回测数据装载：把一段历史行情变成 ``{date: Bar}``，并守住两个口径。

**为什么单独一个模块**：回测里"价格从哪来"必须只有一个答案。这个模块是回测价格数据的
**唯一入口**——撮合、盯市、基准全从它拿 bar，不再有第二条取数路径。实时的价格入口是
``account.get_quotes``，两者由 ``market_clock`` 统一成同一种输入（``Bar``）。

两个口径在这里定死：

1. **复权 = 后复权**（``aftype=1``，由 ``get_wind_ohlcv`` 在 as-of 作用域内默认）。前复权
   以"今天"为基准回溯重算历史价，会随未来的分红送转变化 —— 那是偷看未来。后复权的复权
   因子只依赖 t 之前的除权事件，构造上无未来函数，且区间收益已隐含分红再投资。
   ⚠️ **这条只管个股线**：指数 K 线接口没有 ``aftype`` 参数（见 ``_benchmark_frame``），
   基准是按收益率归一化后使用的**相对**参照，不是绝对价位对照。

2. **量比 = 日线代理量比**（``vol[i] / mean(vol[i-5:i])``）。Wind 没有可寻址的历史量比
   接口。⚠️ 这个口径**用到当日全天成交量**，含日内事后信息，会让 ``volume_ratio_min``
   档**更容易被满足**——所以它出现在结果页的声明里，不能悄悄用。

停牌的处理见 ``build_bars_from_frame``：**顺延最后有效收盘，而不是省略或归零**。

3. **取到的日线落盘缓存**（见本节末的"缓存"一段）。回测动辄几十只票、单只一次 Wind 调用，
   而"同一区间反复跑"是试点阶段的常态（先 10 天、再 30 天、改了起始资金再跑一遍）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path

import pandas as pd

from app.market_clock import Bar
from tradingagents.asof import current_asof
from tradingagents.dataflows import wind as _wind
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorError,
    VendorRateLimitError,
    VendorRejectedError,
)

logger = logging.getLogger(__name__)

#: 基准指数：沪深300。既当"这段行情本身如何"的对照，也当**交易日历**——
#: 它每个 A 股交易日都有 K 线，比拿单只个股当日历稳（个股会停牌）。
BENCHMARK_CODE = "000300.SH"

#: 代理量比的回看窗口（前 N 个交易日）。与 Wind 快照的口径（过去 5 日每分钟均量）
#: 在"5 日"这个量级上对齐，但**不是同一个东西**：一个是日内分钟量之比，一个是日线量之比。
_VOL_LOOKBACK = 5

#: 取数时在窗口两端各留的余量（自然日）。左端保证第一天的代理量比有 5 日均量可用，
#: 右端是保险（end_date 本来就被 as-of 封顶）。
_PAD_DAYS = 12


# ---------------------------------------------------------------- 交易日历


def trading_days(start_date: str, end_date: str) -> list[str]:
    """区间内的 A 股交易日（``YYYY-MM-DD`` 升序），取自基准指数的 K 线日期。

    用指数的日期而不是"工作日"：工作日会把春节、国庆算进来，凭空多出没有行情的日子，
    而那些日子的 bar 全是停牌顺延 —— 等于让团队在假期里"交易"。也不拿个股当日历：
    个股停牌时那段日期就消失了，整个回测时间轴会跟着错位。
    """
    df = _benchmark_frame(start_date, end_date)
    return [str(d)[:10] for d in df["Date"]]


def _benchmark_frame(start_date: str, end_date: str) -> pd.DataFrame:
    """基准指数日线。失败时抛 ``NoMarketDataError``——**没有交易日历就没法回测**，
    这里不能降级（降级会得到一个空时间轴，然后静默"跑完"一次什么都不做的回测）。

    也走缓存：一次 run 里 ``trading_days`` 与 ``load_benchmark`` 会拿**完全相同的参数**
    各查一次，加上预览探数就是三次。指数线只有一个标的，省下的量不大，但它是最先被调用、
    最不能慢的一步——它挂了整个回测连交易日历都没有。
    """
    df, _hit = _fetch_cached(
        BENCHMARK_CODE, start_date, end_date, "idx",
        lambda: _with_retry(
            lambda: _wind.get_index_ohlcv(BENCHMARK_CODE, start_date, end_date),
            what=f"基准指数 {BENCHMARK_CODE}",
        ),
        adj="na",  # 指数 K 线接口没有 aftype，它是价格指数——缓存名上不许标 hfq
    )
    return df


def load_benchmark(start_date: str, end_date: str) -> dict[str, float]:
    """基准收盘序列 ``{date: close}``。

    ⚠️ **不要把这条线和策略说成"同一复权口径"**：指数 K 线接口（``get_index_ohlcv``）
    根本没有 ``aftype`` 参数，它是价格指数、无所谓复权。沪深300 是分红除息后自然回落的，
    而策略那条线隐含分红再投资——两者**不是**可比的总收益口径。

    所以基准只按**收益率归一化后**使用（起点都归到 ``init_basis``），回答的是"这段行情
    本身涨跌如何"，不是绝对价位对照。主基准始终是等权买入持有（``equal_weight_baseline``），
    它和策略共用同一套手数与费用模型，差异只剩"团队决策"本身。这一条写进 §3.2 与结果页声明。
    """
    df = _benchmark_frame(start_date, end_date)
    return {str(r["Date"])[:10]: float(r["Close"]) for _, r in df.iterrows()}


# ---------------------------------------------------------------- 取数（带退避）


#: Wind 明确说了"过会儿再来"的拒绝原文。这类**可以**重试——和"额度用完"（要等次日）
#: 是两回事，判据是**重试有没有可能成功**，不是错误文本里有没有限流字样。
_TRANSIENT_REJECT_MARKS = ("服务暂时不可用", "请稍后重试", "系统繁忙", "服务繁忙", "稍后再试")


def _is_retryable(e: Exception) -> bool:
    """这个取数失败值得再试一次吗？"""
    if isinstance(e, VendorRateLimitError):
        return True
    # 额度耗尽的原文里也有"次数超限"，但它**不该**重试：额度次日才重置，退避只是白等。
    # 所以这里只认"服务端说它自己暂时不行"的那几个词。
    return isinstance(e, VendorRejectedError) and any(
        m in str(e) for m in _TRANSIENT_REJECT_MARKS
    )


def _with_retry(fn, what: str, attempts: int = 3, base_delay: float = 2.0):
    """对**可能成功的失败**重试；其余错误直接抛。

    回测要一次性拉几十只票的日线，撞上限流、或碰上 Wind 自己抖一下都是常事——
    实测 24 只里就有 1 只返回「服务暂时不可用，请稍后重试」。这两种都值得再试一次。

    其余错误重试没有意义，只会把失败拖慢：代码写错、这只票确实没行情、
    以及**每日额度耗尽**（次日才重置，等 2 秒、4 秒、8 秒全是白等）。
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except (VendorRateLimitError, VendorRejectedError) as e:
            if not _is_retryable(e):
                raise
            last = e
            delay = base_delay * (2**i)
            logger.warning("%s 取数失败（%s），%.1fs 后重试（%d/%d）",
                           what, type(e).__name__, delay, i + 1, attempts)
            time.sleep(delay)
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------- 落盘缓存


#: 落盘目录。``data/`` 已在 ``.gitignore`` 里（与 ``data/sample_report.json`` 同处），
#: 缓存不进版本库。
_CACHE_DIR = Path(os.getenv(
    "BACKTEST_BARS_CACHE_DIR",
    str(Path(__file__).resolve().parent.parent / "data" / "bars_cache"),
))

#: 缓存格式版本。**改动 ``build_bars_from_frame`` 或 ``get_wind_ohlcv`` 的任何取数口径
#: （量比窗口、停牌顺延、复权方式、列名）都必须 +1**：旧文件会被原样读回来当新口径用，
#: 而结果页上完全看不出来。加了版本号，升级就是换一批文件名，旧的自然失效。
_CACHE_VERSION = "v1"

#: 缓存有效期（天）。后复权 + 已收盘区间在构造上是**不可变**的（复权因子只依赖 t 之前的
#: 除权事件），所以本不需要 TTL；留着是为了兜住 Wind 侧的数据修错。设 0 = 永不失效。
_CACHE_TTL_DAYS = int(os.getenv("BACKTEST_BARS_CACHE_TTL_DAYS", "30"))


def _cache_file(code: str, pad_start: str, end_date: str, kind: str, adj: str) -> Path:
    """缓存文件名。**复权口径与数据种类必须进文件名**（``adj`` / ``kind``）。

    ``adj`` 是复权口径标签，今天会落盘的有三个值：个股线的 ``"hfq"``（后复权，撮合/盯市用）、
    ``"raw"``（不复权，**只为算复制持仓的复权因子**，见 ``load_unadjusted_closes``）和指数线的
    ``"na"``（无复权概念）——缓存只在 as-of 作用域内启用（见 ``_fetch_cached``）。但这个
    标签不能省：

    - **在哈希键里**：前复权与后复权的价位被每只票的常数缩放隔开，**混用不会报错，
      只会静默算错收益**，所以两者必须落在不同的文件上。将来谁把实时路径也接上缓存，
      这一位是唯一拦得住它的东西。
    - **在文件名里**：同样是``hfq``，写出来和不写出来的区别是"打开缓存目录一眼看懂"
      和"从哈希里反推"。缓存目录是给人排查用的——所以指数线标 ``na``、不标 ``hfq``。

    ``kind`` 同理，隔离个股线与指数线（两者形状相同、语义不同）。
    """
    safe = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in code)[:40]
    key = f"{_CACHE_VERSION}|{kind}|{code}|{pad_start}|{end_date}|{adj}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return _CACHE_DIR / (
        f"{kind}_{adj}_{safe}_{pad_start}_{end_date}_{_CACHE_VERSION}_{digest}.json"
    )


def _read_cache(path: Path) -> pd.DataFrame | None:
    """命中就返回与 ``get_wind_ohlcv`` 同形状的 frame；**任何异常都按未命中处理**。

    缓存是纯加速层，坏文件绝不能让回测失败——大不了重取一次。
    """
    try:
        if not path.exists():
            return None
        if _CACHE_TTL_DAYS > 0 and (time.time() - path.stat().st_mtime) > _CACHE_TTL_DAYS * 86400:
            return None
        df = pd.read_json(path, orient="split")
    except Exception as e:  # noqa: BLE001
        logger.warning("回测行情缓存读取失败（按未命中处理）%s：%s", path.name, e)
        return None
    if df is None or df.empty or "Close" not in df.columns:
        return None
    # 与 ``get_wind_ohlcv`` 的收尾保持一致：Date 归一成 datetime64、升序、去空。
    # 不依赖 pandas 反序列化出来的 dtype（它随版本而变），显式归一最稳。
    try:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date", "Close"]).sort_values("Date").reset_index(drop=True)
    except Exception:  # noqa: BLE001
        return None
    return df if not df.empty else None


def _write_cache(path: Path, df: pd.DataFrame) -> None:
    """只在**取到了真数据**时落盘。

    **空 frame 一律不写**：空可能是"这只票确实没有行情"，也可能是一次瞬时故障
    （限流被我们吞了、指标改了名）。缓存下来会让同一只票在 TTL 内被永久判成"无行情"，
    而用户看到的是一个标的数对不上、却查不出原因的回测。宁可下次再打一次接口。

    写临时文件再 ``replace`` —— 两个进程同时取同一只票时，读者不会看到半截 JSON。
    """
    if df is None or df.empty:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        df.to_json(tmp, orient="split", date_format="iso")
        tmp.replace(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("回测行情缓存写入失败（不影响本次取数）%s：%s", path.name, e)


def _fetch_cached(code: str, pad_start: str, end_date: str, kind: str, fetch,
                  adj: str = "hfq"):
    """带缓存的取数：命中就返回，未命中就 ``fetch()`` 并落盘。返回 ``(df, hit)``。

    **只在 as-of 作用域内启用缓存**——这是整个缓存设计的承重墙。理由：

    - 实时路径默认**前复权**（``aftype=0``），它的历史价会随未来的分红送转被重算。
      缓存它，就等于拿一份"昨天算的历史价"当今天的历史价，而且复权基准还会漂移。
    - 回测路径的个股线是后复权，在构造上不可变，缓存是安全的。

    所以判据直接用 ``current_asof()``：**没有 as-of 就完全不碰缓存，连读都不读**，
    实时取数的行为（含请求参数）与本模块加缓存之前**逐字节相同**。

    有效结束日取 ``min(end_date, asof)``：``get_wind_ohlcv`` 在 as-of 作用域内会把
    end_date 封顶到 asof，缓存键必须用**封顶后**的日期，否则两个 as-of 不同的查询
    会共用一份键。

    ``adj`` 是复权口径标签，默认 ``"hfq"``（后复权）——**个股线才是这个口径**。指数线必须
    显式传 ``"na"``：指数 K 线接口根本没有 ``aftype`` 参数（``wind.get_index_ohlcv``），
    它是价格指数、无所谓复权。``kind`` 已经把 ``idx``/``stk`` 隔开，所以这只影响可读性——
    但缓存目录是给人排查用的，**不许在文件名上撒谎**。
    """
    asof = current_asof()
    if not asof:
        return fetch(), False
    eff_end = min(end_date, asof)
    path = _cache_file(code, pad_start, eff_end, kind, adj=adj)
    cached = _read_cache(path)
    if cached is not None:
        return cached, True
    df = fetch()
    _write_cache(path, df)
    return df, False


def load_bars(
    codes: list[str],
    days: list[str],
    start_date: str,
    end_date: str,
    on_progress=None,
) -> tuple[dict[str, dict[str, Bar]], list[str]]:
    """批量取日线 → ``({code: {date: Bar}}, warnings)``。

    单只失败**不中断整轮回测**：记进 warnings，该票在回测里就是"全程无行情"（执行层跳过、
    持仓不盯市）。反过来，因为一只票取不到就整个回测失败，会让一次十小时的运行白跑。

    ⚠️ **但"不中断"不等于"不告知"**：调用方 ``_drive`` 会拿 warnings 做一次完整性门禁
    （缺得太多就停下问人），结果页也必须把它渲染出来。否则用户看到"标的 45 只"、
    实际只有 40 只在跑，是**静默的样本替换**。

    ``on_progress(done, total, code)`` 可选，逐只回调。
    """
    out: dict[str, dict[str, Bar]] = {}
    warnings: list[str] = []
    pad_start = (pd.Timestamp(start_date) - pd.Timedelta(days=_PAD_DAYS)).strftime("%Y-%m-%d")
    total = len(codes)
    for i, code in enumerate(codes, 1):
        try:
            df, _hit = _fetch_cached(
                code, pad_start, end_date, "stk",
                lambda c=code: _with_retry(
                    lambda: _wind.get_wind_ohlcv(c, pad_start, end_date), what=c
                ),
            )
        except (NoMarketDataError, VendorError) as e:
            warnings.append(f"{code}：历史行情取不到（{type(e).__name__}: {e}），该标的全程不参与")
            logger.warning("回测取数失败 %s: %s", code, e)
            if on_progress is not None:
                on_progress(i, total, code)
            continue
        except Exception as e:  # noqa: BLE001 —— 单只的意外失败同样不该拖垮整轮
            warnings.append(f"{code}：历史行情取数异常（{type(e).__name__}: {e}），该标的全程不参与")
            logger.exception("回测取数异常 %s", code)
            if on_progress is not None:
                on_progress(i, total, code)
            continue
        bars = build_bars_from_frame(df, days)
        if not bars:
            warnings.append(f"{code}：区间内无任何行情，该标的全程不参与")
        out[code] = bars
        if on_progress is not None:
            on_progress(i, total, code)
    return out, warnings


def load_unadjusted_closes(
    codes: list[str],
    start_date: str,
    end_date: str,
    on_progress=None,
) -> tuple[dict[str, dict[str, float]], list[str]]:
    """批量取**不复权**日线 → ``({code: {date: close}}, warnings)``。

    **唯一用途**：复制起始持仓时算复权因子。回测全空间是后复权（``aftype=1``），而真实
    托管簿的成本价是**不复权**盘面价 —— 把两者放进同一个持仓行，任何"现价 vs 成本"的
    比较都失去意义。最刺眼的后果是止损判据 ``bar.low < cost × (1−pct)`` 对复权因子 ≠1 的票
    **恒为真**：实测 159300.SZ 在 09-08 被按后复权价清仓，真实市值 51043 元的持仓只换回
    14414 元 —— 回测凭空少掉 36629 元（−71.8%），且这笔钱立刻变成现金参与其后所有决策。
    所以复制持仓必须按 ``f = 后复权价 / 不复权价`` 换算成后复权口径（见
    ``backtest._rescale_copied_book``）。

    窗口与个股线的 ``load_bars`` **完全一致**（同一个 ``pad_start``、同样按 as-of 封顶），
    只是 ``aftype=2`` 且缓存标签为 ``"raw"`` —— 两条线因此落在各自的缓存文件里，
    ``scripts/warm_bars_cache.py`` 与 ``scripts/verify_bars_cache.py`` 能一对一做掉。

    ⚠️ **不要拿它当第二条行情入口**：撮合、盯市、基准一律走后复权那条路。这个函数只喂
    复权因子的换算，返回值里没有任何 ``Bar``。

    单只失败与 ``load_bars`` 同策：记 warning、不中断。**但调用方不许把失败当 f=1 用**——
    详见 ``_rescale_copied_book`` 里为什么那必须是一个硬错误。
    """
    out: dict[str, dict[str, float]] = {}
    warnings: list[str] = []
    pad_start = (pd.Timestamp(start_date) - pd.Timedelta(days=_PAD_DAYS)).strftime("%Y-%m-%d")
    total = len(codes)
    for i, code in enumerate(codes, 1):
        try:
            df, _hit = _fetch_cached(
                code, pad_start, end_date, "stk", adj="raw",
                fetch=lambda c=code: _with_retry(
                    lambda: _wind.get_wind_ohlcv(c, pad_start, end_date, aftype=2), what=c
                ),
            )
        except (NoMarketDataError, VendorError) as e:
            warnings.append(f"{code}：不复权行情取不到（{type(e).__name__}: {e}）")
            logger.warning("回测不复权取数失败 %s: %s", code, e)
            if on_progress is not None:
                on_progress(i, total, code)
            continue
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{code}：不复权行情取数异常（{type(e).__name__}: {e}）")
            logger.exception("回测不复权取数异常 %s", code)
            if on_progress is not None:
                on_progress(i, total, code)
            continue
        closes = closes_from_frame(df)
        if closes:
            out[code] = closes
        else:
            warnings.append(f"{code}：区间内无任何不复权收盘价")
        if on_progress is not None:
            on_progress(i, total, code)
    return out, warnings


# ---------------------------------------------------------------- 纯函数：DataFrame → Bar


def closes_from_frame(df: pd.DataFrame) -> dict[str, float]:
    """Wind 日线 → ``{date: 收盘价}``。纯函数、无 I/O。

    只保留**严格为正**的收盘价：0 或负是坏数据（未上市占位、接口异常），留着会让后面的
    复权因子算出 0 或负数，而那种数会以"价格全错"的形式静默扩散。``load_unadjusted_closes``
    与 ``scripts/verify_bars_cache.py`` 共用它——断网验收要能与取数路径**逐字节**对比，
    两边各写一份转换就等于让那条断言失效。
    """
    out: dict[str, float] = {}
    for _, r in df.iterrows():
        try:
            d = pd.Timestamp(r["Date"]).strftime("%Y-%m-%d")
            c = float(r["Close"])
        except Exception:  # noqa: BLE001
            continue
        if c > 0:
            out[d] = c
    return out


def build_bars_from_frame(df: pd.DataFrame, days: list[str]) -> dict[str, Bar]:
    """Wind 日线 + 交易日列表 → ``{date: Bar}``。**纯函数、无 I/O**，便于离线断言。

    三件事：

    1. **代理量比** ``vol[i] / mean(vol[i-5:i])``，在**该标的自己的交易日序列**上算
       （不是在日历上）——停牌日没有成交量，混进均量会把量比算歪。前 5 个交易日为 None。
       哪一天算作 ``i`` 是相对整个取数窗口而言的，所以窗口左端留了 ``_PAD_DAYS`` 余量，
       好让区间第一天的量比也算得出来。

    2. **停牌顺延**：某日无 bar（停牌/未上市）时，若此前已有有效收盘，就生成一个
       ``tradable=False``、四价与 ``prev_close`` 全取最后有效收盘的 bar。**不能省略、更不能
       归零**：省略会让持仓在账户页变成"无价 → 市值 0"，归零会把浮亏算成 -100%，
       两者都会让执行层看到错误的仓位占比，进而做出错误的减仓决策。可交易与否由
       ``tradable`` 表达，与"值多少钱"是两件事。

    3. **上市前不生成 bar**：在该标的首根 K 线之前的日子直接不放进结果——那时它根本不存在，
       持仓里也不可能有它。调用方 ``bars.get(code)`` 得到 None，自然跳过。
    """
    if df is None or df.empty:
        return {}

    rows: list[tuple[str, float, float, float, float, float]] = []
    for _, r in df.iterrows():
        d = str(r["Date"])[:10]
        try:
            o, h, low, c = (float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]))
            vol = float(r["Volume"]) if "Volume" in df.columns and pd.notna(r["Volume"]) else 0.0
        except (TypeError, ValueError, KeyError):
            continue
        if c != c or o != o:  # NaN 自查（不依赖 pandas 的 isna，避免 object 列踩坑）
            continue
        rows.append((d, o, h, low, c, vol))
    if not rows:
        return {}

    out: dict[str, Bar] = {}
    for i, (d, o, h, low, c, vol) in enumerate(rows):
        vr = None
        if i >= _VOL_LOOKBACK:
            window = [rows[j][5] for j in range(i - _VOL_LOOKBACK, i)]
            avg = sum(window) / len(window)
            if avg > 0:
                vr = vol / avg
        # prev_close 取**该标的上一根 bar** 的收盘，而不是交易日历上的前一天：
        # 停牌复牌后的前收是停牌前那天的收盘，用日历会取到一个不存在的价。
        prev_close = rows[i - 1][4] if i > 0 else None
        out[d] = Bar(o, h, low, c, prev_close=prev_close, volume_ratio=vr, tradable=True)

    # 顺延：只在"已有有效收盘"之后填，所以上市前的日子不会凭空多出 bar。
    if not days:
        return out
    filtered: dict[str, Bar] = {}
    last_close: float | None = None
    for d in days:
        bar = out.get(d)
        if bar is not None:
            filtered[d] = bar
            last_close = bar.close
        elif last_close is not None:
            filtered[d] = Bar(
                open=last_close, high=last_close, low=last_close, close=last_close,
                prev_close=last_close, volume_ratio=None, tradable=False,
            )
    return filtered


def bars_on(bars_by_code: dict[str, dict[str, Bar]], date: str) -> dict[str, Bar]:
    """把"逐票的日期索引"转成"某一天的逐票 bar"。"""
    return {c: m[date] for c, m in bars_by_code.items() if date in m}
