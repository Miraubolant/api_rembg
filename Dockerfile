FROM python:3.9-slim

WORKDIR /app

# Installer les dépendances système nécessaires pour Pillow et autres
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

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