FROM python:3.12-slim-bookworm

# The release workflow writes the version label; a hardcoded one here would only ever go stale.
LABEL org.opencontainers.image.title="RewardHub"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=9696

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.txt

COPY app.py .
COPY templates ./templates
COPY static ./static

EXPOSE 9696
CMD ["python", "-u", "app.py"]
