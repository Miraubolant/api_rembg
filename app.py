from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import sys
import requests
import time
import subprocess
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

# Positions de recadrage valides
VALID_CROP_POSITIONS = ['center', 'top_left', 'top', 'top_right', 'left', 'right', 'bottom_left', 'bottom', 'bottom_right']
DEFAULT_CROP_POSITION = 'center'
DEFAULT_BG_COLOR = (255, 255, 255)  # Blanc par défaut

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

def resize_with_xnconvert(input_path, output_path, width=None, height=None, crop_position='center', bg_color=(255, 255, 255), output_format='jpeg', quality=80):
    """
    Redimensionne et recadre une image avec PIL/Pillow mais simule le comportement de XnConvert.
    """
    try:
        # Ouvrir l'image d'entrée
        img = Image.open(input_path)
        original_width, original_height = img.size
        logger.info(f"Image d'origine: {original_width}x{original_height}, mode: {img.mode}")
        
        # Définir la taille cible
        target_width = width if width is not None else original_width
        target_height = height if height is not None else original_height
        
        # Redimensionner avec conservation du ratio (équivalent à -ratio dans XnConvert)
        if width is not None and height is not None:
            # Calculer le ratio pour conserver les proportions
            width_ratio = target_width / original_width
            height_ratio = target_height / original_height
            ratio = min(width_ratio, height_ratio)
            
            # Calculer les nouvelles dimensions
            resize_width = int(original_width * ratio)
            resize_height = int(original_height * ratio)
            
            # Redimensionner l'image avec resampling de haute qualité (équivalent à -rtype hanning)
            img = img.resize((resize_width, resize_height), Image.LANCZOS)
            logger.info(f"Image redimensionnée à {resize_width}x{resize_height}")
            
            # Créer un nouveau canvas avec la couleur de fond (équivalent à -canvas)
            bg = Image.new('RGBA', (target_width, target_height), bg_color + (255,))
            
            # Calculer la position pour placer l'image sur le canvas
            positions = {
                'center': ((target_width - resize_width) // 2, (target_height - resize_height) // 2),
                'top_left': (0, 0),
                'top': ((target_width - resize_width) // 2, 0),
                'top_right': (target_width - resize_width, 0),
                'left': (0, (target_height - resize_height) // 2),
                'right': (target_width - resize_width, (target_height - resize_height) // 2),
                'bottom_left': (0, target_height - resize_height),
                'bottom': ((target_width - resize_width) // 2, target_height - resize_height),
                'bottom_right': (target_width - resize_width, target_height - resize_height)
            }
            
            position = positions.get(crop_position, positions['center'])
            
            # Placer l'image sur le canvas
            if img.mode == 'RGBA':
                bg.paste(img, position, img)
            else:
                bg.paste(img, position)
            
            img = bg
            logger.info(f"Image placée sur canvas de {target_width}x{target_height} à la position {crop_position}")
        elif width is not None:
            # Redimensionner uniquement par largeur en gardant le ratio
            ratio = target_width / original_width
            new_height = int(original_height * ratio)
            img = img.resize((target_width, new_height), Image.LANCZOS)
            logger.info(f"Image redimensionnée par largeur à {target_width}x{new_height}")
        elif height is not None:
            # Redimensionner uniquement par hauteur en gardant le ratio
            ratio = target_height / original_height
            new_width = int(original_width * ratio)
            img = img.resize((new_width, target_height), Image.LANCZOS)
            logger.info(f"Image redimensionnée par hauteur à {new_width}x{target_height}")
        
        # Enregistrer dans le format demandé
        if output_format.lower() in ('jpg', 'jpeg'):
            # Pour JPEG, convertir en RGB et utiliser le fond blanc pour l'alpha
            if img.mode == 'RGBA':
                white_bg = Image.new('RGB', img.size, (255, 255, 255))
                white_bg.paste(img, (0, 0), img)
                img = white_bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')
                
            img.save(output_path, format='JPEG', quality=quality)
            logger.info(f"Image sauvegardée en JPEG avec qualité {quality}: {output_path}")
        else:
            # Pour PNG, conserver l'alpha
            img.save(output_path, format='PNG')
            logger.info(f"Image sauvegardée en PNG: {output_path}")
        
        # Simuler le message de log de XnConvert pour maintenir la cohérence
        logger.info(f"Redimensionnement simulé de XnConvert réussi: {output_path}")
        return True
    except Exception as e:
        logger.error(f"Erreur lors du redimensionnement avec Pillow/XnConvert: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return False

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
    
    # Récupérer les paramètres de redimensionnement, de modération et de format
    width = request.args.get('width')
    height = request.args.get('height')
    content_moderation = request.args.get('content_moderation', 'false').lower() in ('true', '1', 't', 'y', 'yes')
    output_format = request.args.get('format', 'jpg').lower()  # Format par défaut jpg
    crop_position = request.args.get('crop_position', DEFAULT_CROP_POSITION).lower()
    
    # Vérifier que la position de recadrage est valide
    if crop_position not in VALID_CROP_POSITIONS:
        logger.warning(f"Position de recadrage invalide: {crop_position}, utilisation de {DEFAULT_CROP_POSITION}")
        crop_position = DEFAULT_CROP_POSITION
    
    # Vérifier que le format de sortie est valide
    if output_format not in ['jpg', 'png']:
        logger.warning(f"Format de sortie invalide: {output_format}, utilisation de jpg par défaut")
        output_format = 'jpg'
    
    # Convertir les paramètres de dimension en entiers si présents
    if width:
        try:
            width = int(width)
            logger.info(f"Largeur demandée: {width}")
        except ValueError:
            logger.warning(f"Valeur invalide pour width: {width}")
            return jsonify({'error': 'La valeur de width doit être un nombre entier'}), 400
    
    if height:
        try:
            height = int(height)
            logger.info(f"Hauteur demandée: {height}")
        except ValueError:
            logger.warning(f"Valeur invalide pour height: {height}")
            return jsonify({'error': 'La valeur de height doit être un nombre entier'}), 400
    
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
        # Génération d'un ID unique pour les fichiers temporaires
        unique_id = str(uuid.uuid4())
        temp_input_path = os.path.join(UPLOAD_FOLDER, f"{unique_id}_input.png")
        temp_output_path = os.path.join(OUTPUT_FOLDER, f"{unique_id}_output.{output_format}")
        
        # Lire le fichier en mémoire et sauvegarder l'image d'entrée
        file_data = file.read()
        input_image = Image.open(BytesIO(file_data))
        logger.info(f"Image ouverte, taille: {input_image.size}, mode: {input_image.mode}")
        
        # Sauvegarder l'image d'entrée
        input_image.save(temp_input_path, format='PNG')
        
        # Traiter l'image avec Bria.ai
        logger.info("Début du traitement avec Bria.ai")
        
        # Utiliser le pool de threads pour le traitement
        future = thread_pool.submit(process_with_bria, input_image, content_moderation)
        output_image = future.result()
        
        logger.info(f"Traitement terminé avec succès, mode de l'image résultante: {output_image.mode}")
        
        # Sauvegarder l'image après suppression de fond
        temp_bria_output = os.path.join(OUTPUT_FOLDER, f"{unique_id}_bria.png")
        output_image.save(temp_bria_output, format='PNG')
        
        # Déterminer le format approprié pour XnConvert
        xnconvert_format = 'jpeg' if output_format == 'jpg' else 'png'
        
        # Redimensionner et recadrer avec XnConvert
        bg_color = DEFAULT_BG_COLOR
        resize_success = resize_with_xnconvert(
            temp_bria_output, 
            temp_output_path,
            width=width,
            height=height,
            crop_position=crop_position,
            bg_color=bg_color,
            output_format=xnconvert_format,
            quality=80
        )
        
        if not resize_success:
            raise Exception("Échec du redimensionnement avec XnConvert")
        
        # Vérifier que le fichier existe
        if not os.path.exists(temp_output_path):
            raise Exception("Le fichier de sortie n'a pas été créé")
        
        # Ouvrir l'image résultante
        with open(temp_output_path, 'rb') as f:
            result_data = f.read()
        
        # Déterminer le type MIME
        mimetype = 'image/jpeg' if output_format == 'jpg' else 'image/png'
        
        # Utiliser le nom de fichier original avec la bonne extension
        original_filename = secure_filename(file.filename)
        base_name = os.path.splitext(original_filename)[0]
        download_name = f"{base_name}.{output_format}"
        
        # Préparer le BytesIO pour l'envoi
        img_io = BytesIO(result_data)
        img_io.seek(0)
        
        # Afficher les informations sur la taille de l'image
        img_size = img_io.getbuffer().nbytes
        logger.info(f"Taille de l'image à envoyer: {img_size} octets")
        
        # Envoyer l'image avec le bon type MIME
        logger.info(f"Envoi du fichier {output_format.upper()} au client avec le nom: {download_name}")
        response = send_file(
            img_io, 
            mimetype=mimetype,
            download_name=download_name,
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
        try:
            for temp_file in [temp_input_path, temp_bria_output, temp_output_path]:
                if 'temp_file' in locals() and os.path.exists(temp_file):
                    os.remove(temp_file)
                    logger.info(f"Fichier temporaire supprimé: {temp_file}")
        except Exception as e:
            logger.error(f"Erreur lors du nettoyage des fichiers temporaires: {str(e)}")
            
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
            'resize_method': 'xnconvert',
            'output_formats': ['jpg', 'png'],
            'features': ['background_removal', 'resize', 'crop'],
            'crop_positions': VALID_CROP_POSITIONS,
            'default_crop_position': DEFAULT_CROP_POSITION,
            'default_bg_color': DEFAULT_BG_COLOR
        }
    })

if __name__ == '__main__':
    # Pour la production, utilisez Gunicorn
    # gunicorn -w 4 -b 0.0.0.0:5000 --timeout 300 app:app
    app.run(host='0.0.0.0', port=5000, threaded=True)