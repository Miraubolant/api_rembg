from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import sys
import requests
import hashlib
import time
from werkzeug.utils import secure_filename
import uuid
from io import BytesIO
from PIL import Image

app = Flask(__name__)

# Récupérer les variables d'environnement
BRIA_API_TOKEN = os.environ.get('BRIA_API_TOKEN')
API_KEY_SECRET = os.environ.get('API_KEY_SECRET')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')

# Obtenir les domaines autorisés depuis une variable d'environnement
allowed_origins_str = os.environ.get('ALLOWED_ORIGINS', 'https://miremover.fr,http://miremover.fr')
ALLOWED_ORIGINS = [origin.strip() for origin in allowed_origins_str.split(',')]

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
import logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    stream=sys.stdout)
logger = logging.getLogger(__name__)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def verify_api_key(api_key, timestamp, signature):
    """Vérifie si l'API key est valide"""
    # Si l'API_KEY_SECRET n'est pas configuré, désactiver la vérification en développement
    if not API_KEY_SECRET:
        logger.warning("API_KEY_SECRET non configuré, vérification d'API désactivée")
        return True
        
    # Vérifier si le timestamp n'est pas trop ancien (15 minutes max)
    try:
        current_time = int(time.time())
        if current_time - int(timestamp) > 900:  # 15 minutes
            logger.warning(f"Timestamp trop ancien: {timestamp} vs {current_time}")
            return False
    except ValueError:
        logger.warning(f"Timestamp invalide: {timestamp}")
        return False
    
    # Recréer la signature pour vérification
    message = f"{api_key}:{timestamp}"
    expected_signature = hashlib.sha256(f"{message}:{API_KEY_SECRET}".encode()).hexdigest()
    
    # Comparer les signatures
    is_valid = signature == expected_signature
    if not is_valid:
        logger.warning(f"Signature invalide. Attendue: {expected_signature}, Reçue: {signature}")
    
    return is_valid

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
        response = requests.post(url, headers=headers, files=files, data=data)
        
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
        image_response = requests.get(result_url)
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
    
    # Vérifier l'API key si API_KEY_SECRET est configuré
    if API_KEY_SECRET:
        api_key = request.headers.get('X-API-Key')
        timestamp = request.headers.get('X-Timestamp')
        signature = request.headers.get('X-Signature')
        
        if not all([api_key, timestamp, signature]):
            logger.warning("Tentative d'accès sans authentification")
            return jsonify({'error': 'Authentification requise'}), 401
        
        if not verify_api_key(api_key, timestamp, signature):
            logger.warning(f"Authentification échouée pour la clé: {api_key}")
            return jsonify({'error': 'Authentification invalide'}), 403
        
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
        # Générer un nom de fichier unique
        filename = secure_filename(file.filename)
        unique_filename = f"{uuid.uuid4()}_{filename}"
        input_path = os.path.join(UPLOAD_FOLDER, unique_filename)
        
        logger.info(f"Sauvegarde de l'image vers: {input_path}")
        # Sauvegarder l'image
        file.save(input_path)
        
        logger.info(f"Vérification que le fichier existe: {os.path.exists(input_path)}")
        if not os.path.exists(input_path):
            raise Exception(f"Le fichier n'a pas été sauvegardé correctement à {input_path}")
        
        # Ouvrir l'image
        input_image = Image.open(input_path)
        logger.info(f"Image ouverte, taille: {input_image.size}, mode: {input_image.mode}")
        
        # Traiter l'image avec Bria.ai
        logger.info("Début du traitement avec Bria.ai")
        output_image = process_with_bria(input_image, content_moderation)
        
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
        # Nettoyer les fichiers temporaires
        if 'input_path' in locals() and os.path.exists(input_path):
            try:
                os.remove(input_path)
                logger.info(f"Fichier d'entrée supprimé: {input_path}")
            except Exception as e:
                logger.warning(f"Impossible de supprimer le fichier d'entrée: {str(e)}")

@app.route('/generate-api-key', methods=['POST', 'OPTIONS'])
def generate_api_key():
    """Génère une API key pour un client autorisé"""
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
    
    # Logs de débogage pour identifier le problème
    logger.info(f"Requête reçue sur /generate-api-key")
    logger.info(f"Contenu de la requête: {request.json}")
    logger.info(f"ADMIN_PASSWORD configuré: {'Oui' if ADMIN_PASSWORD else 'Non'}")
    logger.info(f"API_KEY_SECRET configuré: {'Oui' if API_KEY_SECRET else 'Non'}")
    
    # Cette route devrait être sécurisée par le mot de passe admin
    admin_password = request.json.get('admin_password')
    logger.info(f"Mot de passe reçu: {admin_password}")
    
    if not ADMIN_PASSWORD or admin_password != ADMIN_PASSWORD:
        logger.warning(f"Authentification échouée: mot de passe incorrect ou non configuré")
        return jsonify({'error': 'Non autorisé'}), 403
    
    # Générer une nouvelle clé API
    client_id = request.json.get('client_id', 'default')
    new_api_key = hashlib.sha256(f"{client_id}:{time.time()}:{os.urandom(16).hex()}".encode()).hexdigest()
    
    # Générer un exemple de comment utiliser cette clé
    timestamp = str(int(time.time()))
    message = f"{new_api_key}:{timestamp}"
    signature = hashlib.sha256(f"{message}:{API_KEY_SECRET}".encode()).hexdigest()
    
    return jsonify({
        'api_key': new_api_key,
        'client_id': client_id,
        'created': int(timestamp),
        'usage_example': {
            'headers': {
                'X-API-Key': new_api_key,
                'X-Timestamp': timestamp,
                'X-Signature': signature
            }
        }
    })

@app.route('/health', methods=['GET', 'OPTIONS'])
def health_check():
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /health")
    return jsonify({
        'status': 'ok',
        'security': {
            'api_key_required': bool(API_KEY_SECRET),
            'allowed_origins': ALLOWED_ORIGINS
        }
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)