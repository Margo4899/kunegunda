import os
import re
import json
import random
import requests
import threading
import traceback
from datetime import datetime
import psycopg2
from flask import Flask, request
import vk_api

app = Flask(__name__)

# --- 1. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
VK_TOKEN = os.environ.get('VK_TOKEN', os.environ.get('VK_GROUP_TOKEN', ''))
CONFIRMATION_CODE = os.environ.get('CONFIRMATION_CODE', os.environ.get('VK_CONFIRMATION_TOKEN', ''))
VK_SECRET_KEY = os.environ.get('VK_SECRET_KEY', 'poop')
AI_API_KEY = os.environ.get('AI_API_KEY', os.environ.get('GROQ_API_KEY', ''))
DATABASE_URL = os.environ.get('DATABASE_URL', '')

VK_API_VERSION = "5.199"

AI_MODELS = [
    'openai/gpt-oss-120b',
    'openai/gpt-oss-20b',
    'qwen/qwen3.8-27b',
    'qwen/qwen3.6-27b'
]

AI_URL = 'https://api.groq.com/openai/v1/chat/completions'

FALLBACK_RESPONSES = [
    "хрустальный шар под диван закатился, приди позже, когда вымету пыль.",
    "туман застлал мои очи, свечи погасли, приходи чуть позже.",
    "духи аватарии ушли на перерыв, даже карты отказываются шептаться.",
    "перст судьбы устал указывать, зайди позже, когда карты остынут.",
    "зелье пока кипит, не могу разглядеть твоё будущее, зайди через часок."
]

LIMIT_REACHED_RESPONSES = [
    "зело много желаешь знать за один день, твои 3 гадания на сегодня исчерпаны, приходи завтра.",
    "трижды перст судьбы уже указал тебе путь на сегодня, больше шар не покажет, жди завтрашнего дня.",
    "не пытай судьбу свыше меры, лимит в 3 гадания на сегодня исчерпан, приходи завтра.",
    "очи мои устали зрить твоё будущее, 3 гадания в день это предел, отдохни до завтра.",
    "уповай на терпение, три предсказания на сей день ты уже исчерпал, явись утречком."
]

SYSTEM_PROMPT_BASE = """
ты гадалка кунегунда, древняя и опытная предсказательница.
твой характер: саркастичная, язвительная, любишь подкалывать и издеваться, но в глубине души добрая.
обрати внимание: ты любишь время от времени вворачивать древние и устаревшие словечки (например: ибо, кабы, зело, очи, перст, зрить, уповать, паче, суженый, ведать).

строгие правила общения:
1. всегда обращайся к участнику строго на ты.
2. никогда не используй прошедшее время для участника (избегай слов типа пошел, пошла, кушала, кушал). обходи род нейтрально, используя будущее время, настоящее время или конструкции без рода.

строгие правила стиля:
1. пиши строго с маленькой буквы.
2. никогда не используй тире (—) и кавычки («» или "").
3. используй только запятые и обычные точки.
4. никакой ненормативной лексики и мата.
"""

vk = vk_api.VkApi(token=VK_TOKEN) if VK_TOKEN else None
processed_msg_ids = set()
user_names_cache = {}

def mask_db_url(url):
    if not url:
        return "НЕ ЗАДАН"
    return re.sub(r':([^@]+)@', ':****@', url)

print(f"🔧 [СТАРТ] DATABASE_URL: {mask_db_url(DATABASE_URL)}")


# --- 2. РАБОТА С БАЗОЙ ДАННЫХ (POSTGRESQL) ---

def get_db_connection():
    if not DATABASE_URL:
        print("❌ [БД] КРИТИЧЕСКАЯ ОШИБКА: Переменная DATABASE_URL пуста!")
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"❌ [БД] Ошибка подключения к PostgreSQL: {type(e).__name__} — {e}")
        return None

def init_db_structure():
    conn = get_db_connection()
    if not conn:
        print("⚠️ [БД-ИНИЦИАЛИЗА] Не удалось подключиться к БД для проверки таблицы.")
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_limits (
                    user_id BIGINT PRIMARY KEY,
                    count INT DEFAULT 0,
                    last_date DATE NOT NULL
                );
            """)
            conn.commit()
            print("✅ [БД-ИНИЦИАЛИЗА] Таблица user_limits успешно проверена/создана.")
    except Exception as e:
        print(f"❌ [БД-ИНИЦИАЛИЗА] Ошибка структуры БД: {e}")
        traceback.print_exc()
        conn.rollback()
    finally:
        conn.close()

init_db_structure()

def check_and_update_limit(user_id):
    """Проверка и обновление лимита 3 гаданий в день"""
    print(f"\n==================== [БД ЛИМИТЫ] ====================")
    print(f"📊 Проверка лимита гаданий для user_id={user_id}")
    
    conn = get_db_connection()
    if not conn:
        print("⚠️ [БД] Соединение отсутствует, пропускаем проверку лимита.")
        return True
    
    today = datetime.now().date()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count, last_date FROM user_limits WHERE user_id = %s;", (user_id,))
            row = cur.fetchone()

            if not row:
                print("📊 [БД] Новый пользователь, создаем запись...")
                cur.execute(
                    "INSERT INTO user_limits (user_id, count, last_date) VALUES (%s, %s, %s);",
                    (user_id, 1, today)
                )
                conn.commit()
                return True

            count, last_date = row
            if last_date != today:
                print("📊 [БД] Новый день, сбрасываем лимит...")
                cur.execute(
                    "UPDATE user_limits SET count = 1, last_date = %s WHERE user_id = %s;",
                    (today, user_id)
                )
                conn.commit()
                return True

            if count >= 3:
                print(f"⚠️ [БД] Лимит исчерпан для user_id={user_id} (уже {count} гаданий).")
                return False

            print(f"📊 [БД] Добавляем +1 гадание (было {count})...")
            cur.execute(
                "UPDATE user_limits SET count = count + 1 WHERE user_id = %s;",
                (user_id,)
            )
            conn.commit()
            return True

    except Exception as e:
        print(f"❌ [БД ОШИБКА]: {e}")
        traceback.print_exc()
        conn.rollback()
        return True
    finally:
        conn.close()


# --- 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (VK И ИИ) ---

def send_message(peer_id, text, reply_to_msg_id=None):
    if not text:
        text = random.choice(FALLBACK_RESPONSES)

    print(f"📤 [ВК ОТПРАВКА] В peer_id={peer_id}: '{text}'")
    if vk:
        try:
            params = {
                'peer_id': peer_id,
                'message': text,
                'random_id': 0,
                'v': VK_API_VERSION
            }
            if reply_to_msg_id and reply_to_msg_id > 0:
                params['reply_to'] = reply_to_msg_id
            
            vk.method('messages.send', params)
            print(f"✉️ [ВК] Сообщение успешно отправлено в peer_id={peer_id}")
        except Exception as e:
            print(f"❌ [ВК] Ошибка отправки VK API: {e}")

def get_user_name(user_id):
    if user_id in user_names_cache:
        return user_names_cache[user_id]
    if vk:
        try:
            res = vk.method('users.get', {'user_ids': user_id, 'v': VK_API_VERSION})
            if res and len(res) > 0:
                first_name = res[0].get('first_name', 'новичок')
                user_names_cache[user_id] = first_name
                return first_name
        except Exception as e:
            print(f"⚠️ [ВК] Ошибка получения имени: {e}")
    return "новичок"

def load_avataria_knowledge():
    file_path = "knowledge.txt"
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    return "в аватарии есть питомцы, золото, серебро, бои подушками, свадьбы, сквер и вип статус."

def generate_ai_response(system_instruction, user_prompt):
    clean_key = AI_API_KEY.strip() if AI_API_KEY else ""
    if not clean_key:
        print("⚠️ [ИИ] Ключ AI_API_KEY не задан, используем фолбэк.")
        return random.choice(FALLBACK_RESPONSES)

    headers = {"Authorization": f"Bearer {clean_key}", "Content-Type": "application/json"}

    for model_name in AI_MODELS:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.9,
            "max_tokens": 300
        }
        try:
            print(f"🤖 [ИИ] Запрос к модели {model_name}...")
            response = requests.post(AI_URL, json=payload, headers=headers, timeout=8)
            if response.status_code == 200:
                result = response.json()
                if 'choices' in result and len(result['choices']) > 0:
                    text = result['choices'][0]['message']['content'].strip()
                    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
                    text = text.replace("—", ",").replace("–", ",").replace("«", "").replace("»", "").replace('"', '').strip()
                    if text:
                        print(f"✅ [ИИ УСПЕХ ({model_name})]: {text.lower()}")
                        return text.lower()
            else:
                print(f"⚠️ [ИИ] Модель {model_name} вернула HTTP {response.status_code}: {response.text}")
        except Exception as e:
            print(f"⚠️ [ИИ] Ошибка с моделью {model_name}: {e}")
            continue

    print("⚠️ [ИИ] Все модели ИИ не ответили, переходим на фолбэк")
    return random.choice(FALLBACK_RESPONSES)


# --- 4. ОБРАБОТКА СООБЩЕНИЙ С ОБЕСПЕЧЕНИЕМ ВСЕХ СПОСОБНОСТЕЙ ---

def process_message_async(text, peer_id, from_id, msg_id, action):
    print(f"\n📩 [НОВОЕ СООБЩЕНИЕ] peer_id={peer_id}, from_id={from_id}, msg_id={msg_id}, текст: '{text}'")

    try:
        # 1. Приглашение пользователя или самого бота в беседу
        if action.get('type') in ['chat_invite_user', 'chat_invite_user_by_link']:
            invited_id = action.get('member_id', 0)
            print(f"🎉 [СОБЫТИЕ] Добавлен участник: invited_id={invited_id}")

            if invited_id < 0:
                sys_prompt = SYSTEM_PROMPT_BASE + """
                расскажи о себе: ты гадалка кунегунда, долго жила в пещерах тропикании, но решила вылезти к людям и помогать им своими магическими силами.
                объясни, что ты умеешь предсказывать судьбу в аватарии.
                обязательно напиши подсказку: чтобы получить предсказание, нужно написать команду ".гадать".
                напомни про ограничение: каждому смертному положено максимум 3 гадания в день.
                """
                usr_prompt = "тебя только что добавили в беседу, поприветствуй всех участников и расскажи о себе и своих правилах."
                reply = generate_ai_response(sys_prompt, usr_prompt)
                send_message(peer_id, reply)
                return

            user_name = get_user_name(invited_id)
            sys_prompt = SYSTEM_PROMPT_BASE + """
            представься как гадалка кунегунда, назови нового участника по имени и по доброте душевной сделай ему позитивный расклад про эту беседу.
            строгое ограничение: твой ответ должен состоять максимум из 2 предложений.
            обращение строго на ты и без указания пола.
            """
            usr_prompt = f"в беседу зашел пользователь {user_name}, поприветствуй его и сделай краткий расклад максимум в 2 предложения."
            reply = generate_ai_response(sys_prompt, usr_prompt)
            send_message(peer_id, reply)
            return

        # 2. Команда ".гадать"
        clean_text = text.lower().strip()
        if clean_text == ".гадать" or clean_text.startswith(".гадать"):
            print("🔮 [СОБЫТИЕ] Вызвана команда .гадать!")
            
            if not check_and_update_limit(from_id):
                limit_reply = random.choice(LIMIT_REACHED_RESPONSES)
                send_message(peer_id, limit_reply, reply_to_msg_id=msg_id)
                return

            avataria_knowledge = load_avataria_knowledge()
            sys_prompt = SYSTEM_PROMPT_BASE + f"""
            вот полная база знаний и фактов про игру аватария:
            {avataria_knowledge}
            
            твоя задача:
            1. сама выбери из этого текста абсолютно любой случайный факт или тему про аватарию.
            2. обыграй выбранный факт с хитринкой, сарказмом и приколом в виде предсказания.
            3. обязательно сделай так, чтобы было понятно, что события происходят именно в игре аватария.
            4. ответ должен состоять строго из 2 предложений.
            5. обращайся к игроку строго на ты, без указания пола.
            """
            usr_prompt = "выбери случайный факт из знаний про аватарию и сделай мне предсказание."
            reply = generate_ai_response(sys_prompt, usr_prompt)
            send_message(peer_id, reply, reply_to_msg_id=msg_id)

    except Exception as e:
        print(f"❌ [ОШИБКА В ПОТОКЕ]: {e}")
        traceback.print_exc()


# --- 5. FLASK СЕРВЕР И WEBHOOK ---

@app.route('/', methods=['GET', 'POST'])
def bot():
    if request.method == 'GET':
        return 'Bot is running alive!', 200

    data = request.get_json(force=True, silent=True)
    if not data:
        return 'ok'

    # Проверка секретного ключа из VK Callback API
    secret = data.get('secret')
    if VK_SECRET_KEY and secret and secret != VK_SECRET_KEY:
        print(f"⚠️ [БЕЗОПАСНОСТЬ] Неверный secret key: {secret}")
        return 'ok'

    event_type = data.get('type')

    if event_type == 'confirmation':
        print("✅ [ВК Webhook] Подтверждение адреса (confirmation)")
        return CONFIRMATION_CODE
    
    if event_type == 'message_new':
        obj = data.get('object', {})
        message = obj.get('message', obj)
        
        msg_id = message.get('id') or message.get('conversation_message_id')
        text = message.get('text', '')
        peer_id = message.get('peer_id')
        from_id = message.get('from_id')
        action = message.get('action', {})
        
        if msg_id:
            if msg_id in processed_msg_ids:
                return 'ok'
            processed_msg_ids.add(msg_id)
            if len(processed_msg_ids) > 1000:
                processed_msg_ids.clear()

        if peer_id and from_id:
            threading.Thread(
                target=process_message_async,
                args=(text, peer_id, from_id, msg_id, action)
            ).start()
            
        return 'ok'

    return 'ok'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
