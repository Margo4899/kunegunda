import os
import random
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from groq import Groq, GroqError
from supabase import create_client, Client

app = Flask(__name__)

# Переменные окружения
VK_CONFIRMATION_TOKEN = os.environ.get("VK_CONFIRMATION_TOKEN")
VK_SECRET_KEY = os.environ.get("VK_SECRET_KEY")
VK_GROUP_TOKEN = os.environ.get("VK_GROUP_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Инициализация клиентов API
groq_client = Groq(api_key=GROQ_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Список моделей с ротацией
MODELS_FALLBACK = [
    'openai/gpt-oss-120b',
    'openai/gpt-oss-20b',
    'qwen/qwen3.8-27b',
    'qwen/qwen3.6-27b'
]

# Запасные ответы Кунегунды при ошибках ИИ
FALLBACK_RESPONSES = [
    "хрустальный шар под диван закатился, приди позже, когда вымету пыль.",
    "туман застлал мои очи, свечи погасли, приходи чуть позже.",
    "духи аватарии ушли на перерыв, даже карты отказываются шептаться.",
    "перст судьбы устал указывать, зайди позже, когда карты остынут.",
    "зелье пока кипит, не могу разглядеть твоё будущее, зайди через часок."
]

# Ответы Кунегунды при исчерпании лимита (напоминание про 3 гадания)
LIMIT_REACHED_RESPONSES = [
    "зело много желаешь знать за один день, твои 3 гадания на сегодня исчерпаны, приходи завтра.",
    "трижды перст судьбы уже указал тебе путь на сегодня, больше шар не покажет, жди завтрашнего дня.",
    "не пытай судьбу свыше меры, лимит в 3 гадания на сегодня исчерпан, приходи завтра.",
    "очи мои устали зрить твоё будущее, 3 гадания в день это предел, отдохни до завтра.",
    "уповай на терпение, три предсказания на сей день ты уже исчерпал, явись утречком."
]

# Системные правила для характера и стиля Кунегунды
SYSTEM_PROMPT_BASE = """
ты гадалка кунегунда, древняя и опытная предсказательница.
твой характер: саркастичная, язвительная, любишь подкалывать и издеваться, но в глубине души добрая.
обрати внимание: ты любишь время от времени вворачивать древние и устаревшие словечки (например: ибо, кабы, зело, очи, перст, зрить, уповать, паче, суженый, ведать).

строгие правила общения:
1. всегда обращайся к участнику строго на ты.
2. никогда не используй прошедшее время для участника (избегай слов типа пошел, пошла, кушала, кушал). обходи род нейтрально, используя будущее время, настоящее время или конструкции без рода (например: тебя ждет, сможешь найти, предстоит увидеть, будешь радоваться).

строгие правила стиля:
1. пиши строго с маленькой буквы.
2. никогда не используй тире (—) и кавычки («» или "").
3. используй только запятые и обычные точки.
4. никакой ненормативной лексики и мата.
"""

def check_and_update_limit_supabase(user_id):
    """
    Проверяет и обновляет лимит гаданий через Supabase.
    Максимум 3 предсказания в день на пользователя.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    
    try:
        res = supabase.table("user_limits").select("*").eq("user_id", user_id).execute()
        data = res.data

        if not data:
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
            supabase.table("user_limits").update({
                "count": 1,
                "last_date": today
            }).eq("user_id", user_id).execute()
            return True

        if count >= 3:
            return False

        supabase.table("user_limits").update({
            "count": count + 1
        }).eq("user_id", user_id).execute()
        return True

    except Exception as e:
        print(f"[LOG] ошибка работы с Supabase: {e}")
        return True

def load_avataria_knowledge():
    """Чтение всей базы знаний про Аватарию из файла knowledge.txt"""
    file_path = "knowledge.txt"
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                return content
    return "в аватарии есть питомцы, золото, серебро, бои подушками, свадьбы, сквер и вип статус."

def generate_ai_response(system_instruction, user_prompt):
    """Запрос к Groq с автопереключением моделей"""
    for model_name in MODELS_FALLBACK:
        try:
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
                    return text.lower()
        except GroqError as e:
            print(f"[LOG] ошибка модели {model_name}: {e}, переключаемся...")
            continue
        except Exception as e:
            print(f"[LOG] ошибка: {e}")
            continue

    return random.choice(FALLBACK_RESPONSES)

def get_vk_user_name(user_id):
    """Получение имени пользователя ВК"""
    url = "https://api.vk.com/method/users.get"
    params = {
        "user_ids": user_id,
        "access_token": VK_GROUP_TOKEN,
        "v": "5.131"
    }
    res = requests.get(url, params=params).json()
    if "response" in res and len(res["response"]) > 0:
        return res["response"][0].get("first_name", "новичок")
    return "новичок"

def send_vk_message(peer_id, message_text, reply_to_msg_id=None):
    """Отправка сообщения в беседу с ответом на конкретное сообщение"""
    if not message_text or not str(message_text).strip():
        message_text = random.choice(FALLBACK_RESPONSES)

    url = "https://api.vk.com/method/messages.send"
    params = {
        "peer_id": peer_id,
        "message": message_text,
        "random_id": 0,
        "access_token": VK_GROUP_TOKEN,
        "v": "5.131"
    }
    if reply_to_msg_id and reply_to_msg_id > 0:
        params["reply_to"] = reply_to_msg_id

    res = requests.post(url, data=params).json()
    print(f"[LOG] Результат отправки сообщения ВК: {res}")

@app.route('/', methods=['POST'])
def vk_callback():
    data = request.get_json(force=True)
    print(f"[LOG] Входящий запрос от ВК: {data}")

    type_event = data.get('type')

    if type_event == 'confirmation':
        print(f"[LOG] Отправляем подтверждение: {VK_CONFIRMATION_TOKEN}")
        return str(VK_CONFIRMATION_TOKEN)

    elif type_event == 'message_new':
        msg = data.get('object', {}).get('message', {})
        peer_id = msg.get('peer_id')
        from_id = msg.get('from_id')
        text = msg.get('text', '').strip().lower()
        msg_id = msg.get('id', 0)
        action = msg.get('action', {})

        # 1. Приглашение в беседу
        if action.get('type') in ['chat_invite_user', 'chat_invite_user_by_link']:
            invited_id = action.get('member_id', 0)

            # Проверяем: если добавили самого бота
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
                return 'ok'

            # Если добавили обычного нового пользователя
            user_name = get_vk_user_name(invited_id)

            sys_prompt = SYSTEM_PROMPT_BASE + """
            представься как гадалка кунегунда, назови нового участника по имени и по доброте душевной сделай ему позитивный расклад про эту беседу.
            строгое ограничение: твой ответ должен состоять максимум из 2 предложений.
            обращение строго на ты и без указания пола.
            """
            usr_prompt = f"в беседу зашел пользователь {user_name}, поприветствуй его и сделай краткий расклад максимум в 2 предложения."
            
            reply = generate_ai_response(sys_prompt, usr_prompt)
            send_vk_message(peer_id, reply)
            return 'ok'

        # 2. Команда ".гадать"
        if text == ".гадать" or text.startswith(".гадать"):
            if not check_and_update_limit_supabase(from_id):
                limit_reply = random.choice(LIMIT_REACHED_RESPONSES)
                send_vk_message(peer_id, limit_reply, reply_to_msg_id=msg_id)
                return 'ok'

            # Загружаем всю базу знаний
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
            return 'ok'

        return 'ok'

    return 'ok'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
