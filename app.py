from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import sys
import requests
import time
from werkzeug.utils import secure_filename
import uuid
from io import BytesIO
from PIL import Image, ImageFilter
import subprocess
import shutil
import concurrent.futures
import threading
import logging
import logging.handlers
import cv2
import numpy as np
from tqdm import tqdm

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

# Paramètres de redimensionnement par défaut
DEFAULT_RESIZE_PARAMS = {
    'RESIZE_MODE': 'fit',        # Mode de redimensionnement (fit, stretch, fill)
    'KEEP_RATIO': 'true',        # Conserver le ratio d'aspect
    'RESAMPLING': 'hanning',     # Méthode de ré-échantillonnage
    'CROP_POSITION': 'center',   # Position du recadrage (center, top, bottom, left, right)
    'BG_COLOR': 'white',         # Couleur de fond
    'BG_ALPHA': '255'            # Alpha pour la couleur de fond (0-255)
}

# Mapping des méthodes de rééchantillonnage pour PIL
RESAMPLING_METHODS = {
    'nearest': Image.NEAREST,
    'box': Image.BOX,
    'bilinear': Image.BILINEAR,
    'hamming': Image.HAMMING,
    'bicubic': Image.BICUBIC,
    'lanczos': Image.LANCZOS,
    'hanning': Image.LANCZOS,  # Hanning n'existe pas dans PIL, on utilise Lanczos comme alternative
}

# Vérification de la disponibilité des outils externes
IMAGEMAGICK_AVAILABLE = shutil.which('convert') is not None
GRAPHICSMAGICK_AVAILABLE = shutil.which('gm') is not None

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

def resize_with_imagemagick(input_path, output_path, width, height, resize_params):
    """
    Redimensionne une image avec ImageMagick
    
    Args:
        input_path: Chemin vers l'image d'entrée
        output_path: Chemin vers l'image de sortie
        width: Largeur cible
        height: Hauteur cible
        resize_params: Dictionnaire des paramètres de redimensionnement
    """
    try:
        resize_mode = resize_params.get('RESIZE_MODE', DEFAULT_RESIZE_PARAMS['RESIZE_MODE']).lower()
        keep_ratio = resize_params.get('KEEP_RATIO', DEFAULT_RESIZE_PARAMS['KEEP_RATIO']).lower() in ('true', '1', 't', 'y', 'yes')
        resampling = resize_params.get('RESAMPLING', DEFAULT_RESIZE_PARAMS['RESAMPLING']).lower()
        crop_position = resize_params.get('CROP_POSITION', DEFAULT_RESIZE_PARAMS['CROP_POSITION']).lower()
        bg_color = resize_params.get('BG_COLOR', DEFAULT_RESIZE_PARAMS['BG_COLOR'])
        bg_alpha_str = resize_params.get('BG_ALPHA', DEFAULT_RESIZE_PARAMS['BG_ALPHA'])
        
        try:
            bg_alpha = int(bg_alpha_str)
            bg_alpha = max(0, min(255, bg_alpha))  # Limiter entre 0 et 255
        except ValueError:
            bg_alpha = 255  # Valeur par défaut en cas d'erreur
            
        # Conversion de la méthode de rééchantillonnage pour ImageMagick
        im_resampling_map = {
            'nearest': 'point',
            'box': 'box',
            'bilinear': 'bilinear',
            'hamming': 'hamming',
            'bicubic': 'bicubic',
            'hanning': 'hanning',
            'lanczos': 'lanczos'
        }
        im_resampling = im_resampling_map.get(resampling, 'lanczos')
        
        # Création de la commande ImageMagick
        cmd = ['convert', input_path]
        
        # Définir la couleur de fond (avec transparence si nécessaire)
        if bg_alpha < 255:
            # Convertir la couleur en format rgba
            cmd.extend(['-background', f'{bg_color}'])
            cmd.extend(['-alpha', 'set'])
        else:
            cmd.extend(['-background', bg_color])
        
        # Appliquer le filtre de rééchantillonnage
        cmd.extend(['-filter', im_resampling])
        
        # Appliquer le redimensionnement selon le mode
        if resize_mode == 'fit':
            if keep_ratio:
                cmd.extend(['-resize', f'{width}x{height}'])
                cmd.extend(['-gravity', 'center', '-extent', f'{width}x{height}'])
            else:
                cmd.extend(['-resize', f'{width}x{height}!'])
        elif resize_mode == 'stretch':
            cmd.extend(['-resize', f'{width}x{height}!'])
        elif resize_mode == 'fill':
            # Pour le mode fill, on redimensionne pour couvrir puis on recadre
            cmd.extend(['-resize', f'{width}x{height}^'])
            
            # Définir la gravité pour le recadrage selon crop_position
            gravity_map = {
                'center': 'center',
                'top': 'north',
                'bottom': 'south',
                'left': 'west',
                'right': 'east'
            }
            gravity = gravity_map.get(crop_position, 'center')
            cmd.extend(['-gravity', gravity])
            
            # Recadrer pour obtenir les dimensions exactes
            cmd.extend(['-extent', f'{width}x{height}'])
        
        # Ajouter le chemin de sortie
        cmd.append(output_path)
        
        # Exécuter la commande
        subprocess.run(cmd, check=True, capture_output=True)
        logger.info(f"Redimensionnement avec ImageMagick réussi: {' '.join(cmd)}")
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Erreur ImageMagick: {e.stderr.decode() if e.stderr else str(e)}")
        raise Exception(f"Erreur lors du redimensionnement avec ImageMagick: {str(e)}")
    except Exception as e:
        logger.error(f"Erreur générale avec ImageMagick: {str(e)}")
        raise

def resize_with_graphicsmagick(input_path, output_path, width, height, resize_params):
    """
    Redimensionne une image avec GraphicsMagick
    
    Args:
        input_path: Chemin vers l'image d'entrée
        output_path: Chemin vers l'image de sortie
        width: Largeur cible
        height: Hauteur cible
        resize_params: Dictionnaire des paramètres de redimensionnement
    """
    try:
        resize_mode = resize_params.get('RESIZE_MODE', DEFAULT_RESIZE_PARAMS['RESIZE_MODE']).lower()
        keep_ratio = resize_params.get('KEEP_RATIO', DEFAULT_RESIZE_PARAMS['KEEP_RATIO']).lower() in ('true', '1', 't', 'y', 'yes')
        resampling = resize_params.get('RESAMPLING', DEFAULT_RESIZE_PARAMS['RESAMPLING']).lower()
        crop_position = resize_params.get('CROP_POSITION', DEFAULT_RESIZE_PARAMS['CROP_POSITION']).lower()
        bg_color = resize_params.get('BG_COLOR', DEFAULT_RESIZE_PARAMS['BG_COLOR'])
        bg_alpha_str = resize_params.get('BG_ALPHA', DEFAULT_RESIZE_PARAMS['BG_ALPHA'])
        
        try:
            bg_alpha = int(bg_alpha_str)
            bg_alpha = max(0, min(255, bg_alpha))  # Limiter entre 0 et 255
        except ValueError:
            bg_alpha = 255  # Valeur par défaut en cas d'erreur
            
        # Conversion de la méthode de rééchantillonnage pour GraphicsMagick
        gm_resampling_map = {
            'nearest': 'Point',
            'box': 'Box',
            'bilinear': 'Triangle',
            'hamming': 'Hamming',
            'bicubic': 'Cubic',
            'hanning': 'Hanning',
            'lanczos': 'Lanczos'
        }
        gm_resampling = gm_resampling_map.get(resampling, 'Lanczos')
        
        # Création de la commande GraphicsMagick
        cmd = ['gm', 'convert']
        
        # Définir la méthode de rééchantillonnage
        cmd.extend(['-filter', gm_resampling])
        
        # Définir la couleur de fond
        cmd.extend(['-background', f'{bg_color}'])
        
        if bg_alpha < 255:
            cmd.extend(['-alpha', 'on'])
        
        # Ajouter le chemin d'entrée
        cmd.append(input_path)
        
        # Appliquer le redimensionnement selon le mode
        if resize_mode == 'fit':
            if keep_ratio:
                cmd.extend(['-resize', f'{width}x{height}'])
                cmd.extend(['-gravity', 'center', '-extent', f'{width}x{height}'])
            else:
                cmd.extend(['-resize', f'{width}x{height}!'])
        elif resize_mode == 'stretch':
            cmd.extend(['-resize', f'{width}x{height}!'])
        elif resize_mode == 'fill':
            # Pour le mode fill, on redimensionne pour couvrir puis on recadre
            cmd.extend(['-resize', f'{width}x{height}^'])
            
            # Définir la gravité pour le recadrage selon crop_position
            gravity_map = {
                'center': 'center',
                'top': 'north',
                'bottom': 'south',
                'left': 'west',
                'right': 'east'
            }
            gravity = gravity_map.get(crop_position, 'center')
            cmd.extend(['-gravity', gravity])
            
            # Recadrer pour obtenir les dimensions exactes
            cmd.extend(['-extent', f'{width}x{height}'])
        
        # Ajouter le chemin de sortie
        cmd.append(output_path)
        
        # Exécuter la commande
        subprocess.run(cmd, check=True, capture_output=True)
        logger.info(f"Redimensionnement avec GraphicsMagick réussi: {' '.join(cmd)}")
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Erreur GraphicsMagick: {e.stderr.decode() if e.stderr else str(e)}")
        raise Exception(f"Erreur lors du redimensionnement avec GraphicsMagick: {str(e)}")
    except Exception as e:
        logger.error(f"Erreur générale avec GraphicsMagick: {str(e)}")
        raise

def resize_with_pil(image, width, height, resize_params):
    """
    Redimensionne une image avec PIL (Pillow)
    
    Args:
        image: Image PIL à redimensionner
        width: Largeur cible
        height: Hauteur cible
        resize_params: Dictionnaire de paramètres de redimensionnement
        
    Returns:
        Image PIL redimensionnée
    """
    try:
        # Extraire les paramètres
        resize_mode = resize_params.get('RESIZE_MODE', DEFAULT_RESIZE_PARAMS['RESIZE_MODE']).lower()
        keep_ratio = resize_params.get('KEEP_RATIO', DEFAULT_RESIZE_PARAMS['KEEP_RATIO']).lower() in ('true', '1', 't', 'y', 'yes')
        resampling = resize_params.get('RESAMPLING', DEFAULT_RESIZE_PARAMS['RESAMPLING']).lower()
        crop_position = resize_params.get('CROP_POSITION', DEFAULT_RESIZE_PARAMS['CROP_POSITION']).lower()
        bg_color = resize_params.get('BG_COLOR', DEFAULT_RESIZE_PARAMS['BG_COLOR'])
        bg_alpha_str = resize_params.get('BG_ALPHA', DEFAULT_RESIZE_PARAMS['BG_ALPHA'])
        
        try:
            bg_alpha = int(bg_alpha_str)
            bg_alpha = max(0, min(255, bg_alpha))  # Limiter entre 0 et 255
        except ValueError:
            bg_alpha = 255  # Valeur par défaut en cas d'erreur
        
        # Déterminer la méthode de rééchantillonnage
        resampling_method = RESAMPLING_METHODS.get(resampling, Image.LANCZOS)
        
        # Dimensions originales
        orig_width, orig_height = image.size
        logger.info(f"Dimensions originales: {orig_width}x{orig_height}")
        logger.info(f"Dimensions cibles: {width}x{height}")
        logger.info(f"Mode: {resize_mode}, Keep ratio: {keep_ratio}, Resampling: {resampling}")
        
        # Si les dimensions sont déjà correctes, retourner l'image originale
        if orig_width == width and orig_height == height:
            logger.info("Aucun redimensionnement nécessaire, dimensions déjà correctes")
            return image.copy()
        
        # Préparer l'image résultante
        result_image = None
        
        # Mode "fit" - Ajuste l'image dans les dimensions cibles tout en conservant le ratio
        if resize_mode == 'fit':
            # Créer un fond transparent ou de couleur
            result_image = Image.new('RGBA', (width, height), color=bg_color + (bg_alpha,))
            
            # Déterminer les nouvelles dimensions tout en conservant le ratio
            if keep_ratio:
                # Calculer le ratio pour conserver les proportions
                ratio = min(width / orig_width, height / orig_height)
                new_width = int(orig_width * ratio)
                new_height = int(orig_height * ratio)
                
                # Redimensionner l'image
                resized = image.resize((new_width, new_height), resampling_method)
                
                # Calculer la position pour centrer l'image
                x_offset = (width - new_width) // 2
                y_offset = (height - new_height) // 2
                
                # Placer l'image redimensionnée sur le fond
                result_image.paste(resized, (x_offset, y_offset), resized if resized.mode == 'RGBA' else None)
            else:
                # Redimensionner sans conserver le ratio
                resized = image.resize((width, height), resampling_method)
                result_image = resized
        
        # Mode "stretch" - Étire l'image aux dimensions exactes
        elif resize_mode == 'stretch':
            result_image = image.resize((width, height), resampling_method)
            
        # Mode "fill" - Remplit entièrement la zone cible, recadre si nécessaire
        elif resize_mode == 'fill':
            # Calculer le ratio pour remplir complètement
            ratio = max(width / orig_width, height / orig_height)
            new_width = int(orig_width * ratio)
            new_height = int(orig_height * ratio)
            
            # Redimensionner l'image pour qu'elle couvre la zone
            resized = image.resize((new_width, new_height), resampling_method)
            
            # Calculer les coordonnées de recadrage
            if crop_position == 'center':
                left = (new_width - width) // 2
                top = (new_height - height) // 2
            elif crop_position == 'top':
                left = (new_width - width) // 2
                top = 0
            elif crop_position == 'bottom':
                left = (new_width - width) // 2
                top = new_height - height
            elif crop_position == 'left':
                left = 0
                top = (new_height - height) // 2
            elif crop_position == 'right':
                left = new_width - width
                top = (new_height - height) // 2
            else:  # Par défaut, centre
                left = (new_width - width) // 2
                top = (new_height - height) // 2
                
            # Recadrer l'image
            right = left + width
            bottom = top + height
            result_image = resized.crop((left, top, right, bottom))
        
        return result_image
        
    except Exception as e:
        logger.error(f"Erreur lors du redimensionnement avec PIL: {str(e)}")
        raise

# Fonctions pour le traitement du visage (intégrées depuis le second code)
def crop_below_mouth(image):
    """
    Détecte le visage sur une image PIL, garde uniquement la partie en dessous de la bouche,
    et redimensionne l'image aux dimensions d'origine.
    
    Args:
        image: Image PIL à traiter
        
    Returns:
        Image PIL traitée ou None en cas d'échec
    """
    try:
        # Convertir l'image PIL en format OpenCV
        img_array = np.array(image)
        img = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
        
        # Obtenir les dimensions originales de l'image
        original_height, original_width = img.shape[:2]
        
        # Charger les classificateurs pré-entraînés pour la détection du visage
        face_cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        
        if not os.path.exists(face_cascade_path):
            logger.error(f"Fichier de cascade introuvable: {face_cascade_path}")
            return None
            
        face_cascade = cv2.CascadeClassifier(face_cascade_path)
        
        # Convertir en niveau de gris pour la détection
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # Détecter les visages avec OpenCV
        faces = face_cascade.detectMultiScale(gray, 1.1, 4)
        
        if len(faces) == 0:
            logger.info("Aucun visage détecté dans l'image")
            return None
        
        # Prendre le premier visage détecté
        x, y, w, h = faces[0]
        
        # Estimer la position de la bouche en fonction des proportions du visage (environ 70-75% depuis le haut)
        mouth_y = y + int(h * 0.75)
        
        # Découper l'image pour ne garder que la partie en dessous de la bouche
        cropped = img[mouth_y:original_height, 0:original_width]
        
        if cropped.size == 0:
            logger.info("Échec de la découpe: image résultante vide")
            return None
        
        # Redimensionner l'image coupée aux dimensions d'origine
        resized = cv2.resize(cropped, (original_width, original_height), interpolation=cv2.INTER_LANCZOS4)
        
        # Convertir l'image OpenCV en image PIL
        resized_rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        result_image = Image.fromarray(resized_rgb)
        
        return result_image
        
    except Exception as e:
        logger.error(f"Erreur lors du traitement du visage: {str(e)}")
        return None

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
        # supprimer car pas utilise finalement input_image = optimize_image_for_processing(input_image)
        
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

@app.route('/resize', methods=['POST', 'OPTIONS'])
def resize_image_api():
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /resize")
    
    # Récupérer les dimensions requises
    try:
        width = int(request.args.get('width', 0))
        height = int(request.args.get('height', 0))
        
        if width <= 0 or height <= 0:
            logger.error(f"Dimensions invalides: largeur={width}, hauteur={height}")
            return jsonify({'error': 'Les dimensions width et height doivent être des entiers positifs'}), 400
        
    except ValueError:
        logger.error("Dimensions non numériques fournies")
        return jsonify({'error': 'Les dimensions width et height doivent être des entiers positifs'}), 400
    
    # Récupérer les paramètres de redimensionnement (avec valeurs par défaut)
    resize_params = {
        'RESIZE_MODE': request.args.get('mode', DEFAULT_RESIZE_PARAMS['RESIZE_MODE']),
        'KEEP_RATIO': request.args.get('keep_ratio', DEFAULT_RESIZE_PARAMS['KEEP_RATIO']),
        'RESAMPLING': request.args.get('resampling', DEFAULT_RESIZE_PARAMS['RESAMPLING']),
        'CROP_POSITION': request.args.get('crop', DEFAULT_RESIZE_PARAMS['CROP_POSITION']),
        'BG_COLOR': request.args.get('bg_color', DEFAULT_RESIZE_PARAMS['BG_COLOR']),
        'BG_ALPHA': request.args.get('bg_alpha', DEFAULT_RESIZE_PARAMS['BG_ALPHA'])
    }
    
    # Récupérer l'outil à utiliser pour le redimensionnement
    resize_tool = request.args.get('tool', 'auto').lower()
    
    logger.info(f"Paramètres de redimensionnement: {resize_params}, outil: {resize_tool}")
    
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
        # Générer des noms de fichiers uniques pour les fichiers temporaires
        unique_id = str(uuid.uuid4())
        input_filename = secure_filename(f"{unique_id}_{file.filename}")
        input_path = os.path.join(UPLOAD_FOLDER, input_filename)
        output_filename = f"resized_{input_filename}"
        output_path = os.path.join(OUTPUT_FOLDER, output_filename)
        
        # Déterminer le format de sortie
        output_format = 'JPEG'
        mimetype = 'image/jpeg'
        file_ext = 'jpg'
        
        # Si le fichier d'entrée est un PNG, conserver le format PNG
        if file.filename.lower().endswith('.png'):
            output_format = 'PNG'
            mimetype = 'image/png'
            file_ext = 'png'
        
        # Sauvegarder l'image d'entrée sur le disque
        file_data = file.read()
        with open(input_path, 'wb') as f:
            f.write(file_data)
        
        # Sélectionner l'outil de redimensionnement
        if resize_tool == 'auto':
            # Priorité: ImageMagick > GraphicsMagick > PIL
            if IMAGEMAGICK_AVAILABLE:
                resize_tool = 'imagemagick'
            elif GRAPHICSMAGICK_AVAILABLE:
                resize_tool = 'graphicsmagick'
            else:
                resize_tool = 'pil'
        
        logger.info(f"Utilisation de l'outil: {resize_tool}")
        
        # Redimensionner l'image avec l'outil sélectionné
        if resize_tool == 'imagemagick' and IMAGEMAGICK_AVAILABLE:
            # Redimensionner avec ImageMagick
            resize_with_imagemagick(input_path, output_path, width, height, resize_params)
            
            # Charger l'image résultante pour l'envoyer
            output_image = Image.open(output_path)
            
        elif resize_tool == 'graphicsmagick' and GRAPHICSMAGICK_AVAILABLE:
            # Redimensionner avec GraphicsMagick
            resize_with_graphicsmagick(input_path, output_path, width, height, resize_params)
            
            # Charger l'image résultante pour l'envoyer
            output_image = Image.open(output_path)
            
        else:
            # Redimensionner avec PIL
            input_image = Image.open(BytesIO(file_data))
            output_image = resize_with_pil(input_image, width, height, resize_params)
            
            # Sauvegarder pour conserver les logs
            output_image.save(output_path)
        
        # Préparer l'image pour l'envoi
        img_io = BytesIO()
        
        # Ajuster le format de sortie selon le mode de l'image
        if output_image.mode == 'RGBA' and output_format == 'JPEG':
            # JPEG ne supporte pas la transparence, convertir en RGB
            output_image = output_image.convert('RGB')
            output_format = 'JPEG'
        
        # Sauvegarder dans le buffer
        if output_format == 'JPEG':
            output_image.save(img_io, format='JPEG', quality=90)
        else:
            output_image.save(img_io, format='PNG')
            
        img_io.seek(0)
        
        # Afficher les informations sur la taille de l'image
        img_size = img_io.getbuffer().nbytes
        logger.info(f"Taille de l'image redimensionnée: {img_size} octets")
        
        # Envoyer l'image avec le bon type MIME
        response = send_file(
            img_io, 
            mimetype=mimetype,
            download_name=f"resized_image.{file_ext}",
            as_attachment=True
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
        return jsonify({'error': f'Erreur pendant le redimensionnement: {str(e)}', 'details': error_details}), 500
    
    finally:
        # Nettoyer les ressources
        if 'input_image' in locals():
            input_image.close()
        if 'output_image' in locals():
            output_image.close()
            
        # Nettoyer les fichiers temporaires
        try:
            if 'input_path' in locals() and os.path.exists(input_path):
                os.remove(input_path)
            if 'output_path' in locals() and os.path.exists(output_path):
                os.remove(output_path)
        except Exception as e:
            logger.warning(f"Erreur lors du nettoyage des fichiers temporaires: {str(e)}")

@app.route('/crop-below-mouth', methods=['POST', 'OPTIONS'])
def crop_below_mouth_api():
    """
    Route API pour la fonctionnalité de détection de visage et découpage en dessous de la bouche.
    Cette route prend une image en entrée et renvoie l'image redimensionnée avec uniquement
    la partie en dessous de la bouche.
    """
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /crop-below-mouth")
    
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
        
        # Traiter l'image pour détecter le visage et découper en dessous de la bouche
        future = thread_pool.submit(crop_below_mouth, input_image)
        output_image = future.result()
        
        if output_image is None:
            logger.error("Échec du traitement: aucun visage détecté ou erreur de découpage")
            return jsonify({'error': 'Échec du traitement. Aucun visage détecté ou erreur lors du découpage.'}), 400
        
        logger.info(f"Traitement terminé avec succès, taille de l'image résultante: {output_image.size}")
        
        # Préparer l'image pour l'envoi
        img_io = BytesIO()
        
        # Déterminer le format de sortie
        output_format = 'JPEG'
        mimetype = 'image/jpeg'
        
        # Si l'image d'entrée est un PNG, conserver le format PNG
        if file.filename.lower().endswith('.png'):
            output_format = 'PNG'
            mimetype = 'image/png'
        
        # Ajuster le format de sortie selon le mode de l'image
        if output_image.mode == 'RGBA' and output_format == 'JPEG':
            output_image = output_image.convert('RGB')
        
        # Sauvegarder dans le buffer
        if output_format == 'JPEG':
            output_image.save(img_io, format='JPEG', quality=90)
        else:
            output_image.save(img_io, format='PNG')
            
        img_io.seek(0)
        
        # Afficher les informations sur la taille de l'image
        img_size = img_io.getbuffer().nbytes
        logger.info(f"Taille de l'image à envoyer: {img_size} octets")
        
        # Envoyer l'image avec le bon type MIME
        response = send_file(
            img_io, 
            mimetype=mimetype,
            download_name=f"below_mouth_{file.filename}",
            as_attachment=True
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
        if 'output_image' in locals() and output_image is not None:
            output_image.close()

# Route combinée pour traiter une image avec plusieurs opérations
@app.route('/process-image', methods=['POST', 'OPTIONS'])
def process_image_api():
    """
    Route API pour traiter une image avec plusieurs opérations en séquence.
    Options disponibles: suppression de fond, découpage sous la bouche, redimensionnement.
    """
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /process-image")
    
    # Récupérer les paramètres de traitement
    remove_bg = request.args.get('remove_bg', 'false').lower() in ('true', '1', 't', 'y', 'yes')
    crop_mouth = request.args.get('crop_mouth', 'false').lower() in ('true', '1', 't', 'y', 'yes')
    resize = request.args.get('resize', 'false').lower() in ('true', '1', 't', 'y', 'yes')
    content_moderation = request.args.get('content_moderation', 'false').lower() in ('true', '1', 't', 'y', 'yes')
    
    # Paramètres de redimensionnement
    width = 0
    height = 0
    if resize:
        try:
            width = int(request.args.get('width', 0))
            height = int(request.args.get('height', 0))
            
            if width <= 0 or height <= 0:
                logger.error(f"Dimensions invalides: largeur={width}, hauteur={height}")
                return jsonify({'error': 'Pour le redimensionnement, les dimensions width et height doivent être des entiers positifs'}), 400
        except ValueError:
            logger.error("Dimensions non numériques fournies")
            return jsonify({'error': 'Les dimensions width et height doivent être des entiers positifs'}), 400
    
    # Récupérer les paramètres de redimensionnement (si nécessaire)
    resize_params = {
        'RESIZE_MODE': request.args.get('mode', DEFAULT_RESIZE_PARAMS['RESIZE_MODE']),
        'KEEP_RATIO': request.args.get('keep_ratio', DEFAULT_RESIZE_PARAMS['KEEP_RATIO']),
        'RESAMPLING': request.args.get('resampling', DEFAULT_RESIZE_PARAMS['RESAMPLING']),
        'CROP_POSITION': request.args.get('crop', DEFAULT_RESIZE_PARAMS['CROP_POSITION']),
        'BG_COLOR': request.args.get('bg_color', DEFAULT_RESIZE_PARAMS['BG_COLOR']),
        'BG_ALPHA': request.args.get('bg_alpha', DEFAULT_RESIZE_PARAMS['BG_ALPHA'])
    }
    
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
        current_image = Image.open(BytesIO(file_data))
        logger.info(f"Image ouverte, taille: {current_image.size}, mode: {current_image.mode}")
        
        # Chaîne de traitement
        if remove_bg:
            logger.info("Début du traitement avec Bria.ai pour suppression de fond")
            future = thread_pool.submit(process_with_bria, current_image, content_moderation)
            current_image = future.result()
            logger.info(f"Suppression de fond terminée, nouveau mode: {current_image.mode}")
        
        if crop_mouth:
            logger.info("Début du découpage sous la bouche")
            mouth_result = crop_below_mouth(current_image)
            if mouth_result is None:
                logger.warning("Aucun visage détecté, le découpage sous la bouche est ignoré")
            else:
                current_image = mouth_result
                logger.info("Découpage sous la bouche terminé")
                
        if resize and width > 0 and height > 0:
            logger.info(f"Début du redimensionnement à {width}x{height}")
            current_image = resize_with_pil(current_image, width, height, resize_params)
            logger.info("Redimensionnement terminé")
        
        # Déterminer le format de sortie
        output_format = 'JPEG'
        mimetype = 'image/jpeg'
        file_ext = 'jpg'
        
        # Si le fichier d'entrée est un PNG ou si nous avons supprimé le fond (transparence), utiliser PNG
        if file.filename.lower().endswith('.png') or (remove_bg and current_image.mode == 'RGBA'):
            output_format = 'PNG'
            mimetype = 'image/png'
            file_ext = 'png'
        
        # Préparer l'image pour l'envoi
        img_io = BytesIO()
        
        # Ajuster le format de sortie selon le mode de l'image
        if current_image.mode == 'RGBA' and output_format == 'JPEG':
            # JPEG ne supporte pas la transparence, convertir en RGB
            current_image = current_image.convert('RGB')
        
        # Sauvegarder dans le buffer
        if output_format == 'JPEG':
            current_image.save(img_io, format='JPEG', quality=90)
        else:
            current_image.save(img_io, format='PNG')
            
        img_io.seek(0)
        
        # Afficher les informations sur la taille de l'image
        img_size = img_io.getbuffer().nbytes
        logger.info(f"Taille de l'image finale: {img_size} octets")
        
        # Envoyer l'image avec le bon type MIME
        response = send_file(
            img_io, 
            mimetype=mimetype,
            download_name=f"processed_image.{file_ext}",
            as_attachment=True
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
        if 'current_image' in locals():
            current_image.close()

if __name__ == '__main__':
    # Configuration du serveur
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'false').lower() in ('true', '1', 't', 'y', 'yes')
    
    logger.info(f"Démarrage du serveur sur le port {port}, debug={debug}")
    logger.info(f"Origines CORS autorisées: {ALLOWED_ORIGINS}")
    logger.info(f"IPs autorisées: {AUTHORIZED_IPS}")
    logger.info(f"ImageMagick disponible: {IMAGEMAGICK_AVAILABLE}")
    logger.info(f"GraphicsMagick disponible: {GRAPHICSMAGICK_AVAILABLE}")
    
    app.run(host='0.0.0.0', port=port, debug=debug)