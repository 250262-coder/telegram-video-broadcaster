FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Long-polling worker: no port is exposed on purpose. State lives in Postgres,
# because App Platform containers have an ephemeral filesystem.
CMD ["python", "bot.py"]
