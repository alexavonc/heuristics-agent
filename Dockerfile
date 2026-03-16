FROM python:3.12

WORKDIR /app

COPY requirements.txt .

# Single RUN step: any requirements.txt change forces full rebuild including browser download
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install-deps chromium \
    && playwright install chromium

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "app.py"]
