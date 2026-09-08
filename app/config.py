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
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
# 推理模型需要给足 token 预算：正文 + 推理 token 共享 max_tokens
DEEPSEEK_MAX_TOKENS = int(os.getenv("DEEPSEEK_MAX_TOKENS", "8000"))

# ---- 行情缓存 ----
MARKET_CACHE_PATH = Path(
    os.getenv("MARKET_CACHE_PATH", str(DATA_DIR / "market_cache.sqlite"))
)


def ensure_dirs() -> None:
    """确保数据目录存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MARKET_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
