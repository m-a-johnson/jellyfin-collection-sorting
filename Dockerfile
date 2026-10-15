FROM python:3.13-slim

# Set by the GitHub Actions build (a release tag such as 1.2.0, or main-<short sha>).
ARG APP_VERSION=dev
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 APP_VERSION=${APP_VERSION}

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 99:100 is nobody:users on Unraid, which normally owns appdata shares.
# Override with `user:` in compose if your /config is owned by someone else.
RUN mkdir /config && chown 99:100 /config
USER 99:100

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
