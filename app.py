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
allowed_origins_str = os.environ.get('ALLOWED_ORIGINS', 'https://miremover.fr,http://miremover.fr')
ALLOWED_ORIGINS = [origin.strip() for origin in allowed_origins_str.split(',')]

# Configuration des IPs autorisées
AUTHORIZED_IPS = os.environ.get('AUTHORIZED_IPS', '127.0.0.1').split(',')

# Configuration CORS avec les domaines autorisés
CORS(app, resources={r"/*": {"origins": ALLOWED_ORIGINS}})

# Configuration
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'results'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}

# Valeurs par défaut pour le redimensionnement
DEFAULT_MAX_SIZE = 5000
MIN_SIZE = 100
MAX_SIZE = 10000

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

def optimize_image_for_processing(image, max_width=None, max_height=None, max_size=DEFAULT_MAX_SIZE):
    """
    Optimise l'image avant traitement avec options de redimensionnement personnalisables.
    
    Args:
        image (PIL.Image): L'image à optimiser
        max_width (int, optional): Largeur maximale spécifique
        max_height (int, optional): Hauteur maximale spécifique
        max_size (int, optional): Taille maximale (largeur ou hauteur) si max_width et max_height ne sont pas spécifiés
    
    Returns:
        PIL.Image: L'image optimisée
    """
    width, height = image.size
    
    # Vérifier si des dimensions spécifiques ont été demandées
    if max_width is not None or max_height is not None:
        # Si une seule dimension est spécifiée, calculer l'autre en gardant le ratio
        if max_width is not None and max_height is None:
            # Limiter max_width à une valeur raisonnable
            max_width = min(max(int(max_width), MIN_SIZE), MAX_SIZE)
            ratio = max_width / width
            new_width = max_width
            new_height = int(height * ratio)
            logger.info(f"Redimensionnement avec largeur spécifique: {new_width}x{new_height}")
        
        elif max_height is not None and max_width is None:
            # Limiter max_height à une valeur raisonnable
            max_height = min(max(int(max_height), MIN_SIZE), MAX_SIZE)
            ratio = max_height / height
            new_height = max_height
            new_width = int(width * ratio)
            logger.info(f"Redimensionnement avec hauteur spécifique: {new_width}x{new_height}")
        
        else:
            # Les deux dimensions sont spécifiées
            max_width = min(max(int(max_width), MIN_SIZE), MAX_SIZE)
            max_height = min(max(int(max_height), MIN_SIZE), MAX_SIZE)
            new_width = max_width
            new_height = max_height
            logger.info(f"Redimensionnement avec dimensions spécifiques: {new_width}x{new_height}")
        
        image = image.resize((new_width, new_height), Image.LANCZOS)
    
    # Sinon, utiliser la logique de redimensionnement par défaut basée sur max_size
    elif width > max_size or height > max_size:
        # Limiter max_size à une valeur raisonnable
        max_size = min(max(int(max_size), MIN_SIZE), MAX_SIZE)
        
        # Garder le ratio
        if width > height:
            new_width = max_size
            new_height = int(height * (max_size / width))
        else:
            new_height = max_size
            new_width = int(width * (max_size / height))
        
        image = image.resize((new_width, new_height), Image.LANCZOS)
        logger.info(f"Redimensionnement automatique à {new_width}x{new_height}")
    
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
    
    # Récupérer les paramètres de redimensionnement et de modération
    max_width = request.args.get('max_width')
    max_height = request.args.get('max_height')
    max_size = request.args.get('max_size', DEFAULT_MAX_SIZE)
    content_moderation = request.args.get('content_moderation', 'false').lower() in ('true', '1', 't', 'y', 'yes')
    
    # Convertir les paramètres en entiers si présents
    if max_width:
        try:
            max_width = int(max_width)
            logger.info(f"Largeur maximale demandée: {max_width}")
        except ValueError:
            logger.warning(f"Valeur invalide pour max_width: {max_width}")
            return jsonify({'error': 'La valeur de max_width doit être un nombre entier'}), 400
    
    if max_height:
        try:
            max_height = int(max_height)
            logger.info(f"Hauteur maximale demandée: {max_height}")
        except ValueError:
            logger.warning(f"Valeur invalide pour max_height: {max_height}")
            return jsonify({'error': 'La valeur de max_height doit être un nombre entier'}), 400
    
    if max_size:
        try:
            max_size = int(max_size)
            logger.info(f"Taille maximale demandée: {max_size}")
        except ValueError:
            logger.warning(f"Valeur invalide pour max_size: {max_size}")
            return jsonify({'error': 'La valeur de max_size doit être un nombre entier'}), 400
    
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
        
        # Optimiser l'image avant envoi avec les paramètres spécifiés
        input_image = optimize_image_for_processing(
            input_image, 
            max_width=max_width, 
            max_height=max_height, 
            max_size=max_size
        )
        
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
        },
        'image_processing': {
            'default_max_size': DEFAULT_MAX_SIZE,
            'min_size_allowed': MIN_SIZE,
            'max_size_allowed': MAX_SIZE
        }
    })

if __name__ == '__main__':
    # Pour la production, utilisez Gunicorn
    # gunicorn -w 4 -b 0.0.0.0:5000 --timeout 300 app:app
    app.run(host='0.0.0.0', port=5000, threaded=True)