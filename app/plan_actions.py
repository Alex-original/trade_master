"""计划动作归一与分档：把组合经理的动作列表收敛成可机械执行的动作/梯子。

纯函数、无依赖，放在独立模块是为了让**写时**（落库前）、**读时**（监控卡片）、
**执行层**（机械再平衡）与**报告渲染**四处共用同一套口径——只要有一处用了另一套
折叠规则，用户就会看到「同一只票两行、两次结论还不一样」。

背景：组合经理经常对同一只票**同时**给出两条——基准动作（``hold``，目标占比=当前
占比、无触发条件）和条件动作（如「跌破 1.354 → 减到 5.45%」）。前者等于「默认不动」，
信息量为零，却会让下游表格重复、目标占比互相矛盾。这里丢掉被条件动作覆盖的基准行；
若某只票有多条**不同条件**的动作则全部保留（那是真的两套触发条件）。

## 分档（梯子）

同一只票的多条**同向**动作构成一条「梯子」，由浅到深排列：

    跌破 8.90 → 减到 13.8%    ← 第 1 档（最浅）
    跌破 8.76 → 减到 9%       ← 第 2 档
    跌破 8.75 → 清仓           ← 第 3 档（最深）

这是「放量跌破8.90先减1/4；有效跌破8.76–8.82再减到半仓以下；硬止损8.75」这类计划
真正可执行的形式——此前三个提示词都只让模型给一条，多档只写在 ``reason`` 的散文里，
执行层根本看不到，第 2 档之后永远不生效。

梯子按**动作方向**分两条：``exit``（sell/reduce）与 ``entry``（buy/add）。方向由
``action`` 决定而非 ``trigger_type``——「涨到 10.5 止盈减半」是减仓梯子里的一个
``price_above`` 档，不是加仓。

**校验取「截断到最长合法前缀」而非整只丢弃**：第 3 档格式错时，第 1 档仍然可执行；
因为一档有问题就把整只票的动作全丢掉，等于关掉唯一能动的档，更糟。被丢弃的原因
写进告警，报告里显式列出（见 ``app/plan_report.py`` 的口径段）。
"""
from __future__ import annotations

EXIT_ACTIONS = ("sell", "reduce")
ENTRY_ACTIONS = ("buy", "add")

# 无条件档（不依赖价格）：none=计划生成即生效；open=开盘；intraday=盘中
_TIME_TRIGGERS = ("none", "intraday", "open")
_PRICE_TRIGGERS = ("price_below", "price_above")

_KIND_LABEL = {"exit": "减仓", "entry": "加仓", "hold": "持有"}
_EPS = 1e-9


def normalize_plan_actions(actions: list[dict] | None) -> list[dict]:
    """动作列表 → 每只票一条（或每条不同的触发条件各一条），保持首次出现的顺序。"""
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    for a in actions or []:
        code = a.get("code")
        if not code:
            continue
        if code not in grouped:
            grouped[code] = []
            order.append(code)
        grouped[code].append(a)

    out: list[dict] = []
    for code in order:
        group = grouped[code]
        non_hold = [a for a in group if (a.get("action") or "").lower() != "hold"]
        keep = non_hold or group[:1]
        seen = set()
        for a in keep:
            key = (
                a.get("action"),
                a.get("trigger_type") or "none",
                a.get("trigger_price"),
                a.get("target_weight"),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
    return out


def _action_family(action: str | None) -> str:
    """动作 → 所属梯子。``hold`` 与未知动作归入 hold：只展示、不执行。"""
    a = (action or "").lower()
    if a in EXIT_ACTIONS:
        return "exit"
    if a in ENTRY_ACTIONS:
        return "entry"
    return "hold"


def _is_time_tier(a: dict) -> bool:
    """无条件档（不依赖价格）。注意 ``price_below`` 但缺 trigger_price 的**不算**——
    那是永远不触发的死档，由 ``_sanitize_ladder`` 单独剔除。"""
    return (a.get("trigger_type") or "none") in _TIME_TRIGGERS


def fill_price(action: dict | None, bar) -> float:
    """一档动作在给定 bar 上的**成交价**。纯函数，无 I/O。

    口径（回测与实时共用，靠 ``Bar`` 的退化性统一）：

    - ``price_below``（跌破档）：``min(bar.open, trigger_price)``
    - ``price_above``（涨过档）：``max(bar.open, trigger_price)``
    - 无条件档（``none`` / ``open`` / ``intraday``）：``bar.open``

    前两条就是「日内触发 + 跳空按开盘」：没跳空时 ``open`` 未穿越触发价，取到触发价本身；
    跳空时 ``open`` 已越过触发价，取 ``open``——**跳空按开盘成交**。

    实时路径之所以天然正确：实时 bar 是退化 bar（``open == high == low == close == 现价``），
    而 ``price_below`` 档成立的前提正是 ``现价 <= trigger_price``，此时
    ``min(现价, trigger_price) == 现价``。所以实时下本函数恒等于「按现价成交」，
    与改造前的标量路径**逐位相同**。

    ⚠️ **前置条件**：调用方必须已确认 ``bar.tradable``。停牌 bar 的价位无意义，
    本函数不做兜底——静默返回一个假价格比报错危险得多。
    """
    t = (action or {}).get("trigger_type") or "none"
    tp = (action or {}).get("trigger_price")
    if t == "price_below" and tp is not None:
        return min(float(bar.open), float(tp))
    if t == "price_above" and tp is not None:
        return max(float(bar.open), float(tp))
    return float(bar.open)


def _tier_depth_key(a: dict) -> tuple:
    """梯子内由浅到深的排序键（越浅=离现价越近、越该先动）。

    无条件档恒为最浅（无条件即「立刻」）；跌破档阈值越高越浅；涨过档阈值越低越浅。
    """
    t = a.get("trigger_type") or "none"
    if t == "price_below":
        p = a.get("trigger_price")
        if p is not None:
            return (1, -float(p))
    elif t == "price_above":
        p = a.get("trigger_price")
        if p is not None:
            return (2, float(p))
    return (0, 0.0)


def _target_monotone(kind: str, prev: dict, cur: dict) -> bool:
    """越深的档不得更激进：减仓梯子目标占比逐档**不增**，加仓梯子**不减**。

    目标占比任一为空则跳过（无从比较，交给执行层按缺失处理）。
    """
    a, b = prev.get("target_weight"), cur.get("target_weight")
    if a is None or b is None:
        return True
    return b <= a + _EPS if kind == "exit" else b >= a - _EPS


def _sanitize_ladder(code: str, kind: str, tiers: list[dict], warnings: list[str]) -> list[dict]:
    """一条梯子的校验与截断，返回保留的档（浅→深）。"""
    if not tiers:
        return []
    label = _KIND_LABEL[kind]
    ordered = sorted(tiers, key=_tier_depth_key)

    # 死档：price_below/price_above 却没有触发价，_trigger_satisfied 恒为 False。
    # 留着只会让用户以为「挂了个条件」，不如剔掉并说清楚。
    alive = [a for a in ordered if not (a.get("trigger_type") in _PRICE_TRIGGERS and a.get("trigger_price") is None)]
    if len(alive) != len(ordered):
        warnings.append(f"{code} {label}梯子有 {len(ordered) - len(alive)} 档缺少触发价（永远不会触发），已剔除")
    ordered = alive

    # sell 的语义就是清仓（ConditionalAction 字段描述如此）。强制目标 0，否则「卖到 3%」
    # 会留下一笔永远卖不掉的零股。放在单调性检查之前，让清仓档天然成为最深档。
    if kind == "exit":
        ordered = [
            ({**a, "target_weight": 0.0} if (a.get("action") or "").lower() == "sell" else a)
            for a in ordered
        ]

    # 一条梯子里最多一个无条件档：多个等于自相矛盾，只留最后一个。
    time_idx = [i for i, a in enumerate(ordered) if _is_time_tier(a)]
    if len(time_idx) > 1:
        drop = set(time_idx[:-1])
        warnings.append(f"{code} {label}梯子有多档无条件触发，只保留最后一个")
        ordered = [a for i, a in enumerate(ordered) if i not in drop]

    keep: list[dict] = []
    for a in ordered:
        if keep and not _target_monotone(kind, keep[-1], a):
            warnings.append(
                f"{code} {label}梯子第 {len(keep) + 1} 档目标占比更激进"
                f"（{a.get('target_weight')} vs 上一档 {keep[-1].get('target_weight')}），"
                f"该档及其后已丢弃"
            )
            break
        if keep and _tier_depth_key(keep[-1]) == _tier_depth_key(a):
            # 同一触发价出现多档：不是错误——执行层逐档分批正好会按浅→深依次推进。
            # 只提示，不截断，免得把可执行的档关掉。
            warnings.append(f"{code} {label}梯子有重复触发价 {a.get('trigger_price')}，按由浅到深逐档执行")
        keep.append(a)
    return keep


def _check_directions(code: str, ladders: dict[str, list[dict]], warnings: list[str]) -> None:
    """双向梯子冲突检查：同一只票的减仓与加仓不能在同一个价位同时成立。

    两种冲突：
    1. 价格轴重叠——减仓最高触发价 ≥ 加仓最低触发价，两者在同一价位同时满足。若不拦，
       执行层的「最浅未达成」规则会在两个目标之间来回对冲（卖到 13.8% → 买到 15% →
       再卖回 13.8%…）。
    2. 一侧含无条件档——「无条件减到 10%」与「涨过 10.5 加到 15%」在无状态执行模型里
       同样会来回对冲，因为无条件档每次都满足。

    冲突时**保留减仓梯子**（防御优先），丢弃加仓梯子并告警。宁可错过一次加仓，
    也不能让仓位在两个目标之间反复摩擦产生手续费。
    """
    exit_t, entry_t = ladders.get("exit") or [], ladders.get("entry") or []
    if not exit_t or not entry_t:
        return
    reason = ""
    if any(_is_time_tier(a) for a in exit_t) or any(_is_time_tier(a) for a in entry_t):
        reason = "同一只票同时有无条件档和反向梯子"
    else:
        below = [float(a["trigger_price"]) for a in exit_t if a.get("trigger_type") == "price_below"]
        above = [float(a["trigger_price"]) for a in entry_t if a.get("trigger_type") == "price_above"]
        if below and above and max(below) >= min(above):
            reason = f"减仓最高触发价 {max(below)} ≥ 加仓最低触发价 {min(above)}"
    if reason:
        warnings.append(f"{code} 双向梯子冲突（{reason}），已丢弃加仓梯子")
        ladders["entry"] = []


def build_ladders(actions: list[dict] | None) -> tuple[dict[str, dict[str, list[dict]]], list[str]]:
    """归一 → 按 code 分组、按方向拆梯子 → 排序 → 校验截断。

    返回 ``(ladders, warnings)``：``ladders[code]`` = ``{"exit": [...], "entry": [...],
    "hold": [...]}``，每档浅→深；``warnings`` 进 ``plan["process"]["ladder_warnings"]``。

    对已归一的输入是**幂等**的（写时落库、读时再跑一次作为防御）。
    """
    grouped: dict[str, dict[str, list[dict]]] = {}
    for a in normalize_plan_actions(actions):
        code = a.get("code")
        if not code:
            continue
        buckets = grouped.setdefault(code, {"exit": [], "entry": [], "hold": []})
        buckets[_action_family(a.get("action"))].append(a)

    warnings: list[str] = []
    ladders: dict[str, dict[str, list[dict]]] = {}
    for code, buckets in grouped.items():
        out = {kind: _sanitize_ladder(code, kind, buckets[kind], warnings) for kind in ("exit", "entry")}
        out["hold"] = buckets["hold"]
        _check_directions(code, out, warnings)
        ladders[code] = out
    return ladders, warnings


def sanitize_ladder_actions(actions: list[dict] | None) -> tuple[list[dict], list[str]]:
    """校验后的扁平动作列表（供落库与报告）+ 告警。

    顺序：按 code 首次出现的顺序，先 hold 基准行，再减仓梯子（浅→深），最后加仓梯子
    （浅→深）——单档计划的顺序与 `normalize_plan_actions` 一致。
    """
    ladders, warnings = build_ladders(actions)
    flat: list[dict] = []
    for by_kind in ladders.values():
        for kind in ("hold", "exit", "entry"):
            flat.extend(by_kind[kind])
    return flat, warnings
