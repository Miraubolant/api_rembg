FROM python:3.9-slim

# Installer les dépendances système nécessaires
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6 \
    imagemagick \
    graphicsmagick \
    && rm -rf /var/lib/apt/lists/*

# Créer un répertoire pour l'application
WORKDIR /app

# Copier le fichier requirements.txt
COPY requirements.txt .

# Installer les dépendances Python
RUN pip install --no-cache-dir -r requirements.txt

# Copier le reste du code de l'application
COPY . .

# Créer les dossiers nécessaires
RUN mkdir -p uploads results

# Variable d'environnement pour le port (peut être surchargée lors de l'exécution)
ENV PORT=5000

# Exposer le port
EXPOSE 5000

# Démarrer l'application
CMD ["python", "app.py"]