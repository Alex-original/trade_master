"""持仓解析：文本(DeepSeek) + 截图(阿里云 OCR + DeepSeek)。

截图流程：图片 → 阿里云通用文字识别 → 纯文本 → DeepSeek 解析成结构化持仓。
文本流程：直接 DeepSeek 解析。
原始图片不落库，只返回解析结果供前端预览确认。
"""
from __future__ import annotations

import base64
import json
import os

from app.errors import ServiceError
from app.intent import _llm_json

# 阿里云 OCR 配置（与短信共用同一套 AccessKey）
OCR_ACCESS_KEY_ID = os.getenv("ALIYUN_ACCESS_KEY_ID", "")
OCR_ACCESS_KEY_SECRET = os.getenv("ALIYUN_ACCESS_KEY_SECRET", "")
OCR_CONFIGURED = bool(OCR_ACCESS_KEY_ID and OCR_ACCESS_KEY_SECRET)

_POSITION_SYSTEM = """你是持仓解析器。用户粘贴了券商持仓文本（可能是截图 OCR 结果，格式凌乱）。
提取其中的股票/基金/ETF 持仓，只输出一个 JSON 对象：
{"positions": [{"code":"6位数字或带后缀代码","name":"名称","qty":整数数量,"cost_price":数字成本价}], "confidence":"high|medium|low"}
规则：
1. code 优先取 6 位数字（A股）或带后缀代码（如 06862.HK）；不要臆造，识别不出留空。
2. qty 为股数/份额整数；cost_price 为数字，缺失填 0。
3. 忽略"总资产/市值/盈亏/现金/可用"等非持仓行。
4. 表格凌乱、多数识别不清时 confidence 填 low，positions 可为空。
"""


def parse_text(text: str) -> dict:
    """文本 → 结构化持仓。返回 {positions:[{code,name,qty,cost_price}], confidence}。"""
    if not text or not text.strip():
        raise ServiceError("请粘贴持仓文本")
    result = _llm_json(_POSITION_SYSTEM, [{"role": "user", "content": text.strip()}])
    positions = result.get("positions") or []
    out = []
    for p in positions:
        code = (p.get("code") or "").strip()
        name = (p.get("name") or "").strip()
        try:
            qty = int(p.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        try:
            cost = float(p.get("cost_price") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        if not code:
            continue
        out.append({"code": code, "name": name, "qty": qty, "cost_price": cost})
    return {"positions": out, "confidence": result.get("confidence", "medium")}


def ocr_image(image_bytes: bytes) -> str:
    """阿里云通用文字识别（RecognizeGeneral）→ 纯文本。

    注：SDK 字段名（body/url、返回 data 结构）以实际版本为准，部署验证时校正。
    """
    if not OCR_CONFIGURED:
        raise ServiceError("OCR 未配置（缺 ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET）")
    try:
        from alibabacloud_ocr_api20210707.client import Client
        from alibabacloud_ocr_api20210707 import models as ocr_models
        from alibabacloud_tea_openapi import models as open_api_models
    except ImportError:
        raise ServiceError("未安装阿里云 OCR SDK（alibabacloud_ocr_api20210707）")

    try:
        config = open_api_models.Config(
            access_key_id=OCR_ACCESS_KEY_ID,
            access_key_secret=OCR_ACCESS_KEY_SECRET,
            endpoint="ocr-api.cn-hangzhou.aliyuncs.com",
        )
        client = Client(config)
        req = ocr_models.RecognizeGeneralRequest(body=image_bytes)
        resp = client.recognize_general(req)
        obj = json.loads(resp.body.data or "{}")
        words = []
        for block in (obj.get("prism_wordsInfo") or obj.get("data") or []):
            if isinstance(block, dict) and block.get("word"):
                words.append(block["word"])
        return "\n".join(words)
    except Exception as e:  # noqa: BLE001
        raise ServiceError(f"OCR 识别失败：{e}")


def parse_holdings(input_type: str, content: str) -> dict:
    """主入口。input_type: 'text'（纯文本） | 'image'（base64）。"""
    if input_type == "image":
        try:
            raw = base64.b64decode(content)
        except Exception as e:  # noqa: BLE001
            raise ServiceError(f"图片数据不合法：{e}")
        return parse_text(ocr_image(raw))
    return parse_text(content)
