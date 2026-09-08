FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Production: gunicorn with a gevent worker (serves the REST API and the
# app/ws/* WebSocket routes). Config in gunicorn.conf.py; binds to $PORT.
CMD ["gunicorn", "--config", "gunicorn.conf.py", "wsgi:app"]
