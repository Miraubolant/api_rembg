from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import sys
import requests
import time
import subprocess
import tempfile
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
XNCONVERT_PATH = os.environ.get('XNCONVERT_PATH', '/usr/bin/xnconvert')  # Chemin vers l'exécutable XnConvert

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

# Fonction pour traiter une image avec XnConvert
def process_with_xnconvert(input_image, operations):
    """
    Traite une image avec XnConvert en ligne de commande
    
    Parameters:
    input_image (PIL.Image): Image à traiter
    operations (list): Liste des opérations XnConvert à appliquer
    
    Returns:
    PIL.Image: Image traitée
    """
    try:
        # Créer des fichiers temporaires pour l'entrée et la sortie
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_input:
            temp_input_path = temp_input.name
            
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_output:
            temp_output_path = temp_output.name
        
        # Sauvegarder l'image d'entrée dans le fichier temporaire
        input_image.save(temp_input_path, format='PNG')
        
        # Préparer la commande XnConvert (à ajuster selon vos besoins)
        cmd = [
            XNCONVERT_PATH,
            "-silent",  # Mode silencieux
            "-o", temp_output_path,  # Fichier de sortie
            "-overwrite",  # Écraser si le fichier existe
        ]
        
        # Traiter les opérations spécifiques (resize et crop)
        for op_name, op_value in operations:
            if op_name == "resize":
                # Format attendu: width,height,mode
                # Modes: 0=ignorer ratio, 1=conserver ratio, 2=compléter avec transparence
                parts = op_value.split(",")
                if len(parts) >= 2:
                    width, height = parts[0], parts[1]
                    mode = parts[2] if len(parts) > 2 else "1"  # Mode 1 (garder ratio) par défaut
                    
                    # Pour mode "Ajuster" de XnConvert
                    cmd.extend(["-resize", width, height, mode])
                    
                    # Paramètres supplémentaires
                    if len(parts) > 3 and parts[3] == "always":
                        cmd.extend(["-resizemajor", "a"])
                    else:
                        cmd.extend(["-resizemajor", "d"])  # d = downscale only (default)
                    
                    # Méthode de ré-échantillonnage
                    if len(parts) > 4:
                        resampling = parts[4].lower()
                        if resampling == "hanning":
                            cmd.extend(["-resample", "hanning"])
                        elif resampling == "lanczos":
                            cmd.extend(["-resample", "lanczos"])
                        elif resampling == "bicubic":
                            cmd.extend(["-resample", "bicubic"])
                        elif resampling == "bilinear":
                            cmd.extend(["-resample", "bilinear"])
                        elif resampling == "nearest":
                            cmd.extend(["-resample", "nearest"])
                    
            elif op_name == "crop":
                # Format attendu: x,y,width,height,bgcolor
                parts = op_value.split(",")
                
                if len(parts) >= 4:
                    x, y, width, height = parts[0:4]
                    cmd.extend(["-crop", x, y, width, height])
                    
                    # Si un fond est spécifié (r,g,b,a)
                    if len(parts) > 4:
                        bgcolor = parts[4]
                        cmd.extend(["-canvas_color", bgcolor])
                        
                    # Position (haut-gauche, centre, etc.)
                    if len(parts) > 5:
                        position = parts[5]
                        if position == "top-left":
                            cmd.extend(["-position", "tl"])
                        elif position == "top-center":
                            cmd.extend(["-position", "tc"])
                        elif position == "top-right":
                            cmd.extend(["-position", "tr"])
                        elif position == "center-left":
                            cmd.extend(["-position", "cl"])
                        elif position == "center":
                            cmd.extend(["-position", "cc"])
                        elif position == "center-right":
                            cmd.extend(["-position", "cr"])
                        elif position == "bottom-left":
                            cmd.extend(["-position", "bl"])
                        elif position == "bottom-center":
                            cmd.extend(["-position", "bc"])
                        elif position == "bottom-right":
                            cmd.extend(["-position", "br"])
                
            elif op_name == "bgcolor":
                # Format attendu: r,g,b,a (valeurs 0-255)
                cmd.extend(["-canvas_color", op_value])
                
            elif op_name == "resample":
                # Méthode de ré-échantillonnage
                cmd.extend(["-resample", op_value])
                
            elif op_name == "always":
                if op_value.lower() in ('yes', 'true', '1', 'y'):
                    cmd.extend(["-resizemajor", "a"])
                
            else:
                # Autres opérations génériques
                cmd.extend(["-" + op_name, op_value])
            
        # Ajouter le fichier d'entrée (doit être à la fin)
        cmd.append(temp_input_path)
        
        logger.info(f"Exécution de la commande XnConvert: {' '.join(cmd)}")
        
        # Exécuter XnConvert
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        
        if result.returncode != 0:
            logger.error(f"Erreur lors de l'exécution de XnConvert: {result.stderr}")
            raise Exception(f"Erreur XnConvert: {result.stderr}")
        
        # Ouvrir et retourner l'image de sortie
        output_image = Image.open(temp_output_path)
        processed_image = output_image.copy()  # Créer une copie pour pouvoir fermer l'original
        
        # Nettoyer les fichiers temporaires
        os.unlink(temp_input_path)
        os.unlink(temp_output_path)
        output_image.close()
        
        return processed_image
        
    except Exception as e:
        logger.error(f"Erreur lors du traitement avec XnConvert: {str(e)}")
        # Nettoyer les fichiers temporaires en cas d'erreur
        if 'temp_input_path' in locals():
            try:
                os.unlink(temp_input_path)
            except:
                pass
        if 'temp_output_path' in locals():
            try:
                os.unlink(temp_output_path)
            except:
                pass
        raise

# Fonction optimisée pour le redimensionnement et recadrage en une seule passe
def resize_and_crop_image(img, target_width, target_height):
    """
    Redimensionne et recadre une image pour atteindre exactement les dimensions cibles
    tout en préservant les proportions autant que possible.
    
    Stratégie:
    1. Redimensionner l'image pour qu'au moins une dimension corresponde à la cible
       tout en conservant le rapport hauteur/largeur
    2. Recadrer le surplus au centre pour obtenir exactement les dimensions cibles
    
    Args:
        img (PIL.Image): Image d'entrée
        target_width (int): Largeur cible
        target_height (int): Hauteur cible
        
    Returns:
        PIL.Image: Image redimensionnée et recadrée
    """
    # Obtenir les dimensions actuelles
    width, height = img.size
    
    # Calculer le rapport d'aspect cible et actuel
    target_ratio = target_width / target_height
    img_ratio = width / height
    
    if img_ratio > target_ratio:
        # L'image est plus large que la cible => adapter à la hauteur
        new_height = target_height
        new_width = int(new_height * img_ratio)
    else:
        # L'image est plus haute que la cible (ou a le même ratio) => adapter à la largeur
        new_width = target_width
        new_height = int(new_width / img_ratio)
    
    # Redimensionner l'image
    resized_img = img.resize((new_width, new_height), Image.LANCZOS)
    
    # Calculer les coordonnées de recadrage (centré)
    crop_x = (new_width - target_width) // 2
    crop_y = (new_height - target_height) // 2
    
    # Recadrer l'image
    cropped_img = resized_img.crop((
        crop_x,
        crop_y,
        crop_x + target_width,
        crop_y + target_height
    ))
    
    return cropped_img

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

@app.route('/process-image', methods=['POST', 'OPTIONS'])
def process_image_api():
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /process-image")
    
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
        # Récupérer les opérations à effectuer depuis les paramètres
        # Exemple: resize=800,600,1 crop=10,10,500,500
        operations = []
        for param, value in request.form.items():
            if param not in ['image', 'output_format', 'quality']:
                if value:
                    operations.append((param, value))
                else:
                    operations.append((param, ""))
        
        logger.info(f"Opérations demandées: {operations}")
        
        # Lire le fichier en mémoire
        file_data = file.read()
        input_image = Image.open(BytesIO(file_data))
        logger.info(f"Image ouverte, taille: {input_image.size}, mode: {input_image.mode}")
        
        # Optimiser l'image avant envoi (facultatif)
        input_image = optimize_image_for_processing(input_image)
        
        # Traiter l'image avec XnConvert
        logger.info("Début du traitement avec XnConvert")
        
        # Utiliser le pool de threads pour le traitement
        future = thread_pool.submit(process_with_xnconvert, input_image, operations)
        output_image = future.result()
        
        logger.info(f"Traitement terminé avec succès, mode de l'image résultante: {output_image.mode}")
        
        # Envoyer directement l'image via BytesIO
        logger.info("Préparation de l'image pour l'envoi")
        img_io = BytesIO()
        
        # Définir le format de sortie (configurable)
        output_format = request.form.get('output_format', 'PNG').upper()
        if output_format not in ['PNG', 'JPEG', 'JPG', 'WEBP']:
            output_format = 'PNG'  # Format par défaut
        
        # JPG ne supporte pas la transparence, donc convertir en RGB si nécessaire
        if output_format in ['JPEG', 'JPG'] and output_image.mode == 'RGBA':
            # Créer un fond blanc
            background = Image.new('RGB', output_image.size, (255, 255, 255))
            background.paste(output_image, mask=output_image.split()[3])  # 3 est le canal alpha
            output_image = background
        
        # Qualité pour les formats avec compression
        quality = int(request.form.get('quality', 90))
        
        # Sauvegarder dans le format approprié
        if output_format in ['JPEG', 'JPG']:
            output_image.save(img_io, format='JPEG', quality=quality)
            mimetype = 'image/jpeg'
            filename = 'image_processed.jpg'
        elif output_format == 'WEBP':
            output_image.save(img_io, format='WEBP', quality=quality)
            mimetype = 'image/webp'
            filename = 'image_processed.webp'
        else:  # PNG par défaut
            output_image.save(img_io, format='PNG')
            mimetype = 'image/png'
            filename = 'image_processed.png'
            
        img_io.seek(0)
        
        # Afficher les informations sur la taille de l'image
        img_size = img_io.getbuffer().nbytes
        logger.info(f"Taille de l'image à envoyer: {img_size} octets")
        
        # Envoyer l'image avec le bon type MIME
        logger.info(f"Envoi du fichier {output_format} au client")
        response = send_file(
            img_io, 
            mimetype=mimetype,
            download_name=filename,
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

@app.route('/resize-crop', methods=['POST', 'OPTIONS'])
def resize_crop_api():
    """
    Endpoint simplifié pour redimensionner et recadrer une image en une seule opération.
    Format attendu: dimension=1920x1080
    Sortie par défaut en JPG
    """
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /resize-crop")
    
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
    
    # Obtenir la dimension cible
    dimension = request.form.get('dimension', '1920x1080')
    try:
        width, height = map(int, dimension.lower().split('x'))
        logger.info(f"Dimension cible: {width}x{height}")
    except ValueError:
        logger.error(f"Format de dimension invalide: {dimension}")
        return jsonify({'error': 'Format de dimension invalide. Utilisez WIDTHxHEIGHT (ex: 1920x1080)'}), 400
    
    # Obtenir le format de sortie (JPG par défaut)
    output_format = request.form.get('format', 'jpg').upper()
    if output_format not in ['JPG', 'JPEG', 'PNG', 'WEBP']:
        output_format = 'JPG'  # Format par défaut
    
    # Obtenir la qualité (90 par défaut)
    try:
        quality = int(request.form.get('quality', 90))
        if quality < 1 or quality > 100:
            quality = 90
    except ValueError:
        quality = 90
    
    try:
        # Lire le fichier en mémoire
        file_data = file.read()
        input_image = Image.open(BytesIO(file_data))
        logger.info(f"Image ouverte, taille originale: {input_image.size}, mode: {input_image.mode}")
        
        # Utiliser une méthode optimisée pour resize et crop
        logger.info("Traitement d'image: redimensionnement et recadrage")
        output_image = resize_and_crop_image(input_image, width, height)
        
        logger.info(f"Traitement terminé, nouvelle taille: {output_image.size}")
        
        # Préparer l'image pour l'envoi
        img_io = BytesIO()
        
        # Convertir en RGB si nécessaire pour JPG
        if output_format in ['JPG', 'JPEG'] and output_image.mode in ['RGBA', 'P']:
            logger.info(f"Conversion du mode {output_image.mode} vers RGB pour JPG")
            # Créer un fond blanc pour la transparence
            background = Image.new('RGB', output_image.size, (255, 255, 255))
            if output_image.mode == 'RGBA':
                background.paste(output_image, mask=output_image.split()[3])
            else:
                background.paste(output_image)
            output_image = background
        
        # Sauvegarder dans le format approprié
        if output_format in ['JPEG', 'JPG']:
            output_image.save(img_io, format='JPEG', quality=quality)
            mimetype = 'image/jpeg'
            filename = 'image_resized.jpg'
        elif output_format == 'WEBP':
            output_image.save(img_io, format='WEBP', quality=quality)
            mimetype = 'image/webp'
            filename = 'image_resized.webp'
        else:  # PNG
            output_image.save(img_io, format='PNG')
            mimetype = 'image/png'
            filename = 'image_resized.png'
            
        img_io.seek(0)
        img_size = img_io.getbuffer().nbytes

        # Envoyer l'image traitée
        logger.info(f"Envoi de l'image traitée ({img_size} octets)")
        response = send_file(
            img_io, 
            mimetype=mimetype,
            download_name=filename,
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
        return jsonify({'error': f'Erreur pendant le traitement: {str(e)}'}), 500
    
    finally:
        # Nettoyer les ressources
        if 'input_image' in locals():
            input_image.close()
        if 'output_image' in locals():
            output_image.close()

@app.route('/xnresize', methods=['POST', 'OPTIONS'])
def xnresize_api():
    """
    Endpoint pour redimensionner et recadrer une image avec les mêmes paramètres que XnConvert GUI.
    """
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /xnresize")
    
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
        # Paramètres pour simuler exactement l'interface XnConvert
        
        # Paramètres de redimensionnement
        resize_width = request.form.get('resize_width', '1000')
        resize_height = request.form.get('resize_height', '1500')
        keep_ratio = request.form.get('keep_ratio', 'true').lower() in ('true', '1', 't', 'y', 'yes')
        resize_mode = request.form.get('resize_mode', 'fit')  # fit, stretch, or fill
        resize_always = request.form.get('resize_always', 'true').lower() in ('true', '1', 't', 'y', 'yes')
        resampling = request.form.get('resampling', 'hanning')  # hanning, lanczos, bicubic, bilinear, nearest
        
        # Paramètres de recadrage
        crop_width = request.form.get('crop_width', '1000')
        crop_height = request.form.get('crop_height', '1500')
        crop_mode = request.form.get('crop_mode', 'normal')  # normal, relative
        crop_keep_ratio = request.form.get('crop_keep_ratio', 'true').lower() in ('true', '1', 't', 'y', 'yes')
        bg_color = request.form.get('bg_color', '255,255,255,255')  # r,g,b,a format
        crop_position = request.form.get('crop_position', 'top-left')  # top-left, center, etc.
        
        # Paramètres de sortie
        output_format = request.form.get('output_format', 'jpg').upper()
        quality = int(request.form.get('quality', 90))
        
        # Préparer les opérations
        operations = []
        
        # Paramètre de redimensionnement
        resize_mode_num = '1'  # Par défaut: conserver le ratio
        if resize_mode == 'stretch':
            resize_mode_num = '0'  # Ignorer le ratio (étirer)
        elif resize_mode == 'fill':
            resize_mode_num = '2'  # Compléter avec transparence
            
        if not keep_ratio:
            resize_mode_num = '0'  # Ignorer le ratio si keep_ratio est désactivé
            
        resize_params = f"{resize_width},{resize_height},{resize_mode_num}"
        if resize_always:
            resize_params += ",always"
        resize_params += f",{resampling}"
        operations.append(("resize", resize_params))
        
        # Paramètre de recadrage
        crop_params = f"0,0,{crop_width},{crop_height},{bg_color},{crop_position}"
        operations.append(("crop", crop_params))
        
        # Lire le fichier en mémoire
        file_data = file.read()
        input_image = Image.open(BytesIO(file_data))
        logger.info(f"Image ouverte, taille: {input_image.size}, mode: {input_image.mode}")
        
        # Traiter l'image avec XnConvert
        logger.info("Début du traitement avec XnConvert")
        
        # Utiliser le pool de threads pour le traitement
        future = thread_pool.submit(process_with_xnconvert, input_image, operations)
        output_image = future.result()
        
        logger.info(f"Traitement terminé avec succès, mode de l'image résultante: {output_image.mode}")
        
        # Envoyer directement l'image via BytesIO
        logger.info("Préparation de l'image pour l'envoi")
        img_io = BytesIO()
        
        # JPG ne supporte pas la transparence, donc convertir en RGB si nécessaire
        if output_format in ['JPEG', 'JPG'] and output_image.mode == 'RGBA':
            # Créer un fond blanc
            background = Image.new('RGB', output_image.size, (255, 255, 255))
            background.paste(output_image, mask=output_image.split()[3])  # 3 est le canal alpha
            output_image = background
        
        # Sauvegarder dans le format approprié
        if output_format in ['JPEG', 'JPG']:
            output_image.save(img_io, format='JPEG', quality=quality)
            mimetype = 'image/jpeg'
            filename = 'image_processed.jpg'
        elif output_format == 'WEBP':
            output_image.save(img_io, format='WEBP', quality=quality)
            mimetype = 'image/webp'
            filename = 'image_processed.webp'
        else:  # PNG par défaut
            output_image.save(img_io, format='PNG')
            mimetype = 'image/png'
            filename = 'image_processed.png'
            
        img_io.seek(0)
        
        # Afficher les informations sur la taille de l'image
        img_size = img_io.getbuffer().nbytes
        logger.info(f"Taille de l'image à envoyer: {img_size} octets")
        
        # Envoyer l'image avec le bon type MIME
        logger.info(f"Envoi du fichier {output_format} au client")
        response = send_file(
            img_io, 
            mimetype=mimetype,
            download_name=filename,
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
        'features': {
            'background_removal': BRIA_API_TOKEN is not None,
            'image_processing': os.path.exists(XNCONVERT_PATH)
        }
    })

if __name__ == '__main__':
    # Pour la production, utilisez Gunicorn
    # gunicorn -w 4 -b 0.0.0.0:5000 --timeout 300 app:app
    app.run(host='0.0.0.0', port=5000, threaded=True)