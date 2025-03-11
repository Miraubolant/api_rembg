from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import sys
from werkzeug.utils import secure_filename
import uuid
from io import BytesIO
from PIL import Image
import numpy as np

app = Flask(__name__)
# Activer CORS pour tous les domaines
CORS(app)

# Configuration
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'results'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}

# Modèles disponibles optimisés pour la mode
ALLOWED_MODELS = {
    'standard': {
        'name': 'Standard (u2net)',
        'description': 'Modèle général, bon équilibre entre qualité et vitesse',
        'method': 'rembg',
        'model_param': 'u2net'
    },
    'clothing': {
        'name': 'Vêtements et accessoires',
        'description': 'Optimisé pour les vêtements, accessoires et détails de mode',
        'method': 'rembg',
        'model_param': 'u2net_human_seg'
    },
    'portrait': {
        'name': 'Portraits et mannequins',
        'description': 'Meilleur pour les photos de mannequins et portraits',
        'method': 'mediapipe',
        'model_param': 'selfie'
    },
    'detail': {
        'name': 'Haute précision pour détails',
        'description': 'Pour capturer les détails fins des textiles et accessoires',
        'method': 'deeplab',
        'model_param': 'resnet101'
    },
    'fast': {
        'name': 'Traitement rapide',
        'description': 'Pour traiter rapidement de nombreuses images',
        'method': 'rembg',
        'model_param': 'u2netp'
    }
}

DEFAULT_MODEL = 'clothing'

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

# Fonctions pour différentes méthodes de suppression d'arrière-plan
def process_with_rembg(input_image, model='u2net'):
    """Traitement avec rembg"""
    from rembg import remove, new_session
    
    try:
        # Créer une session rembg avec le modèle spécifié
        session = new_session(model)
        # Supprimer l'arrière-plan
        return remove(input_image, session=session)
    except Exception as e:
        logger.error(f"Erreur lors du traitement avec rembg: {str(e)}")
        raise

def process_with_deeplab(input_image, model='resnet101'):
    """Traitement avec DeepLabV3"""
    try:
        import torch
        import torchvision.transforms as transforms
        from torchvision.models.segmentation import deeplabv3_resnet50, deeplabv3_resnet101

        if model == 'resnet50':
            segmentation_model = deeplabv3_resnet50(pretrained=True)
        elif model == 'resnet101':
            segmentation_model = deeplabv3_resnet101(pretrained=True)
        else:  # mobilenetv3_large
            segmentation_model = torch.hub.load('pytorch/vision', 'deeplabv3_mobilenet_v3_large', pretrained=True)
        
        segmentation_model.eval()
        
        # Prétraitement
        preprocess = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        ])
        
        input_tensor = preprocess(input_image)
        input_batch = input_tensor.unsqueeze(0)
        
        with torch.no_grad():
            output = segmentation_model(input_batch)['out'][0]
            output_predictions = output.argmax(0).byte().cpu().numpy()
        
        # Créer un masque pour les personnes (classe 15 dans COCO)
        mask = np.zeros(output_predictions.shape, dtype=np.uint8)
        mask[output_predictions == 15] = 255
        
        # Appliquer le masque
        input_array = np.array(input_image)
        result_array = np.zeros((input_array.shape[0], input_array.shape[1], 4), dtype=np.uint8)
        result_array[:, :, :3] = input_array[:, :, :3]
        result_array[:, :, 3] = mask
        
        return Image.fromarray(result_array)
    except Exception as e:
        logger.error(f"Erreur lors du traitement avec DeepLabV3: {str(e)}")
        raise

def process_with_mediapipe(input_image, model='selfie'):
    """Traitement avec MediaPipe (spécifique aux personnes)"""
    try:
        import mediapipe as mp
        
        # Convertir l'image PIL en array numpy
        img_np = np.array(input_image)
        
        if model == 'selfie':
            # Utiliser MediaPipe Selfie Segmentation
            mp_selfie_segmentation = mp.solutions.selfie_segmentation
            selfie_segmentation = mp_selfie_segmentation.SelfieSegmentation(model_selection=1)  # 0 pour paysage, 1 pour portrait
            
            # Traiter l'image
            results = selfie_segmentation.process(img_np)
            
            # Obtenir le masque
            mask = results.segmentation_mask
            
            # Convertir le masque en valeurs 0-255
            mask = (mask * 255).astype(np.uint8)
            
        else:  # model == 'general'
            # Utiliser MediaPipe Holistic pour une segmentation plus générale
            mp_holistic = mp.solutions.holistic
            holistic = mp_holistic.Holistic(
                static_image_mode=True,
                model_complexity=2,
                enable_segmentation=True
            )
            
            # Traiter l'image
            results = holistic.process(img_np)
            
            # Obtenir le masque
            mask = results.segmentation_mask
            
            if mask is None:
                # Si aucun masque n'est disponible, retourner une version simple
                logger.warning("Aucun masque disponible de MediaPipe, utilisation d'un masque de secours")
                # Créer un masque de secours (tout est en avant-plan)
                mask = np.ones((img_np.shape[0], img_np.shape[1]), dtype=np.uint8) * 255
            else:
                mask = (mask * 255).astype(np.uint8)
        
        # Créer une image RGBA
        result = np.zeros((img_np.shape[0], img_np.shape[1], 4), dtype=np.uint8)
        result[:, :, 0:3] = img_np[:, :, 0:3]
        result[:, :, 3] = mask
        
        return Image.fromarray(result)
    except Exception as e:
        logger.error(f"Erreur lors du traitement avec MediaPipe: {str(e)}")
        raise

def post_process_fashion(image):
    """Post-traitement optimisé pour la mode"""
    try:
        # Convertir en RGBA si ce n'est pas déjà le cas
        if image.mode != 'RGBA':
            image = image.convert('RGBA')
        
        # Récupérer le canal alpha
        r, g, b, a = image.split()
        
        # Appliquer un léger flou au canal alpha pour adoucir les bords
        from PIL import ImageFilter
        a_smooth = a.filter(ImageFilter.GaussianBlur(radius=0.5))
        
        # Améliorer le contraste du masque alpha
        from PIL import ImageEnhance
        enhancer = ImageEnhance.Contrast(a_smooth)
        a_enhanced = enhancer.enhance(1.2)
        
        # Recombiner les canaux
        result = Image.merge('RGBA', (r, g, b, a_enhanced))
        
        return result
    except Exception as e:
        logger.error(f"Erreur lors du post-traitement: {str(e)}")
        # En cas d'erreur, retourner l'image originale
        return image

@app.route('/remove-background', methods=['POST', 'OPTIONS'])
def remove_background_api():
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /remove-background")
    
    # Récupérer le modèle spécifié dans la requête
    model_key = request.args.get('model') or request.form.get('model') or DEFAULT_MODEL
    post_process = request.args.get('post_process', 'true').lower() in ('true', '1', 't', 'y', 'yes')
    
    # Vérifier si le modèle est valide
    if model_key not in ALLOWED_MODELS:
        logger.warning(f"Modèle non reconnu: {model_key}, utilisation du modèle par défaut: {DEFAULT_MODEL}")
        model_key = DEFAULT_MODEL
    
    # Obtenir les paramètres de méthode et modèle
    method = ALLOWED_MODELS[model_key]['method']
    model_param = ALLOWED_MODELS[model_key]['model_param']
    
    logger.info(f"Utilisation du modèle: {model_key} ({method} avec {model_param})")
    
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
        
        # Traiter l'image selon la méthode associée au modèle choisi
        logger.info(f"Début du traitement avec {method} et paramètre {model_param}")
        
        if method == 'rembg':
            output_image = process_with_rembg(input_image, model_param)
        elif method == 'deeplab':
            output_image = process_with_deeplab(input_image, model_param)
        elif method == 'mediapipe':
            output_image = process_with_mediapipe(input_image, model_param)
        
        logger.info(f"Traitement terminé avec succès, mode de l'image résultante: {output_image.mode}")
        
        # Appliquer un post-traitement optimisé pour la mode si demandé
        if post_process:
            logger.info("Application du post-traitement optimisé pour la mode")
            output_image = post_process_fashion(output_image)
        
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

@app.route('/models', methods=['GET', 'OPTIONS'])
def list_models():
    """Endpoint pour lister tous les modèles disponibles"""
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /models")
    return jsonify({
        'default_model': DEFAULT_MODEL,
        'available_models': ALLOWED_MODELS
    })

@app.route('/health', methods=['GET', 'OPTIONS'])
def health_check():
    # Gérer les requêtes OPTIONS (pre-flight) pour CORS
    if request.method == 'OPTIONS':
        return '', 200
        
    logger.info("Requête reçue sur /health")
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)