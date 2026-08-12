# Playwright'ın Chromium'unu ve tüm sistem bağımlılıklarını hazır getiren
# resmi imaj — Render gibi bir bulut ortamında headless tarayıcıyı sıfırdan
# kurmakla uğraşmamak için.
FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8765

CMD ["python3", "server.py"]
