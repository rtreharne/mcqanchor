FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN apt-get update && \
    apt-get install -y --no-install-recommends poppler-utils tesseract-ocr && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --upgrade pip && \
    pip install -r /app/requirements.txt

COPY . /app

RUN chmod +x /app/bin/render-start.sh

EXPOSE 10000

CMD ["/app/bin/render-start.sh"]
