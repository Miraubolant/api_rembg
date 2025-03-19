from quart import Quart, request, jsonify, send_file
from quart_cors import cors
import os
import sys
import aiohttp
import time
from werkzeug.utils import secure_filename
import uuid
from io import BytesIO
from PIL import Image
import asyncio
from concurrent.futures import ThreadPoolExecutor
import functools
import logging
import logging.handlers

app = Quart(__name__)

# Récupérer les variables d'environnement
BRIA_API_TOKEN = os.environ.get('BRIA_API_TOKEN')

# Obtenir les domaines autorisés depuis une variable d'environnement
allowed_origins_str = os.environ.get('ALLOWED_ORIGINS', 'https://miremover.fr,http://miremover.fr')
ALLOWED_ORIGINS = [origin.strip() for origin in allowed_origins_str.split(',')]

# Configuration des IPs autorisées
AUTHORIZED_IPS = os.environ.get('AUTHORIZED_IPS', '127.0.0.1').split(',')

# Configuration CORS avec les domaines autorisés
app = cors(app, allow_origin=ALLOWED_ORIGINS)

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

# Créer un pool de threads pour les opérations intensives
thread_pool = ThreadPoolExecutor(max_workers=4)

# Timeout pour les requêtes API externes
BRIA_API_TIMEOUT = aiohttp.ClientTimeout(total=30)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Fonction pour exécuter des tâches bloquantes dans un thread
async def run_in_thread(func, *args, **kwargs):
    """Exécute une fonction dans un thread séparé"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        thread_pool, 
        functools.partial(func, *args, **kwargs)
    )

# Fonction pour optimiser l'image avant traitement
async def optimize_image_for_processing(image, max_size=1500):
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
        
        image = await run_in_thread(lambda: image.resize((new_width, new_height), Image.LANCZOS))
        logger.info(f"Image redimensionnée à {new_width}x{new_height}")
    
    return image

# Middleware pour vérifier l'IP source
@app.before_request
async def restrict_access_by_ip():
    # Autoriser toujours les requêtes OPTIONS pour CORS
    if request.method == 'OPTIONS':
        return None
        
    client_ip = request.remote_addr
    
    # Vérifier si l'IP est autorisée
    if client_ip not in AUTHORIZED_IPS:
        logger.warning(f"Tentative d'accès non autorisée depuis l'IP: {client_ip}")
        return jsonify({'error': 'Accès non autorisé'}), 403

async def process_with_bria(input_image, content_moderation=False):
    """Traitement asynchrone avec l'API Bria.ai RMBG 2.0"""
    try:
        # Vérifier si la clé API est disponible
        if not BRIA_API_TOKEN:
            raise Exception("Clé API Bria.ai non configurée. Veuillez définir la variable d'environnement BRIA_API_TOKEN.")
            
        # Sauvegarder l'image temporairement dans la mémoire
        temp_file = BytesIO()
        await run_in_thread(lambda: input_image.save(temp_file, format='PNG'))
        temp_file.seek(0)
        
        # Préparer la requête à l'API Bria
        url = "https://engine.prod.bria-api.com/v1/background/remove"
        headers = {
            "api_token": BRIA_API_TOKEN
        }
        
        logger.info("Envoi de l'image à Bria.ai API")
        
        # Utiliser aiohttp pour les requêtes asynchrones
        async with aiohttp.ClientSession(timeout=BRIA_API_TIMEOUT) as session:
            data = aiohttp.FormData()
            data.add_field('file', 
                          temp_file.getvalue(), 
                          filename='image.png', 
                          content_type='image/png')
            
            if content_moderation:
                data.add_field('content_moderation', 'true')
            
            async with session.post(url, headers=headers, data=data) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Erreur API Bria: {response.status} - {error_text}")
                    raise Exception(f"Erreur API Bria: {response.status} - {error_text}")
                
                result_data = await response.json()
                result_url = result_data.get('result_url')
                
                if not result_url:
                    raise Exception("Aucune URL de résultat retournée par Bria API")
                
                logger.info(f"Image traitée avec succès par Bria.ai, URL résultante: {result_url}")
                
                # Télécharger l'image résultante
                async with session.get(result_url) as image_response:
                    if image_response.status != 200:
                        raise Exception(f"Erreur lors du téléchargement de l'image résultante: {image_response.status}")
                    
                    image_data = await image_response.read()
                    result_image = await run_in_thread(lambda: Image.open(BytesIO(image_data)))
                    
                    return result_image
                    
    except Exception as e:
        logger.error(f"Erreur lors du traitement avec Bria.ai: {str(e)}")
        raise

@app.route('/remove-background', methods=['POST', 'OPTIONS'])
async def remove_background_api():
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /remove-background")
    
    # Récupérer le paramètre de modération de contenu
    content_moderation = request.args.get('content_moderation', 'false').lower() in ('true', '1', 't', 'y', 'yes')
    
    # Vérifier si une image a été envoyée
    if 'image' not in (await request.files):
        logger.error("Aucune image n'a été envoyée")
        return jsonify({'error': 'Aucune image n\'a été envoyée'}), 400
    
    files = await request.files
    file = files['image']
    logger.info(f"Fichier reçu: {file.filename}")
    
    # Vérifier si le fichier est valide
    if file.filename == '':
        logger.error("Nom de fichier vide")
        return jsonify({'error': 'Nom de fichier vide'}), 400
    
    if not allowed_file(file.filename):
        logger.error(f"Format de fichier non supporté: {file.filename}")
        return jsonify({'error': f'Format de fichier non supporté. Formats acceptés: {", ".join(ALLOWED_EXTENSIONS)}'}), 400
    
    try:
        # Générer un nom de fichier unique pour les logs et les références
        filename = secure_filename(file.filename)
        unique_filename = f"{uuid.uuid4()}_{filename}"
        input_path = os.path.join(UPLOAD_FOLDER, unique_filename)
        
        # Lire directement le fichier en mémoire
        file_data = await file.read()
        input_image = await run_in_thread(lambda: Image.open(BytesIO(file_data)))
        logger.info(f"Image ouverte, taille: {input_image.size}, mode: {input_image.mode}")
        
        # Optimiser l'image avant envoi
        input_image = await optimize_image_for_processing(input_image)
        
        # Traiter l'image avec Bria.ai
        logger.info("Début du traitement avec Bria.ai")
        output_image = await process_with_bria(input_image, content_moderation)
        
        logger.info(f"Traitement terminé avec succès, mode de l'image résultante: {output_image.mode}")
        
        # Envoyer directement l'image en PNG avec transparence via BytesIO
        logger.info("Préparation de l'image PNG avec transparence pour l'envoi")
        img_io = BytesIO()
        
        # Assurez-vous que l'image est en mode RGBA pour la transparence
        if output_image.mode != 'RGBA':
            logger.info(f"Conversion de l'image du mode {output_image.mode} vers RGBA")
            output_image = await run_in_thread(lambda: output_image.convert('RGBA'))
            
        await run_in_thread(lambda: output_image.save(img_io, format='PNG'))
        img_io.seek(0)
        
        # Afficher les informations sur la taille de l'image
        img_size = img_io.getbuffer().nbytes
        logger.info(f"Taille de l'image à envoyer: {img_size} octets")
        
        # Envoyer l'image avec le bon type MIME
        logger.info("Envoi du fichier PNG au client")
        response = await send_file(
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
            await run_in_thread(lambda: input_image.close())
        if 'output_image' in locals():
            await run_in_thread(lambda: output_image.close())

@app.route('/health', methods=['GET', 'OPTIONS'])
async def health_check():
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
    # En développement, vous pouvez utiliser le serveur intégré de Quart
    # app.run(host='0.0.0.0', port=5000)
    
    # Pour la production avec Hypercorn via python directement
    import hypercorn.asyncio
    hypercorn.asyncio.run(app, {
        'bind': ['0.0.0.0:5000'],
        'workers': 4,
        'worker_class': 'uvloop',
        'keepalive': 65,
    })