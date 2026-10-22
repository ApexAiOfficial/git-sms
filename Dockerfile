FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIT_SMS_HOST=0.0.0.0 \
    PORT=8000 \
    GIT_SMS_DB_PATH=/data/git-sms.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY git_sms ./git_sms

RUN useradd --system --uid 10001 --home-dir /app git-sms && mkdir /data && chown git-sms /data
USER git-sms
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"

CMD ["python", "-m", "git_sms"]
