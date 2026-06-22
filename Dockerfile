FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc build-essential && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

COPY . .

# Create data directory for SQLite database
RUN mkdir -p /app/data
VOLUME /app/data

CMD ["python", "-m", "binance_trade_bot"]
