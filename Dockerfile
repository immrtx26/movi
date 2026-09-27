FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libzbar0 \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import cv2; print('cv2', cv2.__version__); import os; p=getattr(cv2,'data',None); print('haarcascades', getattr(p,'haarcascades',None) if p else None)"

COPY . .

CMD ["python", "telegram_bot.py"]
