from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import sys
import requests
import time
from werkzeug.utils import secure_filename
import uuid
from io import BytesIO
from PIL import Image
import concurrent.futures
import threading
import logging
import logging.handlers

app = Flask(__name__)

# Récupérer les variables d'environnement
BRIA_API_TOKEN = os.environ.get('BRIA_API_TOKEN')

# Obtenir les domaines autorisés depuis une variable d'environnement
allowed_origins_str = os.environ.get('ALLOWED_ORIGINS')
ALLOWED_ORIGINS = [origin.strip() for origin in allowed_origins_str.split(',')]

# Configuration des IPs autorisées
AUTHORIZED_IPS = os.environ.get('AUTHORIZED_IPS', '127.0.0.1').split(',')

# Configuration CORS avec les domaines autorisés
CORS(app, resources={r"/*": {"origins": ALLOWED_ORIGINS}})

# Configuration
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'results'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}

# Créer les dossiers s'ils n'existent pas
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Configuration des logs
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    stream=sys.stdout)
logger = logging.getLogger(__name__)

# Ajouter une rotation des logs
log_handler = logging.handlers.RotatingFileHandler(
    'app.log', maxBytes=10*1024*1024, backupCount=5
)
log_handler.setLevel(logging.INFO)
logger.addHandler(log_handler)

# Pool de threads pour les opérations intensives
thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Middleware pour vérifier l'IP source
@app.before_request
def restrict_access_by_ip():
    # Autoriser toujours les requêtes OPTIONS pour CORS
    if request.method == 'OPTIONS':
        return None
        
    client_ip = request.remote_addr
    
    # Vérifier si l'IP est autorisée
    if client_ip not in AUTHORIZED_IPS:
        logger.warning(f"Tentative d'accès non autorisée depuis l'IP: {client_ip}")
        return jsonify({'error': 'Accès non autorisé'}), 403

def optimize_image_for_processing(image, max_size=1500):
    """
    Optimise l'image avant traitement :
    1. Redimensionne si trop grande
    2. Compresse
    """
    # Redimensionner si nécessaire
    width, height = image.size
    if width > max_size or height > max_size:
        # Garder le ratio
        if width > height:
            new_width = max_size
            new_height = int(height * (max_size / width))
        else:
            new_height = max_size
            new_width = int(width * (max_size / height))
        
        image = image.resize((new_width, new_height), Image.LANCZOS)
        logger.info(f"Image redimensionnée à {new_width}x{new_height}")
    
    return image

def process_with_bria(input_image, content_moderation=False):
    """Traitement avec l'API Bria.ai RMBG 2.0"""
    try:
        # Vérifier si la clé API est disponible
        if not BRIA_API_TOKEN:
            raise Exception("Clé API Bria.ai non configurée. Veuillez définir la variable d'environnement BRIA_API_TOKEN.")
            
        # Sauvegarder l'image temporairement pour l'envoyer via API Bria
        temp_file = BytesIO()
        input_image.save(temp_file, format='PNG')
        temp_file.seek(0)
        
        # Préparer la requête à l'API Bria
        url = "https://engine.prod.bria-api.com/v1/background/remove"
        headers = {
            "api_token": BRIA_API_TOKEN
        }
        
        files = {
            'file': ('image.png', temp_file, 'image/png')
        }
        
        data = {}
        if content_moderation:
            data['content_moderation'] = 'true'
        
        logger.info("Envoi de l'image à Bria.ai API")
        timeout = (5, 30)  # (connect timeout, read timeout)
        response = requests.post(url, headers=headers, files=files, data=data, timeout=timeout)
        
        if response.status_code != 200:
            logger.error(f"Erreur API Bria: {response.status_code} - {response.text}")
            raise Exception(f"Erreur API Bria: {response.status_code} - {response.text}")
        
        # Récupérer l'URL de l'image résultante
        result_data = response.json()
        result_url = result_data.get('result_url')
        
        if not result_url:
            raise Exception("Aucune URL de résultat retournée par Bria API")
        
        logger.info(f"Image traitée avec succès par Bria.ai, URL résultante: {result_url}")
        
        # Télécharger l'image résultante
        image_response = requests.get(result_url, timeout=timeout)
        if image_response.status_code != 200:
            raise Exception(f"Erreur lors du téléchargement de l'image résultante: {image_response.status_code}")
        
        # Ouvrir l'image téléchargée avec PIL
        result_image = Image.open(BytesIO(image_response.content))
        
        return result_image
    except Exception as e:
        logger.error(f"Erreur lors du traitement avec Bria.ai: {str(e)}")
        raise

@app.route('/remove-background', methods=['POST', 'OPTIONS'])
def remove_background_api():
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /remove-background")
    
    # Récupérer le paramètre de modération de contenu
    content_moderation = request.args.get('content_moderation', 'false').lower() in ('true', '1', 't', 'y', 'yes')
    
    # Vérifier si une image a été envoyée
    if 'image' not in request.files:
        logger.error("Aucune image n'a été envoyée")
        return jsonify({'error': 'Aucune image n\'a été envoyée'}), 400
    
    file = request.files['image']
    logger.info(f"Fichier reçu: {file.filename}")
    
    # Vérifier si le fichier est valide
    if file.filename == '':
        logger.error("Nom de fichier vide")
        return jsonify({'error': 'Nom de fichier vide'}), 400
    
    if not allowed_file(file.filename):
        logger.error(f"Format de fichier non supporté: {file.filename}")
        return jsonify({'error': f'Format de fichier non supporté. Formats acceptés: {", ".join(ALLOWED_EXTENSIONS)}'}), 400
    
    try:
        # Lire le fichier en mémoire
        file_data = file.read()
        input_image = Image.open(BytesIO(file_data))
        logger.info(f"Image ouverte, taille: {input_image.size}, mode: {input_image.mode}")
        
        # Optimiser l'image avant envoi
        input_image = optimize_image_for_processing(input_image)
        
        # Traiter l'image avec Bria.ai
        logger.info("Début du traitement avec Bria.ai")
        
        # Utiliser le pool de threads pour le traitement
        future = thread_pool.submit(process_with_bria, input_image, content_moderation)
        output_image = future.result()
        
        logger.info(f"Traitement terminé avec succès, mode de l'image résultante: {output_image.mode}")
        
        # Envoyer directement l'image en PNG avec transparence via BytesIO
        logger.info("Préparation de l'image PNG avec transparence pour l'envoi")
        img_io = BytesIO()
        
        # Assurez-vous que l'image est en mode RGBA pour la transparence
        if output_image.mode != 'RGBA':
            logger.info(f"Conversion de l'image du mode {output_image.mode} vers RGBA")
            output_image = output_image.convert('RGBA')
            
        output_image.save(img_io, format='PNG')
        img_io.seek(0)
        
        # Afficher les informations sur la taille de l'image
        img_size = img_io.getbuffer().nbytes
        logger.info(f"Taille de l'image à envoyer: {img_size} octets")
        
        # Envoyer l'image avec le bon type MIME
        logger.info("Envoi du fichier PNG au client")
        response = send_file(
            img_io, 
            mimetype='image/png',
            download_name='image_sans_fond.png',
            as_attachment=True  # Force le téléchargement plutôt que l'affichage
        )
        
        # Ajouter des en-têtes pour éviter la mise en cache
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["Content-Length"] = str(img_size)
        
        return response
    
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        logger.error(f"ERREUR: {str(e)}")
        logger.error(f"DÉTAILS: {error_details}")
        return jsonify({'error': f'Erreur pendant le traitement: {str(e)}', 'details': error_details}), 500
    
    finally:
        # Nettoyer les ressources
        if 'input_image' in locals():
            input_image.close()
        if 'output_image' in locals():
            output_image.close()

@app.route('/health', methods=['GET', 'OPTIONS'])
def health_check():
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /health")
    return jsonify({
        'status': 'ok',
        'security': {
            'ip_restriction': True,
            'allowed_origins': ALLOWED_ORIGINS,
            'authorized_ips': AUTHORIZED_IPS
        }
    })

if __name__ == '__main__':
    # Pour la production, utilisez Gunicorn
    # gunicorn -w 4 -b 0.0.0.0:5000 --timeout 300 app:app
    app.run(host='0.0.0.0', port=5000, threaded=True)
