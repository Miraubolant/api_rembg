FROM python:3.9-slim

WORKDIR /app

# Installer les dépendances système nécessaires pour Pillow, XnConvert et autres
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    wget \
    gnupg \
    libxcb1 \
    libx11-6 \
    libxext6 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Télécharger et installer XnConvert
RUN wget https://download.xnview.com/XnConvert-linux-x64.deb \
    && dpkg -i XnConvert-linux-x64.deb || true \
    && apt-get update && apt-get -f install -y \
    && rm XnConvert-linux-x64.deb \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Définir le chemin vers XnConvert
ENV XNCONVERT_PATH=/usr/bin/xnconvert

# Copier les fichiers de dépendances
COPY requirements.txt .

# Installer les dépendances Python
RUN pip install --no-cache-dir -r requirements.txt

# Copier le code de l'application
COPY app.py .

# Créer les dossiers pour les uploads et les résultats
RUN mkdir -p uploads results logs

# Exposer le port
EXPOSE 5000

# Variable d'environnement pour désactiver le buffer pour les logs
ENV PYTHONUNBUFFERED=1

# Lancer l'application avec Gunicorn en mode optimisé
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--threads", "4", "--timeout", "300", "app:app"]