FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc build-essential && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

COPY . .

# Create default user.cfg from example so env var fallbacks work
COPY .user.cfg.example user.cfg

CMD ["python", "-m", "binance_trade_bot"]
