FROM python:3.10-slim

# Instalacja zaktualizowanych pakietów systemowych dla OpenCV
RUN apt-get update && apt-get install -y \
    libglib2.0-0 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Kopiowanie wymagań i instalacja
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopiowanie reszty plików bota
COPY . .

# Komenda startowa bota
CMD ["python", "main.py"]
