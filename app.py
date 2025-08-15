import os
import json
import time
import uuid
import re
import random
import string
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

# Configuration optimisée de Socket.IO avec async_mode='threading'
socketio = SocketIO(app, 
                   cors_allowed_origins="*",
                   engineio_logger=False,
                   async_mode='threading',  # Solution stable garantie
                   ping_interval=5000,
                   ping_timeout=30000)

# Activation de la compression
Compress(app)

# Cache en mémoire
games_cache = {}
player_score_cache = {}

# Configuration OpenRouter
OPENROUTER_API_KEY = "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"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_TIMEOUT = 15

# Configuration Firebase - CORRIGÉE POUR RENDER
def initialize_firebase():
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

# Initialiser Firebase
initialize_firebase()

# Références Firebase
ref_questions = db.reference('questions')
ref_games = db.reference('games')

# Middleware de performance
@app.before_request
def before_request():
    request.start_time = time.time()

@app.after_request
def after_request(response):
    duration = time.time() - request.start_time
    response.headers['X-Response-Time'] = f"{duration:.2f}s"
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
        }},
        ...
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
        raise

# Fonctions utilitaires optimisées
def update_cache(game_id):
    games_cache[game_id] = ref_games.child(game_id).get() or {}

def bulk_game_update(game_id, updates):
    try:
        ref_games.child(game_id).update(updates)
        update_cache(game_id)
        return True
    except Exception as e:
        print(f"Erreur update: {str(e)}")
        return False

def get_game_data(game_id):
    if game_id not in games_cache:
        update_cache(game_id)
    return games_cache.get(game_id, {})

# Routes
@app.route('/')
def home():
    return render_template('home.html')

@app.route('/moderator')
def moderator():
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
    ref_games.child(game_id).set(initial_data)
    games_cache[game_id] = initial_data
    return render_template('moderator.html', game_id=game_id)

@app.route('/player')
def player():
    return render_template('player.html')

@app.route('/api/generate_questions', methods=['POST'])
def api_generate_questions():
    try:
        game_id = request.json['game_id']
        themes_str = request.json['themes']
        difficulty = request.json.get('difficulty', 'moyen')
        count = int(request.json.get('count', 5))
        
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
        return jsonify(status="error"), 500
    except Exception as e:
        return jsonify(status="error", message=str(e)), 500

@app.route('/api/reset_buzzer', methods=['POST'])
def api_reset_buzzer():
    game_id = request.json['game_id']
    updates = {
        'buzzer_player': None,
        'buzzer_enabled': False,
        'countdown': 0
    }
    if bulk_game_update(game_id, updates):
        return jsonify(status="success")
    return jsonify(status="error"), 500

@app.route('/api/activate_buzzer', methods=['POST'])
def activate_buzzer():
    game_id = request.json['game_id']
    state = request.json['state']
    game_data = get_game_data(game_id)
    
    updates = {
        'buzzer_enabled': state,
        'buzzer_player': None,
        'countdown': 0
    }
    
    if state:
        blocked_players = game_data.get('blocked_players', [])
        updates.update({
            f'players/{pid}/buzzer_active': True
            for pid in game_data.get('players', {})
            if pid not in blocked_players
        })
    
    if bulk_game_update(game_id, updates):
        socketio.emit('game_state', get_game_data(game_id), room=game_id)
        return jsonify(status="success")
    return jsonify(status="error"), 500

@app.route('/api/ask_question', methods=['POST'])
def ask_question():
    try:
        game_id = request.json['game_id']
        theme = request.json['theme']
        question_index = request.json['question_index']
        
        game_data = get_game_data(game_id)
        if not game_data or theme not in game_data.get('questions', {}) or question_index >= len(game_data['questions'][theme]):
            return jsonify(status="error", message="Question invalide")
        
        question = game_data['questions'][theme][question_index]
        updates = {
            'current_question': question,
            'buzzer_enabled': True,
            'buzzer_player': None,
            'countdown': 0,
            'blocked_players': [],
            **{f'players/{pid}/buzzer_active': True for pid in game_data.get('players', {})}
        }
        
        if bulk_game_update(game_id, updates):
            socketio.emit('new_question', room=game_id)
            socketio.emit('game_state', get_game_data(game_id), room=game_id)
            return jsonify(status="success")
        return jsonify(status="error"), 500
    except Exception as e:
        return jsonify(status="error", message=str(e)), 500

@app.route('/api/update_score', methods=['POST'])
def api_update_score():
    try:
        game_id = request.json['game_id']
        player_id = request.json['player_id']
        delta = int(request.json.get('delta', 1))

        # Mise à jour du cache
        cache_key = f"{game_id}_{player_id}"
        player_score_cache[cache_key] = player_score_cache.get(cache_key, 0) + delta

        # Mise à jour asynchrone de Firebase
        def _update_firebase():
            player_ref = db.reference(f'games/{game_id}/players/{player_id}')
            player_ref.transaction(lambda data: {'score': (data or {}).get('score', 0) + delta})

        socketio.start_background_task(_update_firebase)

        return jsonify(status="success", new_score=player_score_cache[cache_key])
    except Exception as e:
        return jsonify(status="error", message=str(e)), 500

@app.route('/api/block_player', methods=['POST'])
def api_block_player():
    game_id = request.json['game_id']
    player_id = request.json['player_id']
    game_data = get_game_data(game_id)
    
    blocked_players = game_data.get('blocked_players', [])
    if player_id not in blocked_players:
        blocked_players.append(player_id)
        updates = {
            'blocked_players': blocked_players,
            f'players/{player_id}/buzzer_active': False
        }
        if bulk_game_update(game_id, updates):
            return jsonify(status="success")
    return jsonify(status="error"), 500

@app.route('/api/unblock_all', methods=['POST'])
def api_unblock_all():
    game_id = request.json['game_id']
    game_data = get_game_data(game_id)
    
    updates = {
        'blocked_players': [],
        'buzzer_enabled': True,
        **{f'players/{pid}/buzzer_active': True for pid in game_data.get('players', {})}
    }
    
    if bulk_game_update(game_id, updates):
        return jsonify(status="success")
    return jsonify(status="error"), 500

@app.route('/api/answer_result', methods=['POST'])
def api_answer_result():
    try:
        game_id = request.json['game_id']
        player_id = request.json['player_id']
        is_correct = request.json['is_correct']

        game_ref = db.reference(f'games/{game_id}')
        updates = {}

        if is_correct:
            player_ref = db.reference(f'games/{game_id}/players/{player_id}')
            current_score = player_ref.get().get('score', 0)
            updates[f'players/{player_id}/score'] = current_score + 1

            updates.update({
                'buzzer_player': None,
                'buzzer_enabled': True,
                'blocked_players': [],
                **{f'players/{pid}/buzzer_active': True for pid in (game_ref.child('players').get() or {})}
            })
        else:
            game_data = game_ref.get() or {}
            blocked_players = game_data.get('blocked_players', [])
            if player_id not in blocked_players:
                blocked_players.append(player_id)
                updates['blocked_players'] = blocked_players
            
            updates.update({
                f'players/{player_id}/buzzer_active': False,
                'buzzer_player': None,
                'buzzer_enabled': True,
                **{f'players/{pid}/buzzer_active': True 
                   for pid in (game_ref.child('players').get() or {})
                   if pid != player_id and pid not in blocked_players}
            })

        game_ref.update(updates)
        update_cache(game_id)
        socketio.emit('game_state', get_game_data(game_id), room=game_id)
        return jsonify(status="success")
    except Exception as e:
        return jsonify(status="error", message=str(e)), 500

@app.route('/api/get_game', methods=['GET'])
def get_game():
    game_id = request.args.get('game_id')
    if not game_id:
        return jsonify(status="error", message="Game ID manquant")
    return jsonify(get_game_data(game_id))

# WebSockets
@socketio.on('connect')
def handle_connect():
    print('Client connecté:', request.sid)

@socketio.on('join_game')
def handle_join_game(data):
    game_id = data['game_id']
    player_id = data['player_id']
    player_name = data.get('name', f"Joueur {player_id[:4]}")

    join_room(game_id)
    game_data = get_game_data(game_id)
    is_blocked = player_id in game_data.get('blocked_players', [])

    updates = {
        f'players/{player_id}/name': player_name,
        f'players/{player_id}/buzzer_active': not is_blocked
    }

    if player_id not in game_data.get('players', {}):
        updates[f'players/{player_id}/score'] = 0

    if bulk_game_update(game_id, updates):
        emit('game_state', get_game_data(game_id), room=game_id)

@socketio.on('buzz')
def handle_buzz(data):
    game_id = data['game_id']
    player_id = data['player_id']
    game_data = get_game_data(game_id)

    if not game_data.get('buzzer_enabled') or game_data.get('buzzer_player'):
        return

    player = game_data.get('players', {}).get(player_id)
    if not (player and player.get('buzzer_active')):
        return

    end_time = time.time() + 15
    updates = {
        'buzzer_player': player_id,
        'buzzer_enabled': False,
        'countdown': end_time,
        **{f'players/{pid}/buzzer_active': False 
           for pid in game_data.get('players', {}) 
           if pid != player_id}
    }

    if bulk_game_update(game_id, updates):
        emit('player_buzzed', {
            'player_id': player_id,
            'player_name': player['name']
        }, room=game_id)
        socketio.start_background_task(countdown_timer, game_id, end_time, player_id)

def countdown_timer(game_id, end_time, player_id):
    while time.time() < end_time:
        game_data = get_game_data(game_id)
        if game_data.get('buzzer_player') != player_id:
            break
            
        remaining = max(0, int(end_time - time.time()))
        socketio.emit('countdown', {'remaining': remaining}, room=game_id)
        
        for _ in range(5):
            if time.time() >= end_time:
                break
            time.sleep(0.2)

    game_data = get_game_data(game_id)
    if game_data.get('buzzer_player') == player_id:
        socketio.emit('timeout', {'player_id': player_id}, room=game_id)
        bulk_game_update(game_id, {
            'buzzer_player': None,
            'countdown': 0
        })

@socketio.on('answer_result')
def handle_answer_result(data):
    game_id = data['game_id']
    player_id = data['player_id']
    is_correct = data['is_correct']
    game_data = get_game_data(game_id)

    updates = {}

    if is_correct:
        player_ref = db.reference(f'games/{game_id}/players/{player_id}')
        current_score = player_ref.get().get('score', 0)
        updates[f'players/{player_id}/score'] = current_score + 1

        updates.update({
            'buzzer_player': None,
            'buzzer_enabled': True,
            'blocked_players': [],
            **{f'players/{pid}/buzzer_active': True for pid in game_data.get('players', {})}
        })
        
        socketio.emit('answer_correct', {
            'player_id': player_id
        }, room=game_id)
    else:
        blocked_players = game_data.get('blocked_players', [])
        if player_id not in blocked_players:
            blocked_players.append(player_id)
            updates['blocked_players'] = blocked_players
        
        updates.update({
            f'players/{player_id}/buzzer_active': False,
            'buzzer_player': None,
            'buzzer_enabled': True,
            **{f'players/{pid}/buzzer_active': True 
               for pid in game_data.get('players', {})
               if pid != player_id and pid not in blocked_players}
        })
        
        socketio.emit('answer_incorrect', {
            'player_id': player_id
        }, room=game_id)

    if bulk_game_update(game_id, updates):
        socketio.emit('game_state', get_game_data(game_id), room=game_id)

# Route de santé
@app.route('/health')
def health_check():
    return jsonify({
        'status': 'healthy',
        'games_in_cache': len(games_cache),
        'memory_usage': psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024,
        'active_connections': len(socketio.server.manager.rooms) if hasattr(socketio.server, 'manager') else 0
    })

# Préchargement initial du cache au démarrage
all_games = ref_games.get() or {}
for game_id, game_data in all_games.items():
    games_cache[game_id] = game_data

if __name__ == '__main__':
    # Développement local uniquement
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, debug=True, host='0.0.0.0', port=port)