FROM python:3.9-slim

WORKDIR /app

# Installer les dépendances système nécessaires pour Pillow, XnConvert et autres
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    wget \
    ca-certificates \
    gnupg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Installer XnConvert
RUN mkdir -p /etc/apt/keyrings \
    && wget -O- https://dl.xnview.com/keys/xnview-key.asc | gpg --dearmor -o /etc/apt/keyrings/xnview.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/xnview.gpg] https://dl.xnview.com/apt all main" > /etc/apt/sources.list.d/xnview.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends xnconvert \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Configurer le chemin vers XnConvert
ENV XNCONVERT_PATH=/usr/bin/xnconvert

# Copier les fichiers de dépendances
COPY requirements.txt .

# Installer les dépendances Python
RUN pip install --no-cache-dir -r requirements.txt

# Copier le code de l'application
COPY app.py .

# Créer les dossiers pour les uploads, les résultats et les fichiers temporaires
RUN mkdir -p uploads results logs xnconvert_temp

# Exposer le port
EXPOSE 5000

# Variable d'environnement pour désactiver le buffer pour les logs
ENV PYTHONUNBUFFERED=1

# Lancer l'application avec Gunicorn en mode optimisé
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--threads", "4", "--timeout", "300", "app:app"]