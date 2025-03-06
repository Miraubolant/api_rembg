from flask import Flask, request, jsonify, send_file
from rembg import remove
import os
from werkzeug.utils import secure_filename
import uuid
from io import BytesIO
from PIL import Image

app = Flask(__name__)

# Configuration
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'results'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}

# Créer les dossiers s'ils n'existent pas
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/remove-background', methods=['POST'])
def remove_background_api():
    # Vérifier si une image a été envoyée
    if 'image' not in request.files:
        return jsonify({'error': 'Aucune image n\'a été envoyée'}), 400
    
    file = request.files['image']
    print(f"Fichier reçu: {file.filename}")
    
    # Vérifier si le fichier est valide
    if file.filename == '':
        return jsonify({'error': 'Nom de fichier vide'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'error': f'Format de fichier non supporté. Formats acceptés: {", ".join(ALLOWED_EXTENSIONS)}'}), 400
    
    try:
        # Générer un nom de fichier unique
        filename = secure_filename(file.filename)
        unique_filename = f"{uuid.uuid4()}_{filename}"
        input_path = os.path.join(UPLOAD_FOLDER, unique_filename)
        output_path = os.path.join(OUTPUT_FOLDER, unique_filename)
        
        print(f"Sauvegarde de l'image vers: {input_path}")
        # Sauvegarder l'image
        file.save(input_path)
        
        print(f"Vérification que le fichier existe: {os.path.exists(input_path)}")
        
        # Supprimer l'arrière-plan avec rembg
        print("Début du traitement avec rembg")
        input_image = Image.open(input_path)
        print(f"Image ouverte, taille: {input_image.size}, mode: {input_image.mode}")
        output_image = remove(input_image)
        print(f"Traitement terminé, sauvegarde vers: {output_path}")
        output_image.save(output_path)
        
        print(f"Vérification que le résultat existe: {os.path.exists(output_path)}")
        
        # Retourner l'image sans arrière-plan
        return send_file(output_path, mimetype='image/png')
    
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"ERREUR: {str(e)}")
        print(f"DÉTAILS: {error_details}")
        return jsonify({'error': f'Erreur pendant le traitement: {str(e)}', 'details': error_details}), 500
    
    finally:
        # Nettoyer les fichiers temporaires
        if 'input_path' in locals() and os.path.exists(input_path):
            os.remove(input_path)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)