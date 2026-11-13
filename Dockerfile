FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

RUN mkdir -p /data
ENV SLICE_DB_PATH=/data/slices.db

EXPOSE 8000

# uvicorn 单 worker；docker-compose 启动两个独立进程（api-1 / api-2）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
