import os
import json
import time
import uuid
import re
import random
import string
import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room
import firebase_admin
from firebase_admin import credentials, db
import requests
from tenacity import retry, stop_after_attempt, wait_exponential
from flask_compress import Compress
import psutil

# Initialisation de l'application
app = Flask(__name__)
app.secret_key = os.urandom(24)

# Configuration de Socket.IO - CORRIGÉE
socketio = SocketIO(app, 
                   cors_allowed_origins="*",
                   engineio_logger=False,
                   socketio_logger=False,
                   async_mode='eventlet',  # Cohérent avec eventlet.monkey_patch()
                   ping_interval=25,
                   ping_timeout=60)

# Activation de la compression
Compress(app)

# Cache en mémoire
games_cache = {}
player_score_cache = {}

# Configuration OpenRouter
OPENROUTER_API_KEY = "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"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_TIMEOUT = 15

# Configuration Firebase - CORRIGÉE
def initialize_firebase():
    try:
        if not firebase_admin._apps:  # Vérifier si déjà initialisé
            if os.environ.get('GOOGLE_APPLICATION_CREDENTIALS_JSON'):
                # Production (Render) - utilise variable d'environnement
                creds_info = json.loads(os.environ['GOOGLE_APPLICATION_CREDENTIALS_JSON'])
                cred = credentials.Certificate(creds_info)
            else:
                # Local - utilise fichier
                cred = credentials.Certificate("credentials.json")
            
            firebase_admin.initialize_app(cred, {
                'databaseURL': 'https://quizapp-45497-default-rtdb.europe-west1.firebasedatabase.app/',
                'httpTimeout': 10,
                'maxIdleConnections': 50
            })
            print("Firebase initialisé avec succès")
    except Exception as e:
        print(f"Erreur initialisation Firebase: {e}")
        raise

# Initialiser Firebase avec gestion d'erreur
try:
    initialize_firebase()
    # Références Firebase
    ref_questions = db.reference('questions')
    ref_games = db.reference('games')
except Exception as e:
    print(f"Erreur critique Firebase: {e}")
    # Mode dégradé sans Firebase pour debug
    ref_questions = None
    ref_games = None

# Middleware de performance
@app.before_request
def before_request():
    request.start_time = time.time()

@app.after_request
def after_request(response):
    duration = time.time() - request.start_time
    response.headers['X-Response-Time'] = f"{duration:.2f}s"
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

# Fonction optimisée pour générer des questions
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
def generate_questions(theme, difficulty="moyen", count=5):
    prompt = f"""
    Génère {count} questions de quiz de difficulté {difficulty} sur le thème "{theme}" au format JSON.
    Chaque question doit avoir:
    - text: la question
    - answers: une liste de 4 réponses possibles
    - correct: l'index de la réponse correcte (0-3)
    - difficulty: {difficulty}

    Format de sortie:
    [
        {{
            "text": "Question ici",
            "answers": ["Réponse 1", "Réponse 2", "Réponse 3", "Réponse 4"],
            "correct": 0,
            "difficulty": "{difficulty}"
        }}
    ]
    """

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "X-Request-Timeout": str(OPENROUTER_TIMEOUT)
    }

    payload = {
        "model": "mistralai/mixtral-8x7b-instruct",
        "messages": [
            {"role": "system", "content": "Tu es un expert en création de questions de quiz."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 2000
    }

    try:
        response = requests.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=OPENROUTER_TIMEOUT)
        response.raise_for_status()
        content = response.json()['choices'][0]['message']['content']
        
        json_match = re.search(r'\[.*\]', content, re.DOTALL)
        if json_match:
            questions = json.loads(json_match.group(0))
            for question in questions:
                question['theme'] = theme
            return questions
        return []
    except Exception as e:
        print(f"Erreur OpenRouter: {str(e)}")
        return []  # Retour gracieux au lieu de raise

# Fonctions utilitaires optimisées avec gestion d'erreur
def update_cache(game_id):
    try:
        if ref_games:
            games_cache[game_id] = ref_games.child(game_id).get() or {}
        else:
            games_cache[game_id] = games_cache.get(game_id, {})
    except Exception as e:
        print(f"Erreur update_cache: {e}")

def bulk_game_update(game_id, updates):
    try:
        if ref_games:
            ref_games.child(game_id).update(updates)
        # Mise à jour du cache local dans tous les cas
        if game_id in games_cache:
            games_cache[game_id].update(updates)
        else:
            games_cache[game_id] = updates
        return True
    except Exception as e:
        print(f"Erreur bulk_game_update: {str(e)}")
        # Mise à jour du cache local même en cas d'erreur Firebase
        if game_id in games_cache:
            games_cache[game_id].update(updates)
        else:
            games_cache[game_id] = updates
        return True

def get_game_data(game_id):
    if game_id not in games_cache:
        update_cache(game_id)
    return games_cache.get(game_id, {})

# Routes avec gestion d'erreur améliorée
@app.route('/')
def home():
    try:
        return render_template('home.html')
    except Exception as e:
        return f"Erreur template home.html: {e}", 500

@app.route('/moderator')
def moderator():
    try:
        game_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        initial_data = {
            'active': False,
            'themes': "",
            'difficulty': "moyen",
            'current_question': None,
            'buzzer_enabled': False,
            'buzzer_player': None,
            'countdown': 0,
            'players': {},
            'questions': {},
            'blocked_players': []
        }
        
        if ref_games:
            ref_games.child(game_id).set(initial_data)
        games_cache[game_id] = initial_data
        
        return render_template('moderator.html', game_id=game_id)
    except Exception as e:
        return f"Erreur création jeu: {e}", 500

@app.route('/player')
def player():
    try:
        return render_template('player.html')
    except Exception as e:
        return f"Erreur template player.html: {e}", 500

@app.route('/api/generate_questions', methods=['POST'])
def api_generate_questions():
    try:
        data = request.get_json()
        if not data:
            return jsonify(status="error", message="Données JSON manquantes"), 400
            
        game_id = data.get('game_id')
        themes_str = data.get('themes')
        difficulty = data.get('difficulty', 'moyen')
        count = int(data.get('count', 5))
        
        if not game_id or not themes_str:
            return jsonify(status="error", message="game_id et themes requis"), 400
        
        themes = [t.strip() for t in themes_str.split(',') if t.strip()]
        all_questions = {}
        
        for theme in themes:
            questions = generate_questions(theme, difficulty, count)
            if questions:
                all_questions[theme] = questions
        
        if not all_questions:
            return jsonify(status="error", message="Échec de génération des questions")
        
        updates = {
            'themes': themes_str,
            'difficulty': difficulty,
            'questions': all_questions
        }
        
        if bulk_game_update(game_id, updates):
            return jsonify(status="success", questions=all_questions)
        return jsonify(status="error", message="Échec mise à jour"), 500
    except Exception as e:
        print(f"Erreur api_generate_questions: {e}")
        return jsonify(status="error", message=str(e)), 500

# Autres routes avec gestion d'erreur similaire...
@app.route('/api/reset_buzzer', methods=['POST'])
def api_reset_buzzer():
    try:
        data = request.get_json()
        game_id = data.get('game_id')
        if not game_id:
            return jsonify(status="error", message="game_id requis"), 400
            
        updates = {
            'buzzer_player': None,
            'buzzer_enabled': False,
            'countdown': 0
        }
        if bulk_game_update(game_id, updates):
            return jsonify(status="success")
        return jsonify(status="error"), 500
    except Exception as e:
        return jsonify(status="error", message=str(e)), 500

@app.route('/api/get_game', methods=['GET'])
def get_game():
    try:
        game_id = request.args.get('game_id')
        if not game_id:
            return jsonify(status="error", message="Game ID manquant"), 400
        return jsonify(get_game_data(game_id))
    except Exception as e:
        return jsonify(status="error", message=str(e)), 500

# Route de santé améliorée
@app.route('/health')
def health_check():
    try:
        return jsonify({
            'status': 'healthy',
            'firebase_connected': ref_games is not None,
            'games_in_cache': len(games_cache),
            'memory_usage': psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024,
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# WebSockets avec gestion d'erreur
@socketio.on('connect')
def handle_connect():
    try:
        print('Client connecté:', request.sid)
        emit('connection_success', {'message': 'Connecté avec succès'})
    except Exception as e:
        print(f'Erreur connexion: {e}')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client déconnecté:', request.sid)

# Gestionnaire d'erreur global
@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Route non trouvée'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Erreur interne du serveur'}), 500

@app.errorhandler(Exception)
def handle_exception(e):
    print(f"Erreur non gérée: {e}")
    return jsonify({'error': 'Erreur serveur', 'message': str(e)}), 500

# Préchargement initial du cache au démarrage
try:
    if ref_games:
        all_games = ref_games.get() or {}
        for game_id, game_data in all_games.items():
            games_cache[game_id] = game_data
        print(f"Cache initialisé avec {len(games_cache)} jeux")
except Exception as e:
    print(f"Erreur préchargement cache: {e}")

if __name__ == '__main__':
    # Développement local uniquement
    port = int(os.environ.get('PORT', 5000))
    print(f"Démarrage sur le port {port}")
    socketio.run(app, debug=False, host='0.0.0.0', port=port)