from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from rembg import remove, new_session
import os
import sys
from werkzeug.utils import secure_filename
import uuid
from io import BytesIO
from PIL import Image

app = Flask(__name__)
# Activer CORS pour toutes les routes et tous les domaines
CORS(app, resources={r"/*": {"origins": "*"}})

# Configuration
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'results'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
ALLOWED_MODELS = {'u2net', 'u2netp', 'u2net_human_seg', 'silueta', 'isnet-general-use'}
DEFAULT_MODEL = 'u2net'

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

@app.route('/remove-background', methods=['POST', 'OPTIONS'])
def remove_background_api():
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /remove-background")
    
    # Récupérer le modèle spécifié dans la requête (paramètre ou form data)
    model = request.args.get('model') or request.form.get('model') or DEFAULT_MODEL
    
    if model not in ALLOWED_MODELS:
        logger.warning(f"Modèle non reconnu: {model}, utilisation du modèle par défaut: {DEFAULT_MODEL}")
        model = DEFAULT_MODEL
    
    logger.info(f"Utilisation du modèle: {model}")
    
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
        
        # Créer une session rembg avec le modèle spécifié
        logger.info(f"Création d'une session rembg avec le modèle {model}")
        session = new_session(model)
        
        # Supprimer l'arrière-plan avec rembg
        logger.info("Début du traitement avec rembg")
        input_image = Image.open(input_path)
        logger.info(f"Image ouverte, taille: {input_image.size}, mode: {input_image.mode}")
        
        logger.info(f"Application de rembg avec le modèle {model}...")
        output_image = remove(input_image, session=session)
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
        
        # Ajouter des en-têtes pour éviter la mise en cache et permettre CORS
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["Content-Length"] = str(img_size)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        
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

@app.route('/models', methods=['GET', 'OPTIONS'])
def list_models():
    """Endpoint pour lister tous les modèles disponibles"""
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /models")
    response = jsonify({
        'default': DEFAULT_MODEL,
        'available_models': list(ALLOWED_MODELS),
        'descriptions': {
            'u2net': 'Modèle général, bon équilibre entre qualité et vitesse',
            'u2netp': 'Version plus légère et rapide, qualité légèrement inférieure',
            'u2net_human_seg': 'Optimisé pour la segmentation humaine',
            'silueta': 'Spécialisé dans les silhouettes humaines',
            'isnet-general-use': 'Modèle plus récent avec une bonne qualité générale'
        }
    })
    
    # Ajouter des en-têtes CORS
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    
    return response

@app.route('/health', methods=['GET', 'OPTIONS'])
def health_check():
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /health")
    response = jsonify({'status': 'ok'})
    
    # Ajouter des en-têtes CORS
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    
    return response

@app.route('/test-image', methods=['GET', 'OPTIONS'])
def test_image():
    """Endpoint de test qui génère une simple image avec transparence"""
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /test-image")
    try:
        # Créer une image avec transparence (cercle rouge sur fond transparent)
        img = Image.new('RGBA', (200, 200), color=(0, 0, 0, 0))  # Fond transparent
        
        # Importer le module pour dessiner
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        
        # Dessiner un cercle rouge
        draw.ellipse((50, 50, 150, 150), fill=(255, 0, 0, 255))
        
        # Envoyer l'image
        img_io = BytesIO()
        img.save(img_io, 'PNG')
        img_io.seek(0)
        
        img_size = img_io.getbuffer().nbytes
        logger.info(f"Image de test créée avec succès, taille: {img_size} octets")
        
        response = send_file(
            img_io, 
            mimetype='image/png',
            download_name='test_circle.png',
            as_attachment=True
        )
        
        # Ajouter des en-têtes pour éviter la mise en cache et permettre CORS
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["Content-Length"] = str(img_size)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        
        return response
    except Exception as e:
        logger.error(f"Erreur lors de la création de l'image de test: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)