FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY services/producer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/db ./db
COPY services/producer ./producer

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "producer/camera.py"]
