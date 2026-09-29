FROM python:3.11-slim

WORKDIR /app

# Dependencias del sistema para pyzbar y OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
    libzbar0 \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copiar e instalar dependencias de Python (incluye job-queue)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import cv2; print('cv2', cv2.__version__); import os; p=getattr(cv2,'data',None); print('haarcascades', getattr(p,'haarcascades',None) if p else None)" \
    && python -c "from telegram.ext import JobQueue; print('JobQueue OK')"

# Pre-descarga del Haar cascade como fallback (si cv2.data no lo trae)
RUN python - <<'PY'
import os, urllib.request, pathlib
dest = pathlib.Path("models"); dest.mkdir(exist_ok=True)
target = dest / "haarcascade_frontalface_default.xml"
if not target.is_file():
    try:
        url = "https://raw.githubusercontent.com/opencv/opencv/4.x/data/haarcascades/haarcascade_frontalface_default.xml"
        urllib.request.urlretrieve(url, target)
        print("Haar cascade descargado:", target)
    except Exception as e:
        print("No se pudo descargar Haar cascade:", e)
PY

COPY . .

CMD ["python", "telegram_bot.py"]