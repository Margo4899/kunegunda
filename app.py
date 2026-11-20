import os
import sys
import json
import re
import random
import requests
import threading
import time
import traceback
from datetime import datetime
from flask import Flask, request
from groq import Groq
from supabase import create_client, Client

app = Flask(__name__)

# Чтобы логи сразу появлялись в Render, а не застревали в буфере
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# --- 1. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
VK_CONFIRMATION_TOKEN = os.environ.get("VK_CONFIRMATION_TOKEN")
VK_GROUP_TOKEN = os.environ.get("VK_GROUP_TOKEN")
VK_SECRET_KEY = os.environ.get("VK_SECRET_KEY", "poop")  # Автоматически учитываем secret: poop
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

VK_API_VERSION = "5.199"
# На время тестов: у этих людей (VK id через запятую) нет лимита гаданий.
# Чтобы вернуть лимит, задайте на Render переменную LIMIT_EXEMPT_IDS пустой или уберите id отсюда.
LIMIT_EXEMPT_IDS = {x.strip() for x in os.environ.get("LIMIT_EXEMPT_IDS", "691070777").split(",") if x.strip()}
VK_GROUP_ID = os.environ.get("VK_GROUP_ID", "241804933")
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://kunegunda.onrender.com")

print("==================== [СТАРТ КУНЕГУНДЫ] ====================")
print(f"🔑 VK_CONFIRMATION_TOKEN: {'Задан' if VK_CONFIRMATION_TOKEN else '❌ НЕ ЗАДАН'}")
print(f"🔑 VK_GROUP_TOKEN: {'Задан' if VK_GROUP_TOKEN else '❌ НЕ ЗАДАН'}")
print(f"🔒 VK_SECRET_KEY: {'Задан' if VK_SECRET_KEY else 'не задан'}")
print(f"🔑 GEMINI_API_KEY: {'Задан' if GEMINI_API_KEY else '❌ НЕ ЗАДАН (будет использоваться только Groq)'}")
print(f"🔑 GROQ_API_KEY: {'Задан' if GROQ_API_KEY else '❌ НЕ ЗАДАН'}")
print(f"🔑 SUPABASE_URL: {'Задан' if SUPABASE_URL else '❌ НЕ ЗАДАН'}")
print("==================================================================")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"❌ [SUPABASE] Не удалось подключиться, лимиты отключены: {e}")

processed_msg_ids = set()

MODELS_FALLBACK = [
    'openai/gpt-oss-120b',
    'openai/gpt-oss-20b',
    'qwen/qwen3.8-27b',
    'qwen/qwen3.6-27b'
]

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
ты гадалка кунегунда, древняя и опытная предсказательница из тропикании, живешь в игре аватария.
твой характер разный, как у живого человека: то теплая и заботливая, то загадочная, то веселая, то с легкой иронией.
подшучиваешь иногда и всегда беззлобно, никогда не грубишь, не оскорбляешь и не унижаешь.
говоришь простым живым современным языком, как опытная гадалка с чувством юмора.
не затрагиваешь запрещенные темы: взломы, продажу аккаунтов, читы, накрутки, политику, религию, взрослые темы.
иногда, примерно в каждом втором ответе, можешь вставить одно старинное словечко (например: ибо, зело, очи, ведать), но не больше одного на ответ и только если оно звучит естественно.

строгие правила общения:
1. всегда обращайся к участнику строго на ты.
2. род: если ниже сказано, парень это или девушка, используй правильный род. если пол неизвестен, обходи род (будущее или настоящее время, без слов вроде пошел или пошла).

строгие правила стиля:
1. пиши строго с маленькой буквы.
2. никогда не используй тире (—) и кавычки («» или "").
3. используй только запятые и обычные точки, без точки с запятой.
4. никакой ненормативной лексики и мата.
"""

# --- 2. ЛИМИТЫ (SUPABASE) ---
def check_and_update_limit_supabase(user_id):
    if str(user_id) in LIMIT_EXEMPT_IDS:
        print(f"🧪 [ТЕСТ] Лимит для user_id={user_id} отключен (LIMIT_EXEMPT_IDS).")
        return True
    if not supabase:
        print("⚠️ [SUPABASE] Клиент Supabase не инициализирован, пропускаем лимит.")
        return True
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        print(f"📊 [SUPABASE] Проверка лимита для user_id={user_id}...")
        res = supabase.table("user_limits").select("*").eq("user_id", user_id).execute()
        data = res.data

        if not data:
            print("📊 [SUPABASE] Пользователь новый, создаем запись...")
            supabase.table("user_limits").insert({
                "user_id": user_id,
                "count": 1,
                "last_date": today
            }).execute()
            return True

        user_record = data[0]
        last_date = str(user_record.get("last_date"))
        count = user_record.get("count", 0)

        if last_date != today:
            print("📊 [SUPABASE] Новый день, сбрасываем лимит...")
            supabase.table("user_limits").update({
                "count": 1,
                "last_date": today
            }).eq("user_id", user_id).execute()
            return True

        if count >= 3:
            print(f"⚠️ [SUPABASE] Лимит исчерпан для user_id={user_id} (уже {count} гаданий).")
            return False

        print(f"📊 [SUPABASE] Добавляем +1 гадание (было {count})...")
        supabase.table("user_limits").update({
            "count": count + 1
        }).eq("user_id", user_id).execute()
        return True

    except Exception as e:
        print(f"❌ [SUPABASE ОШИБКА]: {e}")
        traceback.print_exc()
        return True

# --- 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def load_avataria_facts():
    """Разбивает knowledge.txt на отдельные факты: (тема раздела, текст факта)."""
    facts = []
    section = "аватария"
    current = None
    try:
        with open("knowledge.txt", "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip()
                if line.startswith("## "):
                    section = re.sub(r"^##\s*\d*\.?\s*", "", line).strip()
                    current = None
                elif line.startswith("- "):
                    current = [section, line[2:].strip()]
                    facts.append(current)
                elif line.startswith("  ") and current and line.strip():
                    current[1] += " " + line.strip()
                else:
                    current = None
    except FileNotFoundError:
        pass
    if not facts:
        facts = [["аватария", "в аватарии есть питомцы, золото, серебро, бои подушками, свадьбы, сквер и вип статус."]]
    print(f"📚 [ЗНАНИЯ] Загружено фактов про аватарию: {len(facts)}")
    return facts

AVATARIA_FACTS = load_avataria_facts()
recent_facts_by_user = {}
recent_facts_global = []

# Свежие новинки (разделы с этими словами в названии) выпадают примерно в половине гаданий
NEW_SECTION_WORDS = ("новинки", "мини-игры и их билеты", "праздники, ярмарка", "локации и интересные")
NEW_FACT_CHANCE = 0.5

def pick_fact(user_id):
    """Случайный факт, который этот игрок недавно не получал и который не выпадал в последних гаданиях."""
    seen = recent_facts_by_user.setdefault(user_id, [])
    n = len(AVATARIA_FACTS)
    blocked = set(seen) | set(recent_facts_global)
    choices = [i for i in range(n) if i not in blocked] or [i for i in range(n) if i not in seen] or list(range(n))
    fresh = [i for i in choices if any(w in AVATARIA_FACTS[i][0].lower() for w in NEW_SECTION_WORDS)]
    if fresh and random.random() < NEW_FACT_CHANCE:
        choices = fresh
    i = random.choice(choices)
    seen.append(i)
    del seen[:-min(20, n - 1 if n > 1 else 1)]
    recent_facts_global.append(i)
    del recent_facts_global[:-min(5, n - 1 if n > 1 else 1)]
    return AVATARIA_FACTS[i]

# О чем предсказание
FORTUNE_MOODS = [
    "удачу и приятный сюрприз",
    "мелкую неприятность с советом, как ее пережить",
    "смешное предупреждение",
    "неожиданный поворот",
    "совет, что сделать сегодня в игре",
    "романтическую встречу или симпатию в игре",
    "что-то про дружбу или нового друга в игре",
    "приятную находку или выгоду",
]

# Каким тоном говорит Кунегунда (выбирается случайно, чтобы она не была все время одинаковой)
FORTUNE_TONES = [
    ("тепло и ласково, как добрая бабушка", 3),
    ("загадочно и таинственно, как будто смотришь в хрустальный шар", 3),
    ("весело и игриво", 3),
    ("с легкой беззлобной иронией", 2),
    ("театрально и немного драматично", 1),
    ("заботливо, как подруга, которая желает добра", 2),
    ("коротко и уверенно, как опытная гадалка", 2),
]

def load_lore():
    try:
        with open("lore.txt", "r", encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []

KUNEGUNDA_LORE = load_lore()
LORE_CHANCE = 0.17  # примерно каждое шестое гадание

def pick_tone():
    tones, weights = zip(*FORTUNE_TONES)
    return random.choices(tones, weights=weights)[0]

# Символы, которых не должно быть в ответе: китайские, японские, корейские иероглифы и т.п.
BAD_CHARS_RE = re.compile(r"[\u0590-\u08ff\u0e00-\u0fff\u1100-\u11ff\u3000-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")
THINK_RE = re.compile(r"<think>.*?</think>", re.S)

def clean_ai_text(text, max_sentences, max_chars=None, finished=False):
    """Чистит ответ модели. Возвращает None, если ответ испорчен и надо спросить другую модель."""
    if not text:
        return None
    text = THINK_RE.sub("", text)
    text = re.sub(r"[*_#`>]", "", text)  # markdown
    text = text.replace("—", ",").replace("–", ",").replace("«", "").replace("»", "").replace('"', "").replace("“", "").replace("”", "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    text = re.sub(r",+", ",", text)
    text = text.replace(";", ".")

    if BAD_CHARS_RE.search(text):
        print(f"⚠️ [ФИЛЬТР] В ответе иероглифы, пробуем другую модель: {text}")
        return None
    if len(re.findall(r"[а-яё]", text)) < 0.6 * len(re.findall(r"[a-zа-яё]", text) or [1]):
        print(f"⚠️ [ФИЛЬТР] Ответ не на русском, пробуем другую модель: {text}")
        return None

    # модель сама закончила ответ, но забыла точку в конце: дописываем
    if finished and text and text[-1] not in ".!?":
        text += "."

    # оставляем только целые предложения, не больше max_sentences
    sentences = re.findall(r"[^.!?]+[.!?]+", text)
    if not sentences:
        print(f"⚠️ [ФИЛЬТР] Ответ оборван, пробуем другую модель: {text}")
        return None
    text = " ".join(x.strip() for x in sentences[:max_sentences])
    if len(text) < 15:
        return None
    if max_chars and len(text) > max_chars:
        print(f"⚠️ [ФИЛЬТР] Ответ слишком длинный ({len(text)} символов), просим заново: {text}")
        return None
    return text

# Бесплатный уровень: 3.5/3.1 Flash Lite по 500 запросов в день, 3.8 Flash только 20 в день
GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
]

def ask_gemini(model_name, system_instruction, user_prompt):
    """Запрос к Google Gemini (бесплатный уровень). Возвращает текст или бросает исключение."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 2048},
    }
    r = requests.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=40)
    data = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {data.get('error', {}).get('message', data)}")
    cand = (data.get("candidates") or [{}])[0]
    parts = cand.get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return text, cand.get("finishReason")

def generate_ai_response(system_instruction, user_prompt, max_sentences=2, max_chars=None):
    system_instruction += "\nотвечай только на русском языке, без иностранных слов и иероглифов. не используй markdown, звездочки и списки."

    # 1. Сначала Gemini: лучше всех пишет по-русски
    if GEMINI_API_KEY:
        for model_name in GEMINI_MODELS:
            for attempt in range(3):
                try:
                    print(f"🤖 [GEMINI] Запрос к модели {model_name} (попытка {attempt + 1})...")
                    raw, finish = ask_gemini(model_name, system_instruction, user_prompt)
                    text = clean_ai_text(raw, max_sentences, max_chars, finished=(finish == "STOP"))
                    if text:
                        print(f"✅ [GEMINI УСПЕХ ({model_name})]: {text}")
                        return text
                    print(f"⚠️ [GEMINI] Плохой ответ от {model_name} (finish={finish})")
                except Exception as e:
                    print(f"❌ [GEMINI ОШИБКА ({model_name})]: {e}")
                    break

    # 2. Запасной вариант: Groq
    return generate_groq_response(system_instruction, user_prompt, max_sentences, max_chars)

def generate_groq_response(system_instruction, user_prompt, max_sentences=2, max_chars=None):
    if not groq_client:
        print("⚠️ [GROQ] Ключ GROQ_API_KEY отсутствует, возвращаем фолбэк.")
        return random.choice(FALLBACK_RESPONSES)

    for model_name in MODELS_FALLBACK:
        for attempt in range(3):
            try:
                print(f"🤖 [GROQ] Запрос к модели {model_name} (попытка {attempt + 1})...")
                extra = {}
                if model_name.startswith("openai/gpt-oss"):
                    extra["reasoning_effort"] = "low"   # меньше «раздумий», меньше обрывов
                elif model_name.startswith("qwen/"):
                    extra["reasoning_format"] = "hidden"
                response = groq_client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.9,
                    max_tokens=1500,  # с запасом: модели gpt-oss тратят часть токенов на раздумья
                    **extra,
                )
                choice = response.choices[0]
                text = clean_ai_text(choice.message.content, max_sentences, max_chars, finished=(choice.finish_reason == "stop"))
                if text:
                    print(f"✅ [GROQ УСПЕХ ({model_name})]: {text}")
                    return text
                print(f"⚠️ [GROQ] Плохой ответ от {model_name} (finish_reason={choice.finish_reason})")
            except Exception as e:
                print(f"❌ [GROQ ОШИБКА ({model_name})]: {e}")
                break  # модель недоступна, переходим к следующей

    print("⚠️ [GROQ] Ни одна модель не дала нормальный ответ, выдаем случайный фолбэк.")
    return random.choice(FALLBACK_RESPONSES)

# --- 4. ВЗАИМОДЕЙСТВИЕ С VK API 5.199 ---
_user_cache = {}

def get_vk_user(user_id):
    """Имя и пол из профиля ВК: (имя, 'f' / 'm' / None). Пол ВК: 1 женский, 2 мужской, 0 не указан."""
    if user_id in _user_cache:
        return _user_cache[user_id]
    name, sex = None, None
    try:
        res = requests.get("https://api.vk.com/method/users.get", params={
            "user_ids": user_id, "fields": "sex",
            "access_token": VK_GROUP_TOKEN, "v": VK_API_VERSION
        }, timeout=15).json()
        if res.get("response"):
            u = res["response"][0]
            name = u.get("first_name") or None
            sex = {1: "f", 2: "m"}.get(u.get("sex"))
        elif "error" in res:
            print(f"⚠️ [ВК ОШИБКА ИМЕНИ]: {res['error'].get('error_msg')}")
    except Exception as e:
        print(f"⚠️ [ВК ОШИБКА ИМЕНИ]: {e}")
    _user_cache[user_id] = (name, sex)
    return name, sex

def get_vk_user_name(user_id):
    return get_vk_user(user_id)[0] or "новичок"

def person_rule(name, sex):
    """Кусок инструкции для модели: как обращаться к человеку."""
    parts = []
    if name:
        parts.append(f"собеседника зовут {name}. обратись по имени, естественно, один раз.")
    if sex == "f":
        parts.append("это девушка, пиши о ней в женском роде.")
    elif sex == "m":
        parts.append("это парень, пиши о нем в мужском роде.")
    else:
        parts.append("пол неизвестен, обходи род, без слов вроде пошел или пошла.")
    return " ".join(parts)

THINKING_PHRASES = [
    "🔮 {name}смотрю в шар, погоди минутку...",
    "🔮 {name}тасую карты, не торопи судьбу...",
    "🔮 {name}шар мутнеет, сейчас прояснится...",
    "🔮 {name}зажигаю свечи, духи уже шепчутся...",
    "🔮 {name}раскладываю карты, не дыши...",
]

def edit_vk_message(peer_id, cmid, message_text):
    """Заменяет текст уже отправленного сообщения. Возвращает True, если получилось."""
    try:
        res = requests.post("https://api.vk.com/method/messages.edit", data={
            "peer_id": peer_id, "conversation_message_id": cmid,
            "message": message_text, "keep_forward_messages": 1,
            "access_token": VK_GROUP_TOKEN, "v": VK_API_VERSION
        }, timeout=40).json()
        if "error" in res:
            print(f"⚠️ [ВК] Не удалось заменить сообщение: {res['error'].get('error_msg')}")
            return False
        print(f"✏️ [ВК] Сообщение {cmid} заменено на предсказание")
        return True
    except Exception as e:
        print(f"⚠️ [ВК] Не удалось заменить сообщение: {e}")
        return False

def send_vk_message(peer_id, message_text, reply_to_cmid=None):
    """Отправляет сообщение. Возвращает conversation_message_id отправленного сообщения (или None)."""
    if not message_text:
        message_text = random.choice(FALLBACK_RESPONSES)

    if not VK_GROUP_TOKEN:
        print("❌ [ВК] VK_GROUP_TOKEN не задан на Render, отправить сообщение невозможно!")
        return

    print(f"📤 [ВК ОТПРАВКА] В peer_id={peer_id}: '{message_text}'")

    params = {
        "peer_ids": peer_id,  # с peer_ids ВК возвращает conversation_message_id, чтобы потом заменить текст
        "message": message_text,
        "random_id": random.randint(1, 2**31 - 1),
        "access_token": VK_GROUP_TOKEN,
        "v": VK_API_VERSION
    }
    # В беседах reply_to не работает для сообществ, ответ делается через forward
    if reply_to_cmid:
        params["forward"] = json.dumps({
            "peer_id": peer_id,
            "conversation_message_ids": [reply_to_cmid],
            "is_reply": 1
        })

    try:
        res = requests.post("https://api.vk.com/method/messages.send", data=params, timeout=40).json()
        if "error" in res and reply_to_cmid:
            print(f"⚠️ [ВК] Ответ с цитатой не прошёл ({res['error'].get('error_msg')}), шлём без цитаты")
            params.pop("forward", None)
            res = requests.post("https://api.vk.com/method/messages.send", data=params, timeout=40).json()
        if "error" in res:
            print(f"❌ [ВК ОШИБКА ОТПРАВКИ] код {res['error'].get('error_code')}: {res['error'].get('error_msg')}")
            return None
        print(f"📬 [ВК] Отправлено: {res}")
        resp = res.get("response")
        if isinstance(resp, list) and resp:
            return resp[0].get("conversation_message_id")
    except Exception as e:
        print(f"❌ [ВК КРИТИЧЕСКАЯ ОШИБКА ОТПРАВКИ]: {e}")
    return None

def is_fortune_command(text):
    """Команда срабатывает на: гадать, .гадать, /гадать, !гадать, а также с упоминанием бота."""
    t = re.sub(r"\[(club|public)\d+\|[^\]]*\]", "", text)  # убираем упоминание [club123|кунегунда]
    t = t.strip(" ,.!/")
    return t == "гадать" or t.startswith("гадать ")

# --- 5. ОБРАБОТКА СОБЫТИЙ В ФОНЕ ---
def process_event_async(data):
    try:
        print("\n-------------------- [ФОНОВЫЙ ПОТОК СТАРТ] --------------------")
        
        obj = data.get('object', {})
        msg = obj.get('message', obj)
        
        peer_id = msg.get('peer_id')
        from_id = msg.get('from_id')
        text = msg.get('text', '').strip().lower()
        cmid = msg.get('conversation_message_id')
        action = msg.get('action') or {}

        print(f"📩 [ДАННЫЕ] peer_id={peer_id}, from_id={from_id}, cmid={cmid}, text='{text}'")

        # 1. Приглашение в беседу
        if action.get('type') in ['chat_invite_user', 'chat_invite_user_by_link']:
            invited_id = action.get('member_id', 0)
            print(f"🎉 [СОБЫТИЕ] Добавлен участник: invited_id={invited_id}")

            if invited_id < 0:
                sys_prompt = SYSTEM_PROMPT_BASE + """
                расскажи о себе: ты гадалка кунегунда, долго жила в пещерах тропикании, но решила вылезти к людям и помогать им своими магическими силами.
                объясни, что ты умеешь предсказывать судьбу в аватарии.
                обязательно напиши подсказку: чтобы получить предсказание, нужно написать слово гадать.
                напомни про ограничение: каждому смертному положено максимум 3 гадания в день.
                """
                usr_prompt = "тебя только что добавили в беседу, поприветствуй всех участников и расскажи о себе и своих правилах. уложись в 4 предложения."
                reply = generate_ai_response(sys_prompt, usr_prompt, max_sentences=5)
                send_vk_message(peer_id, reply)
                return

            user_name, user_sex = get_vk_user(invited_id)
            user_name = user_name or "новичок"
            tone = pick_tone()
            print(f"🎭 [ТОН ПРИВЕТСТВИЯ] {tone}")
            sys_prompt = SYSTEM_PROMPT_BASE + f"""
            представься как гадалка кунегунда, назови нового участника по имени и сделай ему позитивный расклад про эту беседу.
            говори {tone}.
            строгое ограничение: твой ответ должен состоять максимум из 2 предложений.
            обращение строго на ты. {person_rule(user_name, user_sex)}
            """
            usr_prompt = f"в беседу пришел новый участник по имени {user_name}, поприветствуй и сделай краткий расклад максимум в 2 предложения."
            reply = generate_ai_response(sys_prompt, usr_prompt)
            send_vk_message(peer_id, reply)
            return

        # 2. Команда ".гадать"
        if is_fortune_command(text):
            print("🔮 [СОБЫТИЕ] Вызвана команда гадать!")
            if not check_and_update_limit_supabase(from_id):
                limit_reply = random.choice(LIMIT_REACHED_RESPONSES)
                send_vk_message(peer_id, limit_reply, reply_to_cmid=cmid)
                return

            name, sex = get_vk_user(from_id)
            print(f"👤 [ИГРОК] {name or 'имя неизвестно'}, пол: {sex or 'неизвестен'}")
            thinking = random.choice(THINKING_PHRASES).format(name=f"{name}, " if name else "")
            placeholder_cmid = send_vk_message(peer_id, thinking, reply_to_cmid=cmid)

            topic, fact = pick_fact(from_id)
            mood = random.choice(FORTUNE_MOODS)
            tone = pick_tone()
            lore = random.choice(KUNEGUNDA_LORE) if KUNEGUNDA_LORE and random.random() < LORE_CHANCE else None
            print(f"📚 [ФАКТ] {topic}: {fact}")
            print(f"🎭 [ТОН] {tone} | о чем: {mood} | о себе: {lore or 'нет'}")

            lore_rule = ""
            max_sent, max_len, max_words = 2, 220, 25
            if lore:
                lore_rule = f"""
            7. в этот раз вскользь, одной короткой фразой, упомяни что-то о себе: {lore}. это просто личная деталь, предсказание все равно про факт выше."""
                max_sent, max_len, max_words = 3, 300, 35

            sys_prompt = SYSTEM_PROMPT_BASE + f"""
            предсказание делается про игру аватария, строго про одну тему:
            тема: {topic}
            факт: {fact}

            как писать предсказание:
            1. только про этот факт. не упоминай другие вещи из игры, не придумывай новые предметы.
            2. предскажи {mood}.
            3. говори {tone}.
            4. не больше {max_sent} коротких предложений, всего не больше {max_words} слов.
            5. смысл должен быть понятен с первого прочтения, без нагромождений.
            6. обращайся на ты. {person_rule(name, sex)}{lore_rule}
            """
            usr_prompt = "погадай мне."
            reply = generate_ai_response(sys_prompt, usr_prompt, max_sentences=max_sent, max_chars=max_len)
            if not (placeholder_cmid and edit_vk_message(peer_id, placeholder_cmid, reply)):
                send_vk_message(peer_id, reply, reply_to_cmid=cmid)
        else:
            print(f"ℹ️ [ПРОПУСК] Текст '{text}' не является командой гадать")

    except Exception as e:
        print(f"❌ [КРИТИЧЕСКАЯ ОШИБКА В ПОТОКЕ]: {e}")
        traceback.print_exc()
    finally:
        print("-------------------- [ФОНОВЫЙ ПОТОК ЗАВЕРШЕН] --------------------\n")

# --- 6. LONG POLL: бот сам забирает сообщения из ВК ---
# ВК не может подключиться к Render (Can't connect to server),
# поэтому вместо Callback API используем Bots Long Poll API.
def vk_api_call(method, **params):
    params.update({"access_token": VK_GROUP_TOKEN, "v": VK_API_VERSION})
    res = requests.post(f"https://api.vk.com/method/{method}", data=params, timeout=30).json()
    if "error" in res:
        raise RuntimeError(f"{method}: код {res['error'].get('error_code')}: {res['error'].get('error_msg')}")
    return res["response"]

def long_poll_loop():
    if not VK_GROUP_TOKEN:
        print("❌ [LONG POLL] VK_GROUP_TOKEN не задан, Long Poll не запущен.")
        return
    server = None
    while True:
        try:
            if server is None:
                server = vk_api_call("groups.getLongPollServer", group_id=VK_GROUP_ID)
                print("✅ [LONG POLL] Подключилась к ВК, жду сообщений...")
            r = requests.get(server["server"], params={
                "act": "a_check", "key": server["key"], "ts": server["ts"], "wait": 25
            }, timeout=40).json()

            if "failed" in r:
                if r["failed"] == 1:
                    server["ts"] = r["ts"]
                else:
                    print(f"🔄 [LONG POLL] Переподключение (failed={r['failed']})")
                    server = None
                continue

            server["ts"] = r["ts"]
            for update in r.get("updates", []):
                if update.get("type") == "message_new":
                    obj = update.get("object", {})
                    msg = obj.get("message", obj)
                    key = (msg.get("peer_id"), msg.get("conversation_message_id"), msg.get("id"))
                    if key in processed_msg_ids:
                        continue
                    processed_msg_ids.add(key)
                    if len(processed_msg_ids) > 1000:
                        processed_msg_ids.clear()
                    threading.Thread(target=process_event_async, args=(update,), daemon=True).start()
        except Exception as e:
            print(f"❌ [LONG POLL ОШИБКА]: {e}")
            if "код 15" in str(e) or "код 100" in str(e):
                print("ℹ️ Включите Long Poll API: Управление → Работа с API → Long Poll API.")
            server = None
            time.sleep(10)

def keep_awake_loop():
    """Бесплатный Render засыпает без входящих запросов, поэтому раз в 10 минут стучимся сами к себе."""
    while True:
        time.sleep(600)
        try:
            requests.get(SELF_URL, timeout=30)
        except Exception as e:
            print(f"⚠️ [KEEP AWAKE]: {e}")

_started = False
def start_background():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=long_poll_loop, daemon=True).start()
    threading.Thread(target=keep_awake_loop, daemon=True).start()

start_background()

# --- 7. FLASK (страница проверки и запасной Callback API) ---
@app.route('/', methods=['GET', 'POST'])
def vk_callback():
    if request.method == 'GET':
        return 'Bot is running alive!', 200

    data = request.get_json(force=True, silent=True)
    print(f"\n📥 [ВХОДЯЩИЙ ЗАПРОС ОТ ВК]: {data}")

    if not data:
        print("⚠️ [ВХОДЯЩИЙ HTTP] Данные пустые!")
        return 'ok'

    # Проверяем секретный ключ из ВК
    secret = data.get('secret')
    if VK_SECRET_KEY and secret and secret != VK_SECRET_KEY:
        print(f"⚠️ [БЕЗОПАСНОСТЬ] Неверный secret key: {secret}")
        return 'ok'

    type_event = data.get('type')

    if type_event == 'confirmation':
        if not VK_CONFIRMATION_TOKEN:
            print("❌ [CONFIRMATION] VK_CONFIRMATION_TOKEN не задан на Render! ВК не сможет подтвердить сервер.")
            return 'error', 500
        print(f"✅ [CONFIRMATION] Отправляем строку подтверждения: {VK_CONFIRMATION_TOKEN}")
        return VK_CONFIRMATION_TOKEN.strip(), 200

    elif type_event == 'message_new':
        obj = data.get('object', {})
        msg = obj.get('message', obj)
        msg_key = (msg.get('peer_id'), msg.get('conversation_message_id'), msg.get('id'))

        if msg_key[1] or msg_key[2]:
            if msg_key in processed_msg_ids:
                print(f"⚠️ [ДУБЛИКАТ] Сообщение {msg_key} уже обрабатывалось, пропуск.")
                return 'ok'
            processed_msg_ids.add(msg_key)
            if len(processed_msg_ids) > 1000:
                processed_msg_ids.clear()

        print("🚀 [ПОТОК] Запускаем обработку события в фоновом режиме...")
        threading.Thread(target=process_event_async, args=(data,)).start()
        return 'ok'

    return 'ok'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
