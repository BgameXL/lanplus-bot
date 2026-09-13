FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN sed -i 's/\r$//' requirements.txt \
    && pip install --no-cache-dir -r requirements.txt
COPY *.py ./
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown -R app:app /data
USER app
CMD ["python", "bot.py"]
