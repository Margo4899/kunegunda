import os
import random
import requests
import threading
import traceback
from datetime import datetime
from flask import Flask, request
from groq import Groq
from supabase import create_client, Client

app = Flask(__name__)

# --- 1. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
VK_CONFIRMATION_TOKEN = os.environ.get("VK_CONFIRMATION_TOKEN")
VK_GROUP_TOKEN = os.environ.get("VK_GROUP_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Явно задаём версию VK API 5.199
VK_API_VERSION = "5.199"

print("==================== [СТАРТ ПРИЛОЖЕНИЯ 5.199] ====================")
print(f"🔑 VK_CONFIRMATION_TOKEN: {'Задан' if VK_CONFIRMATION_TOKEN else '❌ НЕ ЗАДАН'}")
print(f"🔑 VK_GROUP_TOKEN: {'Задан' if VK_GROUP_TOKEN else '❌ НЕ ЗАДАН'}")
print(f"🔑 GROQ_API_KEY: {'Задан' if GROQ_API_KEY else '❌ НЕ ЗАДАН'}")
print(f"🔑 SUPABASE_URL: {'Задан' if SUPABASE_URL else '❌ НЕ ЗАДАН'}")
print("==================================================================")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

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

# --- 2. ЛИМИТЫ (SUPABASE) ---
def check_and_update_limit_supabase(user_id):
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

# --- 3. ВПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def load_avataria_knowledge():
    file_path = "knowledge.txt"
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    return "в аватарии есть питомцы, золото, серебро, бои подушками, свадьбы, сквер и вип статус."

def generate_ai_response(system_instruction, user_prompt):
    if not groq_client:
        print("⚠️ [GROQ] Ключ GROQ_API_KEY отсутствует, возвращаем фолбэк.")
        return random.choice(FALLBACK_RESPONSES)

    for model_name in MODELS_FALLBACK:
        try:
            print(f"🤖 [GROQ] Запрос к модели {model_name}...")
            response = groq_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.9,
                max_tokens=300,
            )
            text = response.choices[0].message.content
            if text:
                text = text.replace("—", ",").replace("–", ",").replace("«", "").replace("»", "").replace('"', '').strip()
                if text:
                    print(f"✅ [GROQ УСПЕХ ({model_name})]: {text.lower()}")
                    return text.lower()
        except Exception as e:
            print(f"❌ [GROQ ОШИБКА ({model_name})]: {e}")
            continue

    print("⚠️ [GROQ] Ни одна модель не ответила, выдаем случайный фолбэк.")
    return random.choice(FALLBACK_RESPONSES)

# --- 4. ВЗАИМОДЕЙСТВИЕ С VK API 5.199 ---
def get_vk_user_name(user_id):
    url = "https://api.vk.com/method/users.get"
    params = {
        "user_ids": user_id, 
        "access_token": VK_GROUP_TOKEN, 
        "v": VK_API_VERSION
    }
    try:
        res = requests.get(url, params=params).json()
        if "response" in res and len(res["response"]) > 0:
            return res["response"][0].get("first_name", "новичок")
    except Exception as e:
        print(f"⚠️ [ВК ОШИБКА ИМЕНИ]: {e}")
    return "новичок"

def send_vk_message(peer_id, message_text, reply_to_msg_id=None):
    if not message_text:
        message_text = random.choice(FALLBACK_RESPONSES)

    print(f"📤 [ВК 5.199 ОТПРАВКА] В peer_id={peer_id}: '{message_text}'")

    url = "https://api.vk.com/method/messages.send"
    params = {
        "peer_id": peer_id,
        "message": message_text,
        "random_id": 0,
        "access_token": VK_GROUP_TOKEN,
        "v": VK_API_VERSION
    }
    if reply_to_msg_id and reply_to_msg_id > 0:
        params["reply_to"] = reply_to_msg_id

    try:
        res = requests.post(url, data=params).json()
        print(f"📬 [ВК 5.199 ОТВЕТ СЕРВЕРА]: {res}")
    except Exception as e:
        print(f"❌ [ВК КРИТИЧЕСКАЯ ОШИБКА ОТПРАВКИ]: {e}")

# --- 5. ОБРАБОТКА СОБЫТИЙ В ФОНЕ ---
def process_event_async(data):
    try:
        print("\n-------------------- [ФОНОВЫЙ ПОТОК СТАРТ (5.199)] --------------------")
        
        # В VK API 5.199 структура объекта находится в data['object']['message']
        obj = data.get('object', {})
        msg = obj.get('message', obj)
        
        peer_id = msg.get('peer_id')
        from_id = msg.get('from_id')
        text = msg.get('text', '').strip().lower()
        msg_id = msg.get('id') or msg.get('conversation_message_id')
        action = msg.get('action', {})

        print(f"📩 [ДАННЫЕ 5.199] peer_id={peer_id}, from_id={from_id}, msg_id={msg_id}, text='{text}'")

        # 1. Приглашение в беседу
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
                send_vk_message(peer_id, reply)
                return

            user_name = get_vk_user_name(invited_id)
            sys_prompt = SYSTEM_PROMPT_BASE + """
            представься как гадалка кунегунда, назови нового участника по имени и по доброте душевной сделай ему позитивный расклад про эту беседу.
            строгое ограничение: твой ответ должен состоять максимум из 2 предложений.
            обращение строго на ты и без указания пола.
            """
            usr_prompt = f"в беседу зашел пользователь {user_name}, поприветствуй его и сделай краткий расклад максимум в 2 предложения."
            reply = generate_ai_response(sys_prompt, usr_prompt)
            send_vk_message(peer_id, reply)
            return

        # 2. Команда ".гадать"
        if text == ".гадать" or text.startswith(".гадать"):
            print("🔮 [СОБЫТИЕ] Вызвана команда .гадать!")
            if not check_and_update_limit_supabase(from_id):
                limit_reply = random.choice(LIMIT_REACHED_RESPONSES)
                send_vk_message(peer_id, limit_reply, reply_to_msg_id=msg_id)
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
            send_vk_message(peer_id, reply, reply_to_msg_id=msg_id)
        else:
            print(f"ℹ️ [ПРОПУСК] Текст '{text}' не является командой .гадать")

    except Exception as e:
        print(f"❌ [КРИТИЧЕСКАЯ ОШИБКА В ПОТОКЕ]: {e}")
        traceback.print_exc()
    finally:
        print("-------------------- [ФОНОВЫЙ ПОТОК ЗАВЕРШЕН] --------------------\n")

# --- 6. FLASK И WEBHOOK ---
@app.route('/', methods=['GET', 'POST'])
def vk_callback():
    if request.method == 'GET':
        return 'Bot is running alive!', 200

    data = request.get_json(force=True, silent=True)
    print(f"\n📥 [ВХОДЯЩИЙ HTTP POST 5.199]: {data}")

    if not data:
        print("⚠️ [ВХОДЯЩИЙ HTTP] Данные пустые!")
        return 'ok'

    type_event = data.get('type')

    if type_event == 'confirmation':
        print(f"✅ [CONFIRMATION] Отправляем токен: {VK_CONFIRMATION_TOKEN}")
        return str(VK_CONFIRMATION_TOKEN)

    elif type_event == 'message_new':
        obj = data.get('object', {})
        msg = obj.get('message', obj)
        msg_id = msg.get('id') or msg.get('conversation_message_id')

        if msg_id:
            if msg_id in processed_msg_ids:
                print(f"⚠️ [ДУБЛИКАТ] Сообщение msg_id={msg_id} уже обрабатывалось, пропуск.")
                return 'ok'
            processed_msg_ids.add(msg_id)
            if len(processed_msg_ids) > 1000:
                processed_msg_ids.clear()

        print("🚀 [ПОТОК] Запускаем обработку события в фоновом режиме...")
        threading.Thread(target=process_event_async, args=(data,)).start()
        return 'ok'

    return 'ok'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
