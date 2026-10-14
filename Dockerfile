FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 99:100 is nobody:users on Unraid, which normally owns appdata shares.
# Override with `user:` in compose if your /config is owned by someone else.
RUN mkdir /config && chown 99:100 /config
USER 99:100

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
