# Trade Master · 模拟交易 + AI 托管
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 先装应用依赖（利用 Docker 层缓存，代码改动不重装）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 引擎是独立包（engine/），pip install -e 装其全部依赖（langchain/langgraph/yfinance 等）
COPY engine/ engine/
RUN pip install --no-cache-dir -e engine/

# 应用代码 + 前端
COPY app/ app/
COPY app_frontend/ app_frontend/

RUN mkdir -p /app/data

EXPOSE 8010

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
