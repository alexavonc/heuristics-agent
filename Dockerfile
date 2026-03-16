cd /Users/alexavoncharles/Documents/GitHub/heuristics-agent
cat > Dockerfile << 'EOF'
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    wget \
    curl \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright

RUN playwright install chromium --with-deps

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "app.py"]
EOF
