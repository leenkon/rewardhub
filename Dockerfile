FROM python:3.12-slim-bookworm

# 镜像版本 label 由发布工作流写入，这里写死一个只会过期。
LABEL org.opencontainers.image.title="RewardHub"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=9696

WORKDIR /app

# slim 基础镜像不带 zone 数据库，缺了它 TZ 会静默退回 UTC，每一天都按早 8 小时翻页。
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.txt

COPY app.py .
COPY templates ./templates
COPY static ./static

EXPOSE 9696
CMD ["python", "-u", "app.py"]
