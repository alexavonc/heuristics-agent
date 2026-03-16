FROM python:3.12

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install-deps chromium
RUN playwright install chromium

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "app.py"]

