FROM python:3.11-slim

WORKDIR /app

COPY shared/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/db ./db
COPY shared/migrations ./migrations
COPY shared/alembic.ini .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["alembic", "upgrade", "head"]
