"""Trade Master 配置模块。

读取环境变量（.env），统一 DeepSeek/OpenAI 兼容端点、行情缓存路径等。
用法：from app import config；config.DEEPSEEK_API_KEY
"""
from __future__ import annotations

import os
from pathlib import Path

# 项目根目录（app/ 的上一级）
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

# ---- 大模型（DeepSeek，OpenAI 兼容）----
# 约定与 video-note 一致：用 OPENAI_API_KEY 作为 key 名，base_url 指向 DeepSeek
DEEPSEEK_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or ""
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# 默认值必须与 https://api.deepseek.com/models 实际返回的 id 一致：
# 2026-09-14 12:00 起 deepseek-v4-pro 下线，线上只剩 deepseek-flash。
# 这里留旧 id 会让「.env 少写一行」变成静默调用一个不存在的模型。
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
# 推理模型需要给足 token 预算：正文 + 推理 token 共享 max_tokens
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "8000"))

# ---- 行情缓存 ----
MARKET_CACHE_PATH = Path(
    os.getenv("MARKET_CACHE_PATH", str(DATA_DIR / "market_cache.sqlite"))
)

# ---- 内部运维端点令牌 ----
# ``/api/internal/*`` 用于内测期手动触发执行层、观测调度器。**fail-closed**：
# 未配置时端点恒 403，绝不"没配就放行"——一个默认放行的运维端点等于把下单能力挂在公网上。
# 公网另有 nginx ``location ^~ /api/internal/`` 直接 404（只留回环可达），这里是第二道。
INTERNAL_TOKEN = os.getenv("TRADE_MASTER_INTERNAL_TOKEN", "")

# ---- 管理员白名单（手机号，逗号分隔）----
# 目前只有一个用途：**历史回测只对管理员开放**。与 INTERNAL_TOKEN 同样是 **fail-closed** ——
# 没配就是"谁都不是管理员"，于是回测功能整体关闭（而不是"没配就人人可用"）。
# 代价是"忘了配"会表现成"功能凭空消失"，所以 app.main 启动时会打一行日志说明配了几个。
# 写成手机号而不是 user_id：建号顺序在不同环境里不一样，手机号是唯一稳定的身份。
ADMIN_PHONES = frozenset(
    p.strip() for p in os.getenv("ADMIN_PHONES", "").split(",") if p.strip()
)


def ensure_dirs() -> None:
    """确保数据目录存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MARKET_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
