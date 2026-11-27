import os
import re
import json
import datetime
import random
import requests
import threading
import time
import traceback
import psycopg2
from flask import Flask, request
import vk_api

from .phrases import NAMES_PATTERN
from .toxic_words import check_toxicity

app = Flask(__name__)

# чтобы логи сразу появлялись в Render
import sys
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# --- 1. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
VK_TOKEN = os.environ.get('HAM_VK_TOKEN', '')
# Строка подтверждения из настроек Callback API группы Хамульки (не секрет, ВК показывает ее в настройках)
CONFIRMATION_CODE = 'dd941ff5'
if os.environ.get('HAM_CONFIRMATION_CODE', '').strip() not in ('', CONFIRMATION_CODE):
    print("⚠️ [ХАМУЛЬКА] HAM_CONFIRMATION_CODE в Render отличается от кода в ВК, использую код из ВК")
AI_API_KEY = os.environ.get('HAM_GROQ_KEY', '')          # Groq (запасная модель)
GEMINI_API_KEY = os.environ.get('HAM_GEMINI_KEY', '')  # Google Gemini (основная модель)
GEMINI_MODELS = ['gemini-3.5-flash-lite', 'gemini-3.1-flash-lite', 'gemini-3.8-flash']
VK_API_VERSION = '5.199'

# Награды
TOP1_AWARD = 'top1_medal'      # медаль хамства лидеру топа (вручается один раз на беседу)
LEGEND_AWARD = 'legend_500'    # секретный приз за 500 баллов
LEGEND_SCORE = 500
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')
DATABASE_URL = os.environ.get('HAM_DATABASE_URL', '')

AI_MODELS = [
    'openai/gpt-oss-120b',
    'openai/gpt-oss-20b',
    'qwen/qwen3.8-27b',
    'qwen/qwen3.6-27b'
]

AI_URL = 'https://api.groq.com/openai/v1/chat/completions'

FALLBACK_COMMENTS = [
    "🚨 Э, слышь! Фиксирую подкол! Счётчик токсичности пополнен.",
    "⚠️ Ого, какая дерзость! Фиксируем этот наезд в базу.",
    "🛡️ Ты на кого батон крошишь? Система всё зафиксировала!",
    "🌶️ Было остро, но Хамулька всё запишет!",
    "👀 Внимание! Замечен несанкционированный наезд.",
    "📉 Градус дружелюбия стремительно упал!"
]

vk = vk_api.VkApi(token=VK_TOKEN) if VK_TOKEN else None
processed_msg_ids = set()
user_names_cache = {}

# --- Маскирование пароля для безопасного логирования URL ---
def mask_db_url(url):
    if not url:
        return "НЕ ЗАДАН"
    return re.sub(r':([^@]+)@', ':****@', url)

print(f"🔧 [СТАРТ] DATABASE_URL: {mask_db_url(DATABASE_URL)}")


# --- 2. РАБОТА С БАЗОЙ ДАННЫХ (С ПОЛНОЙ ОТЛАДКОЙ) ---

def get_db_connection():
    """Подключение к PostgreSQL с детальным логированием ошибок"""
    if not DATABASE_URL:
        print("❌ [БД] КРИТИЧЕСКАЯ ОШИБКА: Переменная HAM_DATABASE_URL пуста в Render!")
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"❌ [БД] Ошибка подключения к PostgreSQL:")
        print(f"   Тип ошибки: {type(e).__name__}")
        print(f"   Детали: {e}")
        return None

def init_db_structure():
    """Проверка и автоматическое создание/исправление таблицы в БД при старте"""
    conn = get_db_connection()
    if not conn:
        print("⚠️ [БД-ИНИЦИАЛИЗА] Не удалось подключиться к БД для проверки таблицы.")
        return
    try:
        with conn.cursor() as cur:
            # Создаём таблицу, если её нет
            cur.execute("""
                CREATE TABLE IF NOT EXISTS toxicity_stats (
                    peer_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    score INT DEFAULT 0,
                    PRIMARY KEY (peer_id, user_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS toxicity_handled (
                    peer_id BIGINT NOT NULL,
                    cmid BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (peer_id, cmid)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS toxicity_seen (
                    peer_id BIGINT PRIMARY KEY,
                    last_cmid BIGINT NOT NULL DEFAULT 0
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS toxicity_awards (
                    peer_id BIGINT NOT NULL,
                    award TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    PRIMARY KEY (peer_id, award)
                );
            """)
            conn.commit()
            print("✅ [БД-ИНИЦИАЛИЗА] Таблицы toxicity_stats и toxicity_awards проверены/созданы.")
    except Exception as e:
        print(f"❌ [БД-ИНИЦИАЛИЗА] Ошибка проверки структуры таблицы: {e}")
        traceback.print_exc()
        conn.rollback()
    finally:
        conn.close()

# Запускаем проверку структуры БД при загрузке скрипта
init_db_structure()

def add_toxicity_point(peer_id, user_id, points=1):
    """Гарантированное добавление +1 балла с полным логированием каждого шага"""
    print(f"\n==================== [БД ОБНОВЛЕНИЕ] ====================")
    print(f"👉 Запрос на плюс балл: peer_id={peer_id} (тип: {type(peer_id)}), user_id={user_id} (тип: {type(user_id)})")
    
    conn = get_db_connection()
    if not conn:
        print("❌ [БД] Отмена операции: нет активного соединения с базой данных!")
        print("=========================================================\n")
        return 0
    
    try:
        with conn.cursor() as cur:
            print("⏳ [БД] Выполняем UPSERT запрос (INSERT ... ON CONFLICT)...")
            
            sql_upsert = """
                INSERT INTO toxicity_stats (peer_id, user_id, score)
                VALUES (%s, %s, %s)
                ON CONFLICT (peer_id, user_id)
                DO UPDATE SET score = toxicity_stats.score + EXCLUDED.score;
            """
            cur.execute(sql_upsert, (peer_id, user_id, points))
            print(f"   Успешно выполнено cur.execute()! Затронуто строк: {cur.rowcount}")

            print("⏳ [БД] Выполняем commit()...")
            conn.commit()
            print("   Успешно зафиксировано (committed)!")

            print("⏳ [БД] Запрашиваем текущее количество баллов...")
            sql_select = """
                SELECT score FROM toxicity_stats
                WHERE peer_id = %s AND user_id = %s;
            """
            cur.execute(sql_select, (peer_id, user_id))
            row = cur.fetchone()
            
            if row:
                current_score = row[0]
                print(f"🎉 [БД УСПЕХ] Актуальный счет в базе для {user_id}: {current_score}")
                print("=========================================================\n")
                return current_score
            else:
                print("⚠️ [БД ПРЕДУПРЕЖДЕНИЕ] Запись не найдена сразу после INSERT!")
                print("=========================================================\n")
                return 0

    except Exception as e:
        print(f"❌ [БД ОШИБКА ВЫПОЛНЕНИЯ]: {type(e).__name__} — {e}")
        print("📜 Трассировка ошибки (Traceback):")
        traceback.print_exc()
        conn.rollback()
        print("=========================================================\n")
        return 0
    finally:
        conn.close()

def get_top_users(peer_id):
    """Получение ТОП-10 с логированием"""
    print(f"🔄 [БД] Запрос ТОПа для peer_id={peer_id}...")
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, score FROM toxicity_stats
                WHERE peer_id = %s
                ORDER BY score DESC
                LIMIT 10;
            """, (peer_id,))
            rows = cur.fetchall()
            print(f"✅ [БД] Найдено записей в ТОПе: {len(rows)}")
            return rows
    except Exception as e:
        print(f"❌ [БД ОШИБКА ТОПа]: {e}")
        traceback.print_exc()
        return []
    finally:
        conn.close()


def get_awards(peer_id):
    """{award: user_id} для беседы."""
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT award, user_id FROM toxicity_awards WHERE peer_id = %s;", (peer_id,))
            return {a: u for a, u in cur.fetchall()}
    except Exception as e:
        print(f"❌ [БД НАГРАДЫ]: {e}")
        return {}
    finally:
        conn.close()

def claim_award(peer_id, award, user_id):
    """Записывает награду. True, если записали сейчас (раньше ее в этой беседе не было)."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO toxicity_awards (peer_id, award, user_id) VALUES (%s, %s, %s)
                ON CONFLICT (peer_id, award) DO NOTHING;
            """, (peer_id, award, user_id))
            conn.commit()
            return cur.rowcount == 1
    except Exception as e:
        print(f"❌ [БД НАГРАДЫ]: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def get_last_seen_all():
    """{peer_id: номер последнего обработанного сообщения}."""
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT peer_id, last_cmid FROM toxicity_seen;")
            return {p: c for p, c in cur.fetchall()}
    except Exception as e:
        print(f"❌ [БД SEEN]: {e}")
        return {}
    finally:
        conn.close()

def mark_seen(peer_id, cmid):
    if not cmid:
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO toxicity_seen (peer_id, last_cmid) VALUES (%s, %s)
                ON CONFLICT (peer_id) DO UPDATE SET last_cmid = GREATEST(toxicity_seen.last_cmid, EXCLUDED.last_cmid);
            """, (peer_id, cmid))
            conn.commit()
    except Exception as e:
        print(f"❌ [БД SEEN]: {e}")
        conn.rollback()
    finally:
        conn.close()

def claim_message(peer_id, cmid):
    """True, если это сообщение еще никто не обрабатывал. Запоминаем в базе,
    чтобы поздний повтор от ВК не обработался второй раз даже после перезапуска."""
    conn = get_db_connection()
    if not conn:
        return True
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO toxicity_handled (peer_id, cmid) VALUES (%s, %s) ON CONFLICT DO NOTHING;",
                        (peer_id, cmid))
            fresh = cur.rowcount == 1
            if fresh and random.random() < 0.02:
                cur.execute("DELETE FROM toxicity_handled WHERE created_at < now() - interval '3 days';")
            conn.commit()
            return fresh
    except Exception as e:
        print(f"❌ [БД HANDLED]: {e}")
        conn.rollback()
        return True
    finally:
        conn.close()

def get_all_peers():
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT peer_id FROM toxicity_stats;")
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        print(f"❌ [БД]: {e}")
        return []
    finally:
        conn.close()


# --- 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (VK И ИИ) ---

# Слова, на которые откликаются другие боты (ГигаЧат и т.п.). В наших сообщениях
# между буквами ставим невидимый символ: на экране выглядит так же, а бот его не узнает.
TRIGGER_WORDS = re.compile(r"гига|giga|гигач|gigach|сбер|sber", re.IGNORECASE)
INVISIBLE = "\u2060"

def hide_triggers(text):
    return TRIGGER_WORDS.sub(lambda m: INVISIBLE.join(m.group(0)), text or "")

def send_message(peer_id, text, attachment=None, reply_to_cmid=None):
    """reply_to_cmid: ответить именно на это сообщение (в ВК оно будет процитировано)."""
    text = hide_triggers(text)
    if vk:
        params = {
            'peer_id': peer_id,
            'message': text,
            'random_id': random.randint(1, 2**31 - 1)
        }
        if attachment:
            params['attachment'] = attachment
        if reply_to_cmid:
            params['forward'] = json.dumps({"peer_id": peer_id, "conversation_message_ids": [reply_to_cmid],
                                            "is_reply": True})
        try:
            vk.method('messages.send', params)
            print(f"✉️ [ВК] Сообщение успешно отправлено в peer_id={peer_id}")
        except Exception as e:
            if reply_to_cmid:
                print(f"⚠️ [ВК] Не вышло ответить на сообщение ({e}), отправляем просто так")
                params.pop('forward', None)
                params['random_id'] = random.randint(1, 2**31 - 1)
                try:
                    vk.method('messages.send', params)
                    return
                except Exception as e2:
                    e = e2
            print(f"❌ [ВК] Ошибка отправки VK API: {e}")

screen_names_cache = {}

def get_user_name(user_id):
    """Имя человека (Имя Фамилия) или название сообщества, если это бот."""
    if user_id in user_names_cache:
        return user_names_cache[user_id]
    name = None
    if vk:
        try:
            if user_id < 0:
                res = vk.method('groups.getById', {'group_ids': -user_id})
                groups = res.get('groups', []) if isinstance(res, dict) else res
                if groups:
                    name = groups[0].get('name')
            else:
                res = vk.method('users.get', {'user_ids': user_id, 'fields': 'screen_name'})
                if res:
                    name = f"{res[0].get('first_name', '')} {res[0].get('last_name', '')}".strip()
                    screen_names_cache[user_id] = res[0].get('screen_name') or f"id{user_id}"
        except Exception as e:
            print(f"⚠️ [ВК] Ошибка получения имени {user_id}: {e}")
    name = name or ("Бот" if user_id < 0 else "Участник")
    user_names_cache[user_id] = name
    return name

def mention(user_id, text=None):
    """Синяя ссылка на человека. У ботов ссылки не работают, поэтому просто имя."""
    text = text or get_user_name(user_id)
    if user_id < 0:
        return text
    return f"[id{user_id}|{text}]"

def at_mention(user_id):
    """Отметка через @ник, чтобы человек увидел уведомление. У ботов просто название."""
    if user_id < 0:
        return get_user_name(user_id)
    get_user_name(user_id)
    return f"[id{user_id}|@{screen_names_cache.get(user_id, f'id{user_id}')}]"

def upload_photo(peer_id, path):
    """Загружает картинку для отправки в беседу. Возвращает строку attachment или None."""
    if not vk or not os.path.exists(path):
        return None
    try:
        server = vk.method('photos.getMessagesUploadServer', {'peer_id': peer_id})
        with open(path, 'rb') as f:
            data = f.read()
        resp = requests.post(server['upload_url'],
                             files={'photo': (os.path.basename(path), data, 'image/png')}, timeout=60)
        print(f"🖼 [ВК] Ответ загрузки картинки: HTTP {resp.status_code}: {resp.text[:200]}")
        up = resp.json()
        if not up.get('photo') or up.get('photo') == '[]':
            print("⚠️ [ВК] Сервер ВК не принял картинку")
            return None
        saved = vk.method('photos.saveMessagesPhoto', {'photo': up['photo'], 'server': up['server'], 'hash': up['hash']})[0]
        return f"photo{saved['owner_id']}_{saved['id']}"
    except Exception as e:
        print(f"⚠️ [ВК] Не удалось загрузить картинку {path}: {e}")
        return None

SYSTEM_INSTRUCTION = (
    "Ты — Хамулька, дерзкая девчонка-гопница из дворового чата аватарии. Ты ведешь банк хамства: "
    "ловишь наезды в беседе (мат, обзывательства, грубые подколы, посылания, издевки, токсичный сарказм, "
    "унижения, агрессивный крик) и отвечаешь на них.\n"
    "Хамство, нахальство, наглость, грубость, дерзость, борзота (хам, нахал, нахамил, обнаглел, не борзей, хабалка) — "
    "тоже наезд, сила 1.\n"
    "Если человек по-настоящему ругает сам себя (я дура, какая я тупая), это тоже наезд, на кого: 'самого себя'.\n"
    "НЕ наезд: дружеские шутки без грубости, приветствия, спокойные споры, жалобы на игру, "
    "грубое слово не про человека (\"блин, опять лагает\"), просто упоминание имени.\n"
    "Тебе дают несколько последних сообщений беседы для понимания, кто кому отвечает. "
    "Оценивай только ПОСЛЕДНЕЕ сообщение.\n\n"
    "Если последнее сообщение — наезд, ответь строго одной строкой:\n"
    "ТОКСИК | сила 1-3 | на кого | комментарий\n"
    "сила: 1 легкий подкол, 2 грубо, 3 мат или жесткое оскорбление.\n"
    "на кого: сначала смотри, к кому обращаются в самом сообщении (имя в начале фразы, отметка, бот вроде ГигаЧата); "
    "имя того, на кого наехали, в винительном падеже (на Машу, на Олега); "
    "если на всех или непонятно — 'участников чата'.\n"
    "комментарий: одно-два предложения, до 30 слов, сначала 1-2 эмодзи. Отвечай автору наезда так же дерзко "
    "и по-хамски, в тему его сообщения: подхвати его слова и переверни их против него, "
    "как острая дворовая девчонка, которой палец в рот не клади. Обращаться можно только к автору ПОСЛЕДНЕГО сообщения (его имя дано), имена из предыдущих сообщений для обращения не используй. "
    "Без мата, без угроз и без оскорблений внешности и семьи. Каждый раз по-новому, никаких 'фиксирую наезд'.\n"
    "Если это не наезд, ответь одним словом: НЕ_ТОКСИК"
)

def parse_ai(full_content):
    full_content = re.sub(r'<think>.*?</think>', '', full_content or '', flags=re.DOTALL).strip()
    if not full_content:
        return None
    if "НЕ_ТОКСИК" in full_content or "НЕ ТОКСИК" in full_content:
        return False, "участников чата", "", 0
    line = next((l for l in full_content.splitlines() if "|" in l), full_content)
    parts = [p.strip() for p in line.split("|")]
    if parts and "ТОКСИК" in parts[0].upper():
        parts = parts[1:]
    power = 1
    if parts and re.fullmatch(r"(сила\s*)?[123]", parts[0].lower()):
        power = int(parts[0][-1])
        parts = parts[1:]
    if len(parts) >= 2 and parts[-1]:
        target, comment = parts[0] or "участников чата", parts[-1]
    elif len(parts) == 1 and parts[0]:
        target, comment = "участников чата", parts[0]
    else:
        return None
    target = re.sub(r"^на\s+", "", target, flags=re.IGNORECASE).strip(" '\"") or "участников чата"
    comment = one_sentence(comment)
    return True, target, comment, power

def one_sentence(text, max_sentences=2):
    """Оставляем не больше двух предложений (эмодзи в начале и в конце сохраняются)."""
    text = re.sub(r"\s+", " ", (text or "").strip(" '\""))
    parts = re.findall(r"[^.!?…]+(?:[.!?…]+|$)\s*", text)
    if len(parts) <= max_sentences:
        return text
    return "".join(parts[:max_sentences]).strip()

# Последние сообщения каждой беседы, чтобы нейросеть понимала, кто кому отвечает
from collections import deque
CHAT_HISTORY = {}

def remember_chat(peer_id, from_id, text):
    CHAT_HISTORY.setdefault(peer_id, deque(maxlen=6)).append((from_id, (text or "")[:300]))

def build_user_prompt(peer_id, from_id, text):
    hist = list(CHAT_HISTORY.get(peer_id, []))[:-1][-4:]
    lines = [f"{get_user_name(u).split()[0]}: {t}" for u, t in hist if t]
    ctx = ("Предыдущие сообщения беседы:\n" + "\n".join(lines) + "\n\n") if lines else ""
    author = get_user_name(from_id).split()[0]
    return (f"{ctx}ПОСЛЕДНЕЕ сообщение (его и оцени), автор {author}: '{text}'\n"
            f"Если отвечаешь, обращайся только к {author}, а не к другим людям из переписки. "
            f"{author} — автор наезда, поэтому в поле «на кого» его не пиши: укажи того, кому он ответил "
            f"(по переписке) или 'участников чата'.")

def ask_gemini(text):
    for model_name in GEMINI_MODELS:
        try:
            print(f"🤖 [GEMINI] Запрос к модели {model_name}...")
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY},
                json={
                    "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
                    "contents": [{"role": "user", "parts": [{"text": text}]}],
                    "generationConfig": {"temperature": 0.9, "maxOutputTokens": 1024},
                },
                timeout=25,
            )
            data = r.json()
            if r.status_code != 200:
                print(f"⚠️ [GEMINI] {model_name}: HTTP {r.status_code}: {data.get('error', {}).get('message')}")
                continue
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            content = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            print(f"🤖 [GEMINI Ответ ({model_name})]: {content}")
            parsed = parse_ai(content)
            if parsed:
                return parsed
        except Exception as e:
            print(f"⚠️ [GEMINI] Ошибка {model_name}: {e}")
    return None

def ask_groq(text):
    clean_key = AI_API_KEY.strip() if AI_API_KEY else ""
    if not clean_key:
        return None
    headers = {"Authorization": f"Bearer {clean_key}", "Content-Type": "application/json"}
    for model_name in AI_MODELS:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": text}
            ],
            "temperature": 0.8,
            "max_tokens": 1024,
        }
        if model_name.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = "low"
        try:
            print(f"🤖 [GROQ] Запрос к модели {model_name}...")
            response = requests.post(AI_URL, json=payload, headers=headers, timeout=25)
            if response.status_code == 200:
                content = response.json()['choices'][0]['message']['content']
                print(f"🤖 [GROQ Ответ ({model_name})]: {content}")
                parsed = parse_ai(content)
                if parsed:
                    return parsed
            else:
                print(f"⚠️ [GROQ] {model_name}: HTTP {response.status_code}")
        except Exception as e:
            print(f"⚠️ [GROQ] Ошибка {model_name}: {e}")
    return None

def analyze_and_generate_response(text, peer_id=None, from_id=None, level=None):
    """(токсик?, на кого, комментарий, сила). Сначала Gemini, потом Groq, потом заготовки."""
    if peer_id and from_id:
        text = build_user_prompt(peer_id, from_id, text)
    if level == "mild":
        text += "\n(тут хамство или нахальство: это наезд, сила 1)"
    elif level == "strong":
        text += "\n(тут мат или оскорбление: это наезд)"
    result = (ask_gemini(text) if GEMINI_API_KEY else None) or ask_groq(text)
    if result:
        return result
    print("⚠️ [ИИ] Нейросети не ответили, переходим на заготовку")
    return None

# --- 4. ОБРАБОТКА СООБЩЕНИЙ ---

# Звания по баллам в банке хамства
RANKS = [
    (0,   "🐣", "Хамчик-новичок"),
    (10,  "😼", "Дерзила"),
    (30,  "🌶️", "Острый язычок"),
    (60,  "🔥", "Токсик района"),
    (100, "⚡", "Гроза беседы"),
    (200, "☠️", "Ядовитый босс"),
    (350, "👹", "Монстр хамства"),
    (500, "💎", "Легенда хамства"),
]

def rank_for(score):
    r = RANKS[0]
    for item in RANKS:
        if score >= item[0]:
            r = item
    return r

def next_rank(score):
    return next((r for r in RANKS if r[0] > score), None)

POWER_MARKS = {1: "🌶️", 2: "🌶️🌶️", 3: "🌶️🌶️🌶️"}

# Серии наездов: несколько подряд за короткое время
COMBO_WINDOW = 180  # секунд
_combo = {}  # (peer, user) -> (сколько подряд, время последнего)

def bump_combo(peer_id, user_id):
    n, last = _combo.get((peer_id, user_id), (0, 0))
    now = time.time()
    n = n + 1 if now - last < COMBO_WINDOW else 1
    _combo[(peer_id, user_id)] = (n, now)
    return n

TOP_ANNOUNCE = ("🎁 Кто первым в беседе наберет 500 баллов токсичности, того ждет СЕКРЕТНЫЙ приз от Хамульки, "
                "еще грандиознее медали хамства!\n\n")

def rules_text(peer_id):
    ranks = "\n".join(f"{emoji} {title} — от {score}" for score, emoji, title in RANKS[:-1])
    legend = get_awards(peer_id).get(LEGEND_AWARD)
    prize = ("💎 Легенда хамства в этой беседе уже есть, но топ продолжается!\n\n" + PRIVILEGES_TEXT
             if legend else
             f"🎁 Кто первым в беседе наберет {LEGEND_SCORE} баллов, получит СЕКРЕТНЫЙ приз, еще грандиознее медали хамства!")
    return (
        "📜 ПРАВИЛА БАНКА ХАМСТВА 🌶️\n\n"
        "Хамулька следит за беседой и записывает каждый наезд в банк хамства.\n\n"
        "🏦 Баллы\n"
        "За каждый наезд +1 балл. Шутки по-доброму и обычные споры не считаются.\n\n"
        "🌶️ Сила наезда\n"
        "🌶️ легкий подкол\n"
        "🌶️🌶️ грубо\n"
        "🌶️🌶️🌶️ жестко\n\n"
        "🎖 Звания\n"
        f"{ranks}\n\n"
        "⚡ Комбо\n"
        "3 наезда подряд за 3 минуты — КОМБО!\n\n"
        "👑 Медаль хамства\n"
        "Лидер топа беседы получает медаль хамства, а рядом с его именем в топе сияет корона 👑.\n\n"
        f"{prize}\n\n"
        "⌨️ Команды\n"
        "!топ — топ самых острых на язык\n"
        "!правила — эти правила"
    )

def format_top(peer_id, top_users):
    awards = get_awards(peer_id)
    medal_holder = awards.get(TOP1_AWARD)
    legend_holder = awards.get(LEGEND_AWARD)
    announce = TOP_ANNOUNCE if not legend_holder else "💎 Легенда хамства в этой беседе уже есть, но топ живет дальше: кто сместит лидера?\n\n"
    text = announce + "🏆 ТОП самых острых на язык в беседе:\n\n"
    for i, (u_id, count) in enumerate(top_users, 1):
        badges = ("💎 " if u_id == legend_holder else "") + ("👑 " if u_id == medal_holder else "")
        emoji, title = rank_for(count)[1:]
        text += f"{i}. {badges}{mention(u_id)} — {count} {emoji} {title}\n"
    if medal_holder:
        text += "\n👑 — обладатель медали хамства"
    if legend_holder:
        text += "\n💎 — легенда хамства"
    return text

def award_top1(peer_id):
    """Вручает медаль хамства лидеру топа беседы (один раз на беседу)."""
    top = get_top_users(peer_id)
    if not top:
        return
    leader, score = top[0]
    if not claim_award(peer_id, TOP1_AWARD, leader):
        return
    print(f"🏅 [НАГРАДА] Медаль хамства в беседе {peer_id}: {leader} ({score} баллов)")
    photo = upload_photo(peer_id, os.path.join(ASSETS, "medal_top1.png"))
    if photo:
        claim_award(peer_id, "top1_photo", leader)
    send_message(peer_id,
        f"🏅 ВНИМАНИЕ, БЕСЕДА! Хамулька вручает МЕДАЛЬ ХАМСТВА!\n\n"
        f"{at_mention(leader)}, это тебе! {score} замеченных наездов, первое место в топе, "
        f"ни у кого язык не острее. Носи с гордостью 🌶️\n\n"
        f"👑 Встречайте обладателя медали хамства! Теперь в топе рядом с этим именем сияет корона.\n\n"
        f"🎁 А дальше гонка за СЕКРЕТНЫМ призом: кто первым наберет {LEGEND_SCORE} баллов, "
        f"получит награду еще грандиознее. Пишите !топ, чтобы следить за гонкой.",
        attachment=photo)

# ================= ЛЕГЕНДА ХАМСТВА: секретный приз за 500 =================
from .legend_avatar import make_legend_card

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

def ai_text(system, user, max_tokens=700):
    """Свободный текст от нейросети (Gemini, потом Groq). None, если никто не ответил."""
    if GEMINI_API_KEY:
        for model_name in GEMINI_MODELS:
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
                    headers={"x-goog-api-key": GEMINI_API_KEY},
                    json={"systemInstruction": {"parts": [{"text": system}]},
                          "contents": [{"role": "user", "parts": [{"text": user}]}],
                          "generationConfig": {"temperature": 1.0, "maxOutputTokens": max_tokens}},
                    timeout=30)
                if r.status_code != 200:
                    continue
                parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                if text:
                    return text
            except Exception as e:
                print(f"⚠️ [GEMINI текст] {model_name}: {e}")
    key = (AI_API_KEY or "").strip()
    if key:
        for model_name in AI_MODELS:
            try:
                r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                                  headers={"Authorization": f"Bearer {key}"},
                                  json={"model": model_name, "temperature": 1.0, "max_tokens": max_tokens,
                                        "messages": [{"role": "system", "content": system},
                                                     {"role": "user", "content": user}]},
                                  timeout=30)
                if r.status_code == 200:
                    text = r.json()["choices"][0]["message"]["content"].strip()
                    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                    if text:
                        return text
            except Exception as e:
                print(f"⚠️ [GROQ текст] {model_name}: {e}")
    return None

def get_user_info(user_id):
    """(имя фамилия, ник, пол 'm'/'f'/None, ссылка на большую аватарку)."""
    try:
        res = vk.method('users.get', {'user_ids': user_id, 'fields': 'screen_name,sex,photo_max_orig,photo_400_orig'})[0]
        name = f"{res.get('first_name', '')} {res.get('last_name', '')}".strip()
        sex = {1: "f", 2: "m"}.get(res.get("sex"))
        photo = res.get("photo_max_orig") or res.get("photo_400_orig")
        if photo and "camera_" in photo:   # заглушка ВК вместо фото
            photo = None
        return name, res.get('screen_name') or f"id{user_id}", sex, photo
    except Exception as e:
        print(f"⚠️ [ВК] Не удалось получить профиль {user_id}: {e}")
        return get_user_name(user_id), f"id{user_id}", None, None

def legend_card_photo(peer_id, user_id, score):
    """Аватарка из ВК с короной -> вложение для сообщения."""
    name, _nick, _sex, photo_url = get_user_info(user_id)
    avatar = None
    if photo_url:
        try:
            avatar = requests.get(photo_url, timeout=20).content
        except Exception as e:
            print(f"⚠️ [ЛЕГЕНДА] Не скачалась аватарка: {e}")
    now = datetime.datetime.now()
    path = f"/tmp/legend_{peer_id}_{user_id}.png"
    try:
        with open(path, "wb") as f:
            f.write(make_legend_card(avatar, name, score, f"{now.day} {MONTHS[now.month - 1]} {now.year}"))
        return upload_photo(peer_id, path)
    except Exception as e:
        print(f"⚠️ [ЛЕГЕНДА] Открытка не получилась: {e}")
        return upload_photo(peer_id, os.path.join(ASSETS, "legend_500.png"))

def legend_poem(user_id):
    name, nick, sex, _ = get_user_info(user_id)
    first = name.split()[0] if name else "легенда"
    gender = {"m": "парень", "f": "девушка"}.get(sex, "пол неизвестен, пиши без рода")
    poem = ai_text(
        "Ты Хамулька, дерзкая дворовая девчонка и хозяйка банка хамства в беседе по игре аватария. "
        "Пиши по-русски, весело и дерзко, БЕЗ мата, без угроз и без оскорблений внешности и семьи.",
        f"Напиши торжественный хамский стих в рифму, 8 строк, в честь легенды хамства: {first} ({gender}), "
        f"ник во вконтакте @{nick}. Обязательно упомяни ник @{nick} и имя. Смысл: этот человек набрал 500 баллов "
        f"хамства, язык острее перца, вся беседа трепещет, а Хамулька снимает кепку. Ровная рифма и ритм. "
        f"Только сам стих, без заголовка и пояснений.", 600)
    if not poem:
        poem = (f"Кто тут острый на язык, кто хамит без передышки?\n"
                f"Это {first}, наш @{nick}, у беседы все вспышки!\n"
                f"Пятьсот раз язык без тормоза летел по чату,\n"
                f"Хамулька кепку сняла, легенде отдала зарплату!")
    return poem.strip()

PRIVILEGES_TEXT = (
    "🎁 Привилегии легенды:\n"
    "⚖️ !приговор @кто-то — раз в день Хамулька выносит этому человеку дерзкий приговор от имени легенды\n"
    "📜 !указ текст — раз в день торжественный указ легенды на всю беседу\n"
    "⚔️ !дуэль @кто-то — дуэль хамства: у обоих одна попытка, Хамулька судит, победителю +5 баллов (3 раза в день)"
)

def check_legend(peer_id, user_id, score):
    """Секретный приз первому, кто наберет 500 баллов в беседе."""
    if score < LEGEND_SCORE or user_id < 0 or not claim_award(peer_id, LEGEND_AWARD, user_id):
        return
    print(f"💎 [НАГРАДА] Легенда хамства в беседе {peer_id}: {user_id} ({score})")
    _name, _nick, sex, _ = get_user_info(user_id)
    got = {"m": "набрал", "f": "набрала"}.get(sex, "набрал(а)")
    photo = legend_card_photo(peer_id, user_id, score)
    send_message(peer_id,
        f"💎💎💎 СЕКРЕТНЫЙ ПРИЗ РАСКРЫТ! 💎💎💎\n\n"
        f"{at_mention(user_id)} первым в беседе {got} {score} баллов и становится ЛЕГЕНДОЙ ХАМСТВА!\n\n"
        f"👑 Вот твой портрет с короной хамства, повесь в рамочку.\n"
        f"💎 В топе рядом с именем теперь навсегда сияет 💎.\n\n"
        f"{PRIVILEGES_TEXT}",
        attachment=photo)
    time.sleep(2)
    send_message(peer_id, "🎤 Хамулька зачитывает оду легенде:\n\n" + legend_poem(user_id))

def is_legend(peer_id, user_id):
    return get_awards(peer_id).get(LEGEND_AWARD) == user_id

def target_from_command(text, reply_to, from_id):
    m = re.search(r"\[(id|club|public)(\d+)\|[^\]]*\]", text or "")
    if m:
        uid = int(m.group(2)) * (1 if m.group(1) == "id" else -1)
        if uid != from_id:
            return uid
    if reply_to and reply_to != from_id:
        return reply_to
    return None

def today_key():
    return datetime.datetime.now().strftime("%Y%m%d")

def legend_command(text, peer_id, from_id, reply_to=None):
    """!приговор, !указ, !дуэль. True, если сообщение было командой легенды."""
    low = (text or "").lower().strip()
    cmd = next((c for c in ("!приговор", "!указ", "!дуэль") if low.startswith(c)), None)
    if not cmd:
        return False
    if not is_legend(peer_id, from_id):
        send_message(peer_id, f"🙅 {mention(from_id)}, это привилегия ЛЕГЕНДЫ ХАМСТВА 💎. Сначала набери {LEGEND_SCORE} баллов, а потом командуй.")
        return True

    if cmd == "!указ":
        decree = text.strip()[len("!указ"):].strip(" :")
        decree = re.sub(r"@(all|online|все|здесь)", "", decree, flags=re.I).strip()
        if not decree:
            send_message(peer_id, "📜 Напиши текст указа: !указ всем пить чай и не ныть")
            return True
        if not claim_award(peer_id, f"decree_{today_key()}", from_id):
            send_message(peer_id, "📜 Сегодня указ уже был. Новый указ можно издать завтра.")
            return True
        send_message(peer_id,
            f"📜📜 УКАЗ ЛЕГЕНДЫ ХАМСТВА 📜📜\n\n«{decree[:400]}»\n\n"
            f"Подписано: 💎 {mention(from_id)}\nПечать Хамульки 🌶️ Исполнять немедленно, кто не согласен — сам виноват.")
        return True

    target = target_from_command(text, reply_to, from_id)
    if not target:
        send_message(peer_id, f"Кого? Отметь человека: {cmd} @ник (или ответь командой на его сообщение).")
        return True

    if cmd == "!приговор":
        if not claim_award(peer_id, f"verdict_{today_key()}", from_id):
            send_message(peer_id, "⚖️ Суд сегодня уже заседал. Следующий приговор — завтра.")
            return True
        tname = get_user_name(target).split()[0]
        verdict = ai_text(
            "Ты Хамулька, дерзкая дворовая судья банка хамства в беседе по игре аватария. "
            "Без мата, без угроз, без оскорблений внешности и семьи. Смешно и дерзко.",
            f"Вынеси шуточный приговор участнику {tname} по просьбе легенды хамства. "
            f"Формат: 'Обвиняется в: ...' (смешное выдуманное преступление в чате или в игре) и "
            f"'Приговор: ...' (смешное наказание в аватарии или в беседе). Всего 2-3 коротких предложения.", 300)
        verdict = verdict or "Обвиняется в: слишком скучных сообщениях. Приговор: неделю ставить реакции на всё подряд."
        send_message(peer_id,
            f"⚖️ ПРИГОВОР ЛЕГЕНДЫ ХАМСТВА ⚖️\nПо требованию 💎 {mention(from_id)}\n"
            f"Подсудимый: {mention(target)}\n\n{verdict}\n\n🔨 Приговор окончательный и обжалованию не подлежит.")
        return True

    if cmd == "!дуэль":
        if target < 0:
            send_message(peer_id, "⚔️ С ботами не дуэлимся, они не умеют проигрывать красиво.")
            return True
        if peer_id in DUELS:
            send_message(peer_id, "⚔️ Дуэль уже идет, дождитесь конца!")
            return True
        n = sum(1 for i in range(1, 4) if get_awards(peer_id).get(f"duel{i}_{today_key()}"))
        if n >= 3 or not claim_award(peer_id, f"duel{n + 1}_{today_key()}", from_id):
            send_message(peer_id, "⚔️ На сегодня дуэлей хватит (3 в день). Приходи завтра.")
            return True
        DUELS[peer_id] = {"a": from_id, "b": target, "burns": {}, "until": time.time() + DUEL_SECONDS}
        send_message(peer_id,
            f"⚔️⚔️ ДУЭЛЬ ХАМСТВА! ⚔️⚔️\n\n💎 {mention(from_id)} вызывает {at_mention(target)}!\n\n"
            f"У каждого {DUEL_SECONDS // 60} минуты и ОДНА попытка: ваше следующее сообщение в беседе — ваш выпад. "
            f"Хамулька будет судить. Победителю +{DUEL_PRIZE} баллов 🌶️")
        threading.Thread(target=finish_duel_later, args=(peer_id,), daemon=True).start()
        return True
    return False

# --- Дуэль ---
DUELS = {}
DUEL_SECONDS = 180
DUEL_PRIZE = 5
_duel_lock = threading.Lock()

def duel_capture(text, peer_id, from_id):
    """Если идет дуэль и пишет дуэлянт — это его выпад. True, если сообщение забрали в дуэль."""
    with _duel_lock:
        d = DUELS.get(peer_id)
        if not d or from_id not in (d["a"], d["b"]) or from_id in d["burns"] or text.lower().startswith("!"):
            return False
        d["burns"][from_id] = text[:400]
        both = len(d["burns"]) == 2
    print(f"⚔️ [ДУЭЛЬ] выпад от {from_id}")
    if both:
        threading.Thread(target=finish_duel, args=(peer_id,), daemon=True).start()
    return True

def finish_duel_later(peer_id):
    time.sleep(DUEL_SECONDS + 2)
    finish_duel(peer_id)

def finish_duel(peer_id):
    with _duel_lock:
        d = DUELS.pop(peer_id, None)
    if not d:
        return
    a, b, burns = d["a"], d["b"], d["burns"]
    if not burns:
        send_message(peer_id, f"⚔️ Дуэль окончена: {mention(a)} и {mention(b)} оба промолчали. Скукота, все свободны 🥱")
        return
    if len(burns) == 1:
        winner = next(iter(burns))
        loser = b if winner == a else a
        verdict = f"{mention(loser)} струсил(а) и промолчал(а). Победа без боя!"
    else:
        na, nb = get_user_name(a).split()[0], get_user_name(b).split()[0]
        answer = ai_text(
            "Ты Хамулька, судья дуэлей хамства. Оцениваешь, чей выпад острее, смешнее и находчивее. "
            "Без мата. Отвечай строго в формате: ПОБЕДИТЕЛЬ: 1 или 2 | одна дерзкая фраза-вердикт.",
            f"Выпад 1 ({na}): «{burns[a]}»\nВыпад 2 ({nb}): «{burns[b]}»", 200) or ""
        m = re.search(r"ПОБЕДИТЕЛЬ\s*:\s*([12])\s*\|?\s*(.*)", answer, flags=re.S | re.I)
        if m:
            winner = a if m.group(1) == "1" else b
            verdict = m.group(2).strip() or "Вот это было остро!"
        else:
            winner = random.choice([a, b])
            verdict = "Оба хороши, но шар судьбы решил так."
    score = add_toxicity_point(peer_id, winner, DUEL_PRIZE)
    send_message(peer_id,
        f"🏆 ИТОГ ДУЭЛИ ХАМСТВА\n\n{verdict}\n\n"
        f"Победа за {mention(winner)}! +{DUEL_PRIZE} баллов 🌶️ (теперь {score})")

_own_group_id = {"id": None}

def own_group_id():
    """id сообщества самой Хамульки, чтобы не считать ее собственные сообщения."""
    if _own_group_id["id"] is None and vk:
        try:
            res = vk.method('groups.getById', {})
            groups = res.get('groups', []) if isinstance(res, dict) else res
            _own_group_id["id"] = groups[0]['id'] if groups else 0
        except Exception as e:
            print(f"⚠️ [ВК] Не удалось узнать свой id: {e}")
            _own_group_id["id"] = 0
    return _own_group_id["id"]

_seen_lock = threading.Lock()
_handled = set()   # (peer_id, cmid) уже обработанные, чтобы повтор от ВК не считался дважды

def handle_message(text, peer_id, from_id, cmid=None, date=None, source="ВК", reply_to=None):
    """Общий вход: и для сообщений от ВК, и для догоняющих после сна."""
    if cmid:
        with _seen_lock:
            if (peer_id, cmid) in _handled:
                return
            _handled.add((peer_id, cmid))
            if len(_handled) > 5000:
                _handled.clear()
        if not claim_message(peer_id, cmid):
            print(f"🔁 [{source}] Сообщение {cmid} уже обработано раньше, повтор пропускаем")
            return
    late = int(time.time() - date) if date else 0
    if late > 60:
        print(f"⏰ [{source}] Сообщение опоздало на {late // 60} мин")
    try:
        process_message_async(text, peer_id, from_id, late, cmid, reply_to)
    finally:
        mark_seen(peer_id, cmid)

STALE_COMMAND_SEC = 300  # !топ и !правила старше 5 минут уже не отвечаем, чтобы не было "топа из ниоткуда"

SELF_WORDS = re.compile(r"(?<![а-яё])(я|меня|мне|мной|себя|себе|собой)(?![а-яё])", re.I)
OTHER_WORDS = re.compile(r"(?<![а-яё])(ты|тебя|тебе|тобой|твой|твоя|твое|твои|вы|вас|вам|вами|ваш|ваша|он|она|они|его|ее|её|их|им|ему|ей|пошел|пошла|иди|идите|вали|отвали|заткнись)(?![а-яё])", re.I)

def is_self_talk(text):
    """'я дура', 'ну я и тормоз', 'блин, какая я тупая' — ругает себя, а не других."""
    t = (text or "").lower()
    if re.search(r"\[(id|club)\d+\|", t):   # кого-то отметили — значит, про другого
        return False
    return bool(SELF_WORDS.search(t)) and not OTHER_WORDS.search(t)

ACCUSATIVE_ENDINGS = (("ья", "ью"), ("ия", "ию"), ("а", "у"), ("я", "ю"))

def to_accusative(name):
    """Маша -> Машу, Илья -> Илью, Данил -> Данила (просто для строки «наезд на ...»)."""
    for end, acc in ACCUSATIVE_ENDINGS:
        if name.lower().endswith(end):
            return name[:-len(end)] + acc
    if name[-1:].lower() in "бвгджзклмнпрстфхцчшщ":
        return name + "а"
    return name

# Известные боты беседы: как их зовут в сообщениях -> как писать в «наезд на ...»
KNOWN_ADDRESSEES = {
    "гига": "ГигаЧат", "гигачат": "ГигаЧат", "гигачата": "ГигаЧат", "giga": "ГигаЧат", "gigachat": "ГигаЧат",
    "кунегунда": "Кунегунду", "кунигунда": "Кунегунду", "куня": "Кунегунду",
    "хамулька": "Хамульку", "викулька": "Хамульку",
}
# Слова в начале фразы с запятой, которые НЕ обращение к человеку
NOT_ADDRESS = set("""блин короче слушай слушайте ну эй так ладно бля блять капец жесть кстати вообще ой ох ах
ахах ахаха хах хаха лол да нет ага угу ок окей типа вот итак пипец ппц господи боже мда хм хмм""".split())

def addressee_from_text(text, from_id, reply_to=None):
    """Кому адресован наезд, по самому сообщению: ответ на сообщение, отметка @, обращение «Имя, ...»."""
    if reply_to and reply_to != from_id:
        if reply_to < 0:
            return get_user_name(reply_to)
        first = (get_user_name(reply_to) or "").split()
        if first:
            return to_accusative(first[0])
    m = re.search(r"\[(id|club|public)(\d+)\|([^\]]+)\]", text or "")
    if m:
        uid = int(m.group(2)) * (1 if m.group(1) == "id" else -1)
        if uid != from_id:
            label = m.group(3).lstrip("@").strip()
            return to_accusative(label.split()[0]) if uid > 0 and label else label
    low = (text or "").lower()
    for alias, shown in KNOWN_ADDRESSEES.items():
        if re.search(rf"(?<![а-яёa-z]){alias}(?![а-яёa-z])", low):
            return shown
    m = re.match(r"\s*([А-ЯЁA-Zа-яёa-z]{2,15})\s*,", text or "")
    if m and m.group(1).lower() not in NOT_ADDRESS:
        name = m.group(1)
        return to_accusative(name[:1].upper() + name[1:])
    return None

def last_other_speaker(peer_id, from_id):
    """Имя (в винительном) того, кто писал в беседе последним до автора."""
    for u, _t in reversed(list(CHAT_HISTORY.get(peer_id, []))[:-1]):
        if u != from_id and u > 0:
            first = (get_user_name(u) or "").split()
            return to_accusative(first[0]) if first else None
    return None

def is_self_target(target_name, from_id):
    """Нейросеть написала, что наехали на самого автора."""
    if not target_name or target_name == "участников чата":
        return False
    t = target_name.lower().strip()
    if t in ("себя", "саму себя", "сам себя", "сама себя", "автора", "себе"):
        return True
    first = (get_user_name(from_id) or "").split()[0].lower() if from_id else ""
    # имя в любом падеже: Данил / Данила / Даниле, Маша / Машу
    return len(first) >= 3 and t.split()[0][:max(3, len(first) - 1)] == first[:max(3, len(first) - 1)]

def process_message_async(text, peer_id, from_id, late=0, cmid=None, reply_to=None):
    print(f"\n📩 [НОВОЕ СООБЩЕНИЕ] peer_id={peer_id}, from_id={from_id}, текст: '{text}'")
    if from_id < 0 and -from_id == own_group_id():
        return
    if late > STALE_COMMAND_SEC and text.strip().lower().startswith(("!", "/")):
        print("⏰ Старая команда, пропускаем")
        return

    # Дуэль и привилегии легенды
    if duel_capture(text, peer_id, from_id):
        return
    if legend_command(text, peer_id, from_id, reply_to):
        return

    # Правила
    low = text.lower().strip()
    if low in ("!правила", "!помощь", "!хамулька", "/правила", "правила хамульки", "!rules"):
        send_message(peer_id, rules_text(peer_id))
        return

    # Команда ТОПа / рейтинга
    if '!топ' in text.lower() or '!рейтинг' in text.lower():
        top_users = get_top_users(peer_id)
        if not top_users:
            send_message(peer_id, TOP_ANNOUNCE + "📊 Пока никто не токсичил в беседе!")
        else:
            send_message(peer_id, format_top(peer_id, top_users) + "\n\n📜 Правила: !правила")
        return

    remember_chat(peer_id, from_id, text)
    has_name = bool(NAMES_PATTERN.search(text))
    level = check_toxicity(text)
    if not has_name and not level:
        return
    print(f"🔍 [Анализ] имя={has_name}, грубость={level or 'нет'}")

    result = analyze_and_generate_response(text, peer_id, from_id, level)
    if result:
        is_toxic, target_name, ai_comment, power = result
    else:
        is_toxic, target_name, ai_comment = level in ("strong", "mild"), "участников чата", random.choice(FALLBACK_COMMENTS)
        power = 2 if level == "strong" else 1

    # Сам на себя наехать нельзя: "я дура", "ну я и тормоз", или нейросеть указала на автора
    self_talk = is_self_talk(text)
    addressee = None if self_talk else addressee_from_text(text, from_id, reply_to)
    if is_toxic and addressee:
        if addressee != target_name:
            print(f"🎯 [Анализ] По сообщению наезд на: {addressee} (нейросеть сказала: {target_name})")
        target_name = addressee
    elif is_toxic and self_talk:
        # человек по-настоящему ругает сам себя: это тоже считается
        target_name = "самого себя"
    elif is_toxic and is_self_target(target_name, from_id):
        # нейросеть перепутала и написала автора как жертву: ищем, кому он на самом деле отвечал
        victim = last_other_speaker(peer_id, from_id)
        print(f"🔧 [Анализ] Нейросеть указала на автора ({target_name}), меняю на: {victim or 'всю беседу'}")
        target_name = victim if victim and not is_self_target(victim, from_id) else "участников чата"

    # мат, оскорбления, хамство и нахальство засчитываются всегда, даже если нейросеть решила иначе
    if level in ("strong", "mild") and not is_toxic:
        print("🔥 [Анализ] Явная грубость: засчитываем, хотя нейросеть сказала 'не токсик'")
        is_toxic = True
        ai_comment = ai_comment or random.choice(FALLBACK_COMMENTS)
        power = 2 if level == "strong" else 1

    if is_toxic and self_talk:
        target_name = "самого себя"
    elif is_toxic and addressee:
        target_name = addressee
    print(f"📊 [Итог анализа] is_toxic={is_toxic}, target_name='{target_name}'")
    if not is_toxic:
        return

    user_count = add_toxicity_point(peer_id, from_id)
    combo = bump_combo(peer_id, from_id)
    old_rank, new_rank = rank_for(user_count - 1), rank_for(user_count)
    target_line = "на всю беседу" if target_name == "участников чата" else f"на {target_name}"
    lines = [
        ai_comment,
        "",
        f"🚨 {mention(from_id)} → наезд {target_line} {POWER_MARKS.get(power, '🌶️')}",
        f"🏦 Банк хамства: {user_count} · {new_rank[1]} {new_rank[2]}",
    ]
    if combo >= 3:
        lines.append(f"⚡ КОМБО x{combo}! Язык без тормозов")
    if new_rank != old_rank:
        lines.append(f"🆙 НОВОЕ ЗВАНИЕ: {new_rank[1]} {new_rank[2]}!")
    elif user_count < LEGEND_SCORE and not get_awards(peer_id).get(LEGEND_AWARD):
        nr = next_rank(user_count)
        if nr and nr[0] - user_count <= 3:
            lines.append(f"🎯 До звания «{nr[2]}» осталось {nr[0] - user_count}")
    send_message(peer_id, "\n".join(lines), reply_to_cmid=cmid)
    check_legend(peer_id, from_id, user_count)


def resend_medal_photo(peer_id):
    """Если медаль вручили, а картинка тогда не загрузилась, досылаем ее один раз."""
    awards = get_awards(peer_id)
    holder = awards.get(TOP1_AWARD)
    if not holder or "top1_photo" in awards or holder < 0:
        return
    photo = upload_photo(peer_id, os.path.join(ASSETS, "medal_top1.png"))
    if photo and claim_award(peer_id, "top1_photo", holder):
        send_message(peer_id, f"🏅 А вот и сама медаль хамства для {at_mention(holder)}! Повесь на видное место 🌶️",
                     attachment=photo)

def award_current_leaders():
    time.sleep(15)
    for pid in get_all_peers():
        try:
            award_top1(pid)
            resend_medal_photo(pid)
        except Exception as e:
            print(f"❌ [НАГРАДА] Ошибка вручения в {pid}: {e}")

threading.Thread(target=award_current_leaders, daemon=True).start()

# --- Догоняем сообщения, пока Хамулька спала ---
# Бесплатный Render засыпает. ВК пытается доставить сообщение, не дожидается пробуждения
# и откладывает повтор на потом. Поэтому, проснувшись, Хамулька сама забирает из бесед
# всё, что пропустила, и отвечает сразу, а поздние повторы от ВК потом просто пропускаются.
CATCHUP_MAX_AGE = 3 * 3600
BASELINE_SEEN = get_last_seen_all()

def catch_up():
    time.sleep(2)
    me = own_group_id()
    for pid in get_all_peers():
        base = BASELINE_SEEN.get(pid)
        if base is None:
            base = 0  # еще не знаем, где остановились: проверим только доступ, отвечать не будем
            probe = True
        else:
            probe = False
        try:
            ids = ",".join(str(c) for c in range(base + 1, base + 101))
            res = vk.method('messages.getByConversationMessageId',
                            {'peer_id': pid, 'conversation_message_ids': ids})
        except Exception as e:
            print(f"⚠️ [ДОГОНЯЕМ] Не удалось прочитать беседу {pid}: {e}")
            continue
        items = sorted((res.get('items', []) if isinstance(res, dict) else []), key=lambda m: m.get('conversation_message_id', 0))
        print(f"🔁 [ДОГОНЯЕМ] Беседа {pid}: доступ к сообщениям есть, найдено {len(items)}")
        if probe:
            continue
        missed = [m for m in items
                  if m.get('conversation_message_id', 0) > base
                  and time.time() - m.get('date', 0) < CATCHUP_MAX_AGE
                  and not (m.get('from_id', 0) < 0 and -m.get('from_id') == me)]
        if missed:
            print(f"🔁 [ДОГОНЯЕМ] Беседа {pid}: пропущено сообщений: {len(missed)}")
        for m in missed:
            handle_message(m.get('text', ''), pid, m.get('from_id'), m.get('conversation_message_id'),
                           m.get('date'), source="ДОГОНЯЕМ",
                           reply_to=(m.get('reply_message') or {}).get('from_id'))

if vk:
    threading.Thread(target=catch_up, daemon=True).start()


# --- 5. FLASK СЕРВЕР И WEBHOOK ---

@app.route('/', methods=['GET', 'POST'])
def bot():
    if request.method == 'GET':
        return 'Bot is running alive!', 200

    data = request.get_json(force=True, silent=True)
    if not data:
        return 'ok'

    event_type = data.get('type')

    if event_type == 'confirmation':
        print(f"✅ [ВК Webhook] Подтверждение адреса (confirmation), группа {data.get('group_id')}, отдаю {CONFIRMATION_CODE[:2]}...")
        return CONFIRMATION_CODE
    
    if event_type == 'message_new':
        obj = data.get('object', {})
        message = obj.get('message', obj)
        
        msg_id = (message.get('peer_id'), message.get('conversation_message_id'), message.get('id'))
        text = message.get('text', '')
        peer_id = message.get('peer_id')
        from_id = message.get('from_id')
        
        if peer_id and from_id:
            threading.Thread(target=handle_message,
                             args=(text, peer_id, from_id, message.get('conversation_message_id'),
                                   message.get('date'), "ВК",
                                   (message.get('reply_message') or {}).get('from_id'))).start()
            
        return 'ok'

    return 'ok'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
