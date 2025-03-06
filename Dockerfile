FROM python:3.9-slim

WORKDIR /app

# Installer les dépendances système nécessaires pour rembg
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Installer les dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copier le code de l'application
COPY app.py .

# Créer les dossiers pour les uploads et les résultats
RUN mkdir -p uploads results

# Exposer le port
EXPOSE 5000

# Lancer l'application avec Gunicorn pour une meilleure performance
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]