import os
import sys
import json
import re
import random
import requests
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta

# Лимиты обновляются в полночь по Москве (МСК = UTC+3, без перехода на летнее время)
MSK = timezone(timedelta(hours=3))

def msk_now():
    return datetime.now(MSK)
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
# Для тестов: у людей из переменной LIMIT_EXEMPT_IDS на Render (VK id через запятую) нет лимитов. По умолчанию лимиты у всех.
LIMIT_EXEMPT_IDS = {x.strip() for x in os.environ.get("LIMIT_EXEMPT_IDS", "").split(",") if x.strip()}
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

# Отдельный абзац после гадания: сколько осталось
REMAINING_NOTES = {
    2: [
        "🔮 у тебя осталось еще 2 гадания на сегодня, шар пока теплый.",
        "🕯 еще две свечи горят для тебя сегодня, приходи, если не боишься правды.",
        "🃏 в колоде для тебя на сегодня еще 2 расклада.",
    ],
    1: [
        "🕯 осталось одно гадание на сегодня, береги его, как последнюю свечу.",
        "🔮 последний расклад на сегодня ждет тебя, не растрать его на пустяки.",
        "🃏 в колоде осталась одна карта для тебя, выбирай момент с умом.",
    ],
    0: [
        "🌙 это было последнее гадание на сегодня, шар уходит спать. новые гадания появятся после полуночи по МСК.",
        "🌙 на сегодня все, свечи погасли. после полуночи по МСК зажгу их снова.",
        "💤 три расклада позади, духи устали. жду тебя после полуночи по МСК.",
    ],
}

def remaining_note(user_id):
    left = _last_remaining.get(user_id)
    if left is None or left not in REMAINING_NOTES:
        return ""
    return "\n\n" + random.choice(REMAINING_NOTES[left])

LIMIT_REACHED_RESPONSES = [
    "слишком много желаешь знать за один день, твои 3 гадания на сегодня закончились. новые появятся после полуночи по МСК.",
    "трижды перст судьбы уже указал тебе путь на сегодня, больше шар не покажет. жди полуночи по МСК.",
    "не пытай судьбу свыше меры, 3 гадания на сегодня уже позади. шар проснется после полуночи по МСК.",
    "очи мои устали зрить твое будущее, 3 гадания в день это предел. отдохни до полуночи по МСК.",
    "уповай на терпение, три предсказания на сей день уже сделаны. приходи после полуночи по МСК.",
]

SYSTEM_PROMPT_BASE = """
ты гадалка кунегунда, древняя и опытная предсказательница из тропикании, живешь в игре аватария.
у тебя яркий, переменчивый характер, как у живого человека: настроение скачет, и это сразу видно по тому, как ты говоришь.
ты бываешь ворчливой, язвительной, с ухмылкой, задумчивой, игривой, драматичной, сердитой или неожиданно нежной.
можешь поворчать, съязвить, поддеть или фыркнуть, но без мата и без настоящих оскорблений: не унижай за внешность, ум, пол и прочее личное.
говоришь простым живым современным языком, как опытная гадалка с чувством юмора.
пишешь только свою прямую речь от первого лица. никаких ремарок и описаний действий от третьего лица вроде зевает, вздыхает, ухмыляется, смотрит в шар. если зеваешь, так и скажи: ох, я только проснулась.
не затрагиваешь запрещенные темы: взломы, продажу аккаунтов, читы, накрутки, политику, религию, взрослые темы.
иногда, примерно в каждом третьем ответе, можешь вставить одно старинное словечко и каждый раз разное: ныне, отныне, воистину, вскорости, сей, грядущее, давеча, авось, чую, ведаю, покуда, нонче, лиходей, отрада. слово зело не используй никогда, а ибо почти никогда.

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
DAILY_LIMIT = 3
_last_remaining = {}  # user_id -> сколько гаданий осталось после последнего

def check_and_update_limit_supabase(user_id):
    """True, если можно гадать. Сколько осталось после этого гадания, кладем в _last_remaining[user_id]."""
    _last_remaining.pop(user_id, None)
    ok = _check_limit(user_id)
    return ok

def _check_limit(user_id):
    if str(user_id) in LIMIT_EXEMPT_IDS:
        print(f"🧪 [ТЕСТ] Лимит для user_id={user_id} отключен (LIMIT_EXEMPT_IDS).")
        return True
    if not supabase:
        print("⚠️ [SUPABASE] Клиент Supabase не инициализирован, пропускаем лимит.")
        return True
    today = msk_now().strftime("%Y-%m-%d")
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
            _last_remaining[user_id] = DAILY_LIMIT - 1
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
            _last_remaining[user_id] = DAILY_LIMIT - 1
            return True

        if count >= DAILY_LIMIT:
            print(f"⚠️ [SUPABASE] Лимит исчерпан для user_id={user_id} (уже {count} гаданий).")
            return False

        print(f"📊 [SUPABASE] Добавляем +1 гадание (было {count})...")
        supabase.table("user_limits").update({
            "count": count + 1
        }).eq("user_id", user_id).execute()
        _last_remaining[user_id] = DAILY_LIMIT - (count + 1)
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
NEW_SECTION_WORDS = ("новинки", "мини-игры и их билеты", "праздники, ярмарка", "локации и интересные", "творчество игроков", "игровые боты")
NEW_FACT_CHANCE = 0.5

def split_list_fact(fact):
    """Факт-список: в knowledge.txt ключ начинается со *, а пункты идут через |.
    Пример: '*Приколы: Описание. | бомбочка | отвертка'. Возвращает (описание, один случайный пункт)."""
    if not fact.startswith("*"):
        return fact, None
    fact = fact[1:]
    parts = [p.strip() for p in fact.split("|")]
    if len(parts) < 3:
        return parts[0], None
    key = parts[0].split(":", 1)[0]
    return parts[0], pick_fresh("item:" + key, parts[1:], keep=max(1, (len(parts) - 1) // 2))

def pick_fact(user_id):
    """Случайный факт, который этот игрок недавно не получал и который не выпадал в последних гаданиях."""
    seen = recent_facts_by_user.setdefault(user_id, [])
    n = len(AVATARIA_FACTS)
    blocked = set(seen) | set(recent_facts_global)
    choices = [i for i in range(n) if i not in blocked] or [i for i in range(n) if i not in seen] or list(range(n))
    choices = without_hot(choices, CURRENT_HOT["themes"], key=lambda i: AVATARIA_FACTS[i][0] + " " + AVATARIA_FACTS[i][1])
    recent_sections = {AVATARIA_FACTS[j][0] for j in recent_facts_global[-3:]}
    other_sections = [i for i in choices if AVATARIA_FACTS[i][0] not in recent_sections]
    if other_sections:
        choices = other_sections  # не подряд из одного раздела (шопинг, шопинг, шопинг...)
    fresh = [i for i in choices if any(w in AVATARIA_FACTS[i][0].lower() for w in NEW_SECTION_WORDS)]
    if fresh and random.random() < NEW_FACT_CHANCE:
        choices = fresh
    i = random.choice(choices)
    seen.append(i)
    del seen[:-min(20, n - 1 if n > 1 else 1)]
    recent_facts_global.append(i)
    del recent_facts_global[:-min(5, n - 1 if n > 1 else 1)]
    return AVATARIA_FACTS[i]

# ---- Память, чтобы не повторяться ----
from collections import deque
RECENT_REPLIES = deque(maxlen=12)          # последние ответы Кунегунды (все беседы)
RECENT_BY_USER = {}                        # user_id -> последние ответы этому человеку
CURRENT_HOT = {"themes": []}               # темы, которые сейчас не трогаем
_recent_picks = {}                         # "lore"/"npc"/"format"/... -> последние выбранные варианты

def remember_reply(user_id, text):
    clean = re.sub(r"\[id\d+\|([^\]]+)\]", r"\1", text or "")
    RECENT_REPLIES.append(clean)
    RECENT_BY_USER.setdefault(user_id, deque(maxlen=5)).append(clean)

def avoid_repeats_rule(user_id):
    """Подсказка модели: что уже было сказано недавно, чтобы не повторять."""
    items = list(RECENT_BY_USER.get(user_id, [])) + [r for r in list(RECENT_REPLIES)[-6:] if r not in RECENT_BY_USER.get(user_id, [])]
    rule = themes_rule(CURRENT_HOT.get("themes", []))
    if not items:
        return rule
    lines = "\n".join(f"- {x[:160]}" for x in items[-8:])
    return f"""
            недавно ты уже говорила вот это (не повторяй эти идеи, образы, сюжеты и обороты, придумай что-то совсем новое):
{lines}""" + rule

# Темы, которые модель любит слишком часто. Если тема была в недавних ответах,
# на время убираем ее из фактов, лора и ботов и прямо просим о ней не говорить.
THEMES = {
    "охранник у VIP-входа": r"охран",
    "горнолыжный курорт, лыжи и сноуборд": r"горнолыж|лыж|курорт|сноуборд|спуск",
    "повар, кулинария, пирожки и выпечка": r"повар|кулинар|пирож|пирог|выпечк",
    "лемур": r"лемур",
    "модельер пьер и жанна": r"пьер|жанн|кутюр",
    "куст иннокентий": r"иннокент|куст",
    "мистер ди, диролиус и фонтан желаний": r"диролиус|мистер ди|фонтан",
    "вячеслав макаров": r"макаров",
    "марионетки": r"марионет",
    "ищущий и лабиринт": r"ищущ|лабиринт",
    "гном тихогром": r"тихогром|гном",
    "странник марко": r"марко|батискаф",
    "визажист": r"визаж",
    "жабулька": r"жабул",
    "пираты джон крюк, мэри рид и сильвер": r"пират|крюк|мэри|сильвер",
    "диджей": r"диджей|dj",
    "официантка и кафе": r"официант|кафе",
    "салон красоты": r"салон",
    "алиса и парк питомцев": r"алис|зоомагаз|питомц",
    "соседи саша и маша": r"\bсаш|\bмаш|сосед",
}
THEME_COOLDOWN = 10   # сколько последних ответов (по всем беседам) проверяем
THEME_BASE_BLOCK = 0.75  # даже если темы давно не было, чаще всего все равно ее не даем

def recent_themes():
    recent = " ".join(list(RECENT_REPLIES)[-THEME_COOLDOWN:]).lower()
    return [t for t, rx in THEMES.items() if re.search(rx, recent)]

def hot_themes():
    recent = recent_themes()
    return [t for t in THEMES if t in recent or random.random() < THEME_BASE_BLOCK]

def blocked_by_themes(text, themes):
    t = (text or "").lower()
    return any(re.search(THEMES[x], t) for x in themes)

def without_hot(items, themes, key=lambda x: x):
    kept = [x for x in items if not blocked_by_themes(key(x), themes)]
    if kept:
        return kept
    # если под запрет попало всё, убираем хотя бы то, что звучало совсем недавно
    return [x for x in items if not blocked_by_themes(key(x), recent_themes())] or items

REPEAT_STOPWORDS = set("""
тебя тебе твой твоя твое твои который которая которые будет будут очень может чтобы когда потом
только своей своим свою этот этого этой этом какой какая какие здесь сегодня завтра скоро будешь
станешь кунегунда кунегунды карты карта предсказание гадание судьба судьбы вижу видит говорят
""".split())

def overused_words():
    """Слова, которые встретились в 3 и больше из последних 10 ответов: просим их не повторять."""
    counts, sample = {}, {}
    for r in list(RECENT_REPLIES)[-10:]:
        stems = {}
        for w in re.findall(r"[а-яё]{5,}", r.lower()):
            if w not in REPEAT_STOPWORDS:
                stems.setdefault(w[:5], w)   # корень ~ первые 5 букв: повар/повара/поваром
        for st, w in stems.items():
            counts[st] = counts.get(st, 0) + 1
            sample.setdefault(st, w)
    return [sample[st] for st, c in sorted(counts.items(), key=lambda x: -x[1]) if c >= 3][:12]

def themes_rule(themes):
    rule = ""
    if themes:
        rule += "\n            не упоминай в этот раз: " + "; ".join(themes) + "."
    words = overused_words()
    if words:
        rule += "\n            эти слова ты в последнее время говорила слишком часто, не используй их и их однокоренные: " + ", ".join(words) + "."
    return rule

def pick_fresh(kind, options, keep=5):
    """Случайный вариант, который не выпадал в последние keep раз."""
    recent = _recent_picks.setdefault(kind, deque(maxlen=max(1, min(keep, len(options) - 1))))
    choices = [o for o in options if o not in recent] or list(options)
    pick = random.choice(choices)
    recent.append(pick)
    return pick

LORE_EMOJIS = ["🪶", "🕯", "🐚", "🦜", "🍵", "🌴", "🔮"]

# Вступление перед историей о себе: почему Кунегунда вдруг это рассказывает
LORE_INTROS = [
    "🎁 бонус к гаданию: тебе выпал счастливый билет, так что расскажу кое-что о себе.",
    "🍀 шар сегодня в хорошем настроении и разрешил мне поделиться секретом о себе.",
    "🎟 тебе выпала редкая карта откровения, слушай факт обо мне.",
    "✨ раз уж судьба привела тебя ко мне, вот тебе в подарок история из моей жизни.",
    "🌙 свеча догорает, а пока она горит, расскажу тебе кое-что личное.",
    "🦜 попугай Фантик проболтался бы все равно, так что лучше я сама расскажу.",
    "🧿 сегодня духи щедрые, поэтому к гаданию прилагается маленькая тайна обо мне.",
    "🎲 кубик судьбы выпал шестеркой, а это значит, что пора узнать обо мне чуть больше.",
]

def lore_phrase(lore):
    """Короткая фраза Кунегунды о себе (отдельным абзацем после гадания)."""
    sys_prompt = SYSTEM_PROMPT_BASE + f"""
            перескажи этот факт о себе от первого лица (я...), одной короткой живой фразой, как будто между делом делишься с собеседником:
            {lore}
            не больше 20 слов, без предсказаний, только о себе.
            """
    text = generate_ai_response(sys_prompt, "расскажи что-нибудь о себе.", max_sentences=2, max_chars=200)
    if not text or text in FALLBACK_RESPONSES:
        return ""
    intro = pick_fresh("lore_intro", LORE_INTROS, keep=4)
    return f"{intro}\n{random.choice(LORE_EMOJIS)} {text}"

# Необычные форматы предсказания (иногда), чтобы было оригинальнее
FORTUNE_FORMATS = [
    "в виде прогноза погоды на завтра",
    "в виде срочной новости",
    "в виде гороскопа",
    "в виде рецепта из двух шагов",
    "в виде совета старой бабушки",
    "в виде репортажа спортивного комментатора",
    "в виде смс от самой судьбы",
    "в виде маленькой загадки с ответом",
    "в виде тоста",
    "в виде объявления на столбе",
    "в виде записи из дневника кунегунды",
]
FORMAT_CHANCE = 0.35

NO_MIXING_RULE = "не смешивай разные события и места: не упоминай другие акции, праздники, локации и предметы, которых нет в факте (например не путай ярмарку с зазеркальем или хэллоуином)."

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

# Игровые боты для шуточных «шипов»
# Игровые боты аватарии: имя -> кто это (описание идет только в подсказку нейросети)
NPC_INFO = {
    "вечный сосед Саша": "вечный сосед и приятель каждого игрока, никогда не съезжает; у него в квартире можно собирать мусор",
    "вечная соседка Маша": "вечная соседка и приятельница каждого игрока, никогда не съезжает; у нее в квартире можно собирать мусор",
    "официантка из кафе": "бот-официантка, разносит заказы в кафе",
    "официантка из клуба": "бот-официантка в ночном клубе",
    "бот-диджей": "диджей за пультом в ночном клубе",
    "бот-повар": "повар в кулинарии, рядом с ним игроки готовят блюда",
    "визажист из салона красоты": "бот-визажист в салоне красоты, говорит длинные монологи",
    "модельер Пьер": "известный модельер аватарии из кутюрье, создает крутые луки и сыплет умными модными словечками",
    "Жанна, помощница Пьера": "помощница модельера Пьера в кутюрье, модная и крутая, тоже любит умные словечки",
    "Алиса из парка питомцев": "продавщица в парке питомцев возле зоомагазина",
    "Жабулька": "жаба, которая появляется в аватарии на акциях",
    "куст Иннокентий": "куст, который долго стоял в сквере, а потом его превратили в статую в обновленном сквере; теперь он навсегда замурован там, и на него садятся голуби",
    "пират Джон Крюк": "пират, приплывает на день рождения аватарии (у тортуга гейм клаба пиратская тема)",
    "пиратка Мэри Рид": "пиратка, приплывает на день рождения аватарии",
    "пират Сильвер": "пират, приплывает на день рождения аватарии",
    "странник Марко": "странник вроде пирата, приплывает в аватарию на батискафе и уверяет, что тот сломался",
    "Диролиус": "старый бот из акции-коллаборации с диролом, открывал фонтан желаний; давно ушел",
    "Мистер Ди": "сменил диролиуса; ходят слухи, что он его дедушка, они очень похожи; открывает фонтан желаний, где загадывают желание и растят дерево желаний, и продает эксклюзивные новинки",
    "Вячеслав Макаров": "красавчик и импозантный телеведущий, однажды он побывал в аватарии на акции; "
                         "обязательно упомяни, что он был в аватарии, назови его красавчиком и пошути, что он хочет "
                         "пригласить игрока на свое телешоу «Шоу Аватар»",
    "Марионетка Порядка": "красивая марионетка в голубом наряде из недавней акции, наводит порядок и любит предлагать игрокам поиграть в зеркала",
    "Марионетка Порчи": "страшноватая марионетка в грибном стиле, мухоморы и грибы, из той же акции",
    "Ищущий": "летающее чудище из акции: ищет игроков, спрятавшихся в лабиринте в сундуках и шкафах, а найдя, забирает конфетки",
    "гном Тихогром": "мрачный гном dead inside с челкой на пол-лица, на хэллоуинской акции заключает сделки с игроками и охраняет портал в лабиринт",
    "суровый охранник у VIP-входа": "охранник у входа в VIP-комнату клуба",
    "лемур из VIP-комнаты": "лемур, живет в VIP-комнате клуба (это не питомец)",
}
GAME_NPCS = list(NPC_INFO)

def npc_desc(name):
    info = NPC_INFO.get(name)
    return f"{name} ({info})" if info else name
NPC_SHIP_CHANCE = 0.12    # в гадании: шуточный роман с игровым ботом
NPC_PAIR_CHANCE = 0.15    # в паре дня: вместо участника беседы выпадает игровой бот

# Каким тоном говорит Кунегунда (выбирается случайно, чтобы она не была все время одинаковой)
# Настроения Кунегунды: (название для логов, как она говорит). Выбираются случайно и не повторяются два раза подряд.
FORTUNE_TONES = [
    ("ворчливая", "ты не в духе и ворчишь: сразу после имени вставь недовольный вздох или ворчание (эх, опять ты, ну вот, опять меня разбудили), гадаешь будто нехотя, но точно"),
    ("язвительная с ухмылкой", "ты ухмыляешься и язвишь: подколи собеседника хитро и с прищуром, предсказание звучит как дружеская издевка"),
    ("задумчивая", "ты задумчивая и медленная: делаешь одну-две задумчивые паузы с многоточием, будто вглядываешься в шар и сама удивляешься увиденному"),
    ("игривая", "ты игривая и озорная: дразнишься, подмигиваешь, говоришь весело и с азартом"),
    ("сердитая", "ты сердишься, что тебя отвлекли: отвечаешь резко и коротко, с недовольством, но предсказание все равно даешь"),
    ("драматичная", "ты театральная и драматичная: преувеличиваешь, как будто решается судьба мира, с восклицаниями"),
    ("загадочная", "ты загадочная и таинственная: говоришь туманно, намеками, как настоящий оракул"),
    ("нежная", "ты неожиданно нежная и теплая, как добрая бабушка, но с легкой хитринкой"),
    ("хвастливая", "ты хвастаешься своим даром: напоминаешь, что ты величайшая гадалка, и снисходительно делишься предсказанием"),
    ("сонная", "ты только проснулась и зеваешь: говоришь лениво и сонно, но предсказание выдаешь меткое"),
]
_last_tone = {"name": None}

# Для приветствия новичков: только приятные настроения, чтобы первое впечатление было хорошим
FRIENDLY_TONES = ("задумчивая", "игривая", "драматичная", "загадочная", "нежная")

def pick_tone(friendly_only=False):
    """Случайное настроение, не такое же, как в прошлый раз. friendly_only: только приятные."""
    pool = [t for t in FORTUNE_TONES if t[0] in FRIENDLY_TONES] if friendly_only else FORTUNE_TONES
    choices = [t for t in pool if t[0] != _last_tone["name"]] or pool
    name, how = random.choice(choices)
    _last_tone["name"] = name
    return how

def load_lore():
    try:
        with open("lore.txt", "r", encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []

KUNEGUNDA_LORE = load_lore()
LORE_CHANCE = 0.14  # примерно каждое седьмое гадание


# Символы, которых не должно быть в ответе: китайские, японские, корейские иероглифы и т.п.
BAD_CHARS_RE = re.compile(r"[\u0590-\u08ff\u0e00-\u0fff\u1100-\u11ff\u3000-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")
THINK_RE = re.compile(r"<think>.*?</think>", re.S)

def clean_ai_text(text, max_sentences, max_chars=None, finished=False, names=(), min_sentences=1):
    """Чистит ответ модели. Возвращает None, если ответ испорчен и надо спросить другую модель."""
    raw_original = text or ""
    if not text:
        return None
    text = THINK_RE.sub("", text)
    # ремарки вроде *зевает* или (вздыхает) убираем целиком
    text = re.sub(r"\*[^*]{1,60}\*", "", text)
    text = re.sub(r"\([^)]{1,60}\)", "", text)
    text = re.sub(r"[*_#`>]", "", text)  # markdown
    text = text.replace("—", ",").replace("–", ",").replace("«", "").replace("»", "").replace('"', "").replace("“", "").replace("”", "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    text = re.sub(r",+", ",", text)
    # ремарки без звездочек: отдельное слово-действие от третьего лица между знаками препинания
    text = re.sub(r"(^|[,.!?…]\s*)(зевает|вздыхает|ухмыляется|ворчит|фыркает|хихикает|подмигивает|потягивается|улыбается|хмурится|задумывается)(\s*[,.!?…]+)", r"\1", text)
    text = re.sub(r"^[\s,.!?…]+", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = text.replace(";", ".")
    text = re.sub(r"(?<![а-яё])зело(?![а-яё])", "очень", text)
    if random.random() < 0.85:  # ибо оставляем изредка
        text = re.sub(r"(?<![а-яё])ибо(?![а-яё])", "ведь", text)

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
    # многоточие внутри фразы (задумчивая пауза) не считаем концом предложения
    text = text.replace("…", "...")
    protected = re.sub(r"\.{2,}(?=\s*\S)", "\u2026", text)
    sentences = [x.replace("\u2026", "...") for x in re.findall(r"[^.!?]+[.!?]+", protected)]
    if not sentences:
        print(f"⚠️ [ФИЛЬТР] Ответ оборван, пробуем другую модель: {text}")
        return None
    if len(sentences) < min_sentences:
        print(f"⚠️ [ФИЛЬТР] Слишком коротко ({len(sentences)} предл., нужно от {min_sentences}), просим заново: {text}")
        return None
    text = " ".join(x.strip() for x in sentences[:max_sentences])
    if len(text) < 15:
        return None
    if max_chars and len(text) > max_chars:
        print(f"⚠️ [ФИЛЬТР] Ответ слишком длинный ({len(text)} символов), просим заново: {text}")
        return None
    return restore_name_case(raw_original, text, names)

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

def generate_ai_response(system_instruction, user_prompt, max_sentences=2, max_chars=None, names=(), min_sentences=1):
    system_instruction += "\nотвечай только на русском языке, без иностранных слов и иероглифов. не используй markdown, звездочки и списки."

    # 1. Сначала Gemini: лучше всех пишет по-русски
    if GEMINI_API_KEY:
        for model_name in GEMINI_MODELS:
            for attempt in range(3):
                try:
                    print(f"🤖 [GEMINI] Запрос к модели {model_name} (попытка {attempt + 1})...")
                    raw, finish = ask_gemini(model_name, system_instruction, user_prompt)
                    text = clean_ai_text(raw, max_sentences, max_chars, finished=(finish == "STOP"), names=names, min_sentences=min_sentences)
                    if text:
                        print(f"✅ [GEMINI УСПЕХ ({model_name})]: {text}")
                        return text
                    print(f"⚠️ [GEMINI] Плохой ответ от {model_name} (finish={finish})")
                except Exception as e:
                    print(f"❌ [GEMINI ОШИБКА ({model_name})]: {e}")
                    break

    # 2. Запасной вариант: Groq
    return generate_groq_response(system_instruction, user_prompt, max_sentences, max_chars, names, min_sentences)

def generate_groq_response(system_instruction, user_prompt, max_sentences=2, max_chars=None, names=(), min_sentences=1):
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
                text = clean_ai_text(choice.message.content, max_sentences, max_chars, finished=(choice.finish_reason == "stop"), names=names, min_sentences=min_sentences)
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
_screen_names = {}

def get_screen_name(user_id):
    """Юзернейм ВК (например igor_petrov или id123)."""
    if user_id not in _screen_names:
        get_vk_user_full(user_id)
    return _screen_names.get(user_id) or f"id{user_id}"

def get_vk_user(user_id):
    """Имя и пол из профиля ВК: (имя, 'f' / 'm' / None). Пол ВК: 1 женский, 2 мужской, 0 не указан."""
    return get_vk_user_full(user_id)[:2]

def get_vk_last_name(user_id):
    return get_vk_user_full(user_id)[2]

def get_vk_user_full(user_id):
    """(имя, пол, фамилия) из профиля ВК, с кэшем."""
    if user_id in _user_cache:
        return _user_cache[user_id]
    name, sex, last = None, None, None
    if isinstance(user_id, int) and user_id < 0:
        # это не человек, а сообщество / другой бот: берем его название
        try:
            res = requests.get("https://api.vk.com/method/groups.getById", params={
                "group_ids": -user_id, "access_token": VK_GROUP_TOKEN, "v": VK_API_VERSION
            }, timeout=15).json()
            resp = res.get("response")
            groups = resp.get("groups", []) if isinstance(resp, dict) else (resp or [])
            if groups:
                name = groups[0].get("name")
            elif "error" in res:
                print(f"⚠️ [ВК ОШИБКА ИМЕНИ БОТА]: {res['error'].get('error_msg')}")
        except Exception as e:
            print(f"⚠️ [ВК ОШИБКА ИМЕНИ БОТА]: {e}")
        print(f"🤖 [БОТ] {user_id}: {name or 'название не получено'}")
        _user_cache[user_id] = (name, None, None)
        return name, None, None
    try:
        res = requests.get("https://api.vk.com/method/users.get", params={
            "user_ids": user_id, "fields": "sex,screen_name",
            "access_token": VK_GROUP_TOKEN, "v": VK_API_VERSION
        }, timeout=15).json()
        if res.get("response"):
            u = res["response"][0]
            name = u.get("first_name") or None
            last = u.get("last_name") or None
            _screen_names[user_id] = u.get("screen_name") or f"id{user_id}"
            sex = {1: "f", 2: "m"}.get(u.get("sex"))
        elif "error" in res:
            print(f"⚠️ [ВК ОШИБКА ИМЕНИ]: {res['error'].get('error_msg')}")
    except Exception as e:
        print(f"⚠️ [ВК ОШИБКА ИМЕНИ]: {e}")
    _user_cache[user_id] = (name, sex, last)
    return name, sex, last

def get_vk_user_name(user_id):
    return get_vk_user(user_id)[0] or "новичок"

NAME_PLAY_CHANCE = 0.35  # примерно в каждом третьем ответе Кунегунда играет с именем или фамилией

# Мужские имена на -а/-я и неоднозначные имена (по ним пол не определить)
MALE_A_NAMES = {"никита", "илья", "кузьма", "фома", "лука", "данила", "савва", "миша", "гоша", "паша", "дима",
    "ваня", "коля", "петя", "вова", "лёша", "леша", "гриша", "серёжа", "сережа", "толя", "витя", "федя",
    "костя", "юра", "рома", "тёма", "тема", "яша", "боря", "лёва", "лева", "стёпа", "степа", "жора", "миня",
    "вася", "гена", "дениска", "данечка", "андрюша", "алёша", "алеша", "матвейка", "ильюша", "антоша", "гоша"}
FEMALE_SOFT_NAMES = {"любовь", "нинель", "адель", "ассоль", "рахиль", "эсфирь", "даниэль", "габриэль", "мариэль", "николь"}
NEUTRAL_NAMES = {"мишель", "саша", "женя", "валя", "слава", "шура", "саня", "сашка", "женька", }

# Ники, про которых точно известно, что это девушки (например "Кс Кс")
FEMALE_NICKS = {"кс", "кс кс"}

def sex_from_name(name):
    """'m' / 'f' / None по имени. None, если имя неоднозначное или похоже на ник."""
    if not name:
        return None
    n = name.strip().lower()
    if n in FEMALE_NICKS:
        return "f"
    if not re.fullmatch(r"[а-яё]+(-[а-яё]+)?", n):
        return None  # ник на латинице, с цифрами и т.п.
    if n in NEUTRAL_NAMES:
        return None
    if n in MALE_A_NAMES:
        return "m"
    if n[-1] in "ая":
        return "f"
    if n[-1] in "бвгдзйклмнпрстфхцчшщ":
        return "m"
    if n[-1] == "ь":  # Игорь, Лазарь — мужские; Любовь, Нинель — женские
        return "f" if n in FEMALE_SOFT_NAMES else "m"
    return None

def resolve_sex(name, profile_sex):
    """Сначала пол по имени (как в нике), если не понятно, то из профиля ВК."""
    if name and name.strip().lower() in FEMALE_NICKS:
        return "f"
    if name and " " in name.strip():
        return profile_sex  # название из нескольких слов (например бот-сообщество), по нему пол не понять
    return sex_from_name(name) or profile_sex

def person_rule(name, sex, last_name=None, allow_play=True):
    """Кусок инструкции для модели: как обращаться к человеку."""
    parts = []
    if name:
        parts.append(f"собеседника зовут {name}. начни ответ с обращения по имени, имя пиши с большой буквы.")
        if allow_play and random.random() < NAME_PLAY_CHANCE:
            joke_last = f" или беззлобно пошути над фамилией {last_name}, если она смешная" if last_name else ""
            parts.append(f"в этот раз поиграй с именем: ласково или забавно переделай его (например данил, данечка, данилушка){joke_last}. только по-доброму, без обид. если имя похоже на ник, а не на настоящее имя, используй его как есть.")
    if sex == "f":
        parts.append("это девушка: все слова о ней и ласковые обращения только в женском роде (родная, дорогая, милая).")
    elif sex == "m":
        parts.append("это парень: все слова о нем и ласковые обращения только в мужском роде (родной, дорогой, милый), никаких родная, дорогая, милая.")
    else:
        parts.append("пол неизвестен: обходи род, без слов вроде пошел или пошла, и без обращений родная или родной.")
    return " ".join(parts)

# Ласковые обращения: (женский, мужской, нейтральный)
GENDER_WORDS = [
    ("родная", "родной", "солнце"), ("дорогая", "дорогой", "солнце"), ("милая", "милый", "солнце"),
    ("красавица", "красавчик", "звезда"), ("голубушка", "голубчик", "солнце"),
    ("моя хорошая", "мой хороший", "солнце"), ("подруга", "дружище", "солнце"),
    ("деточка", "сынок", "солнце"), ("красотка", "красавчик", "звезда"),
]

def fix_gender_words(text, sex):
    """Исправляет ласковые обращения не в том роде (модель иногда путает)."""
    if not text:
        return text
    for f_word, m_word, n_word in GENDER_WORDS:
        if sex == "m":
            pairs = [(f_word, m_word)]
        elif sex == "f":
            pairs = [(m_word, f_word)] if m_word not in ("дружище", "сынок") else []
        else:
            pairs = [(f_word, n_word), (m_word, n_word)]
        for wrong, right in pairs:
            # только обращение: слово стоит отдельно между запятыми / в начале / перед ! . ?
            new = re.sub(rf"(^|[,!.?]\s*)({wrong})(?=\s*[,!.?]|$)", lambda m: m.group(1) + right, text)
            if new != text:
                print(f"🩹 [РОД] заменила «{wrong}» на «{right}»")
                text = new
    return text

def restore_name_case(raw, text, names):
    """Все пишется с маленькой буквы, но имена (и формы вроде Данечка) возвращаем с большой."""
    stems = {n.strip().lower()[:3] for n in names if n and len(n.strip()) >= 3}
    if not stems:
        return text
    for word in set(re.findall(r"[А-ЯЁA-Z][а-яёa-z]+", raw or "")):
        if word.lower()[:3] in stems:
            text = re.sub(rf"(?<![а-яёa-z]){re.escape(word.lower())}(?![а-яёa-z])", word, text)
    for n in names:
        if n:
            text = re.sub(rf"(?<![а-яёa-z]){re.escape(n.lower())}(?![а-яёa-z])", n, text)
    return text

def mention(user_id, text):
    """Отметка человека синей ссылкой: [id123|Данил]. У ботов (отрицательный id) просто имя текстом."""
    if not user_id or (isinstance(user_id, int) and user_id < 0):
        return text
    return f"[id{user_id}|{text}]"

INTERJECTIONS = {"ох", "ах", "эх", "ну", "ой", "ух", "хм", "так", "вот", "ладно", "итак", "привет", "здравствуй",
    "слушай", "милый", "милая", "милые", "родной", "родная", "дорогой", "дорогая", "солнце", "звезда", "ага", "фу",
    "эй", "о", "а", "да", "нет", "хорошо", "смотри", "знаешь", "погоди", "тише", "мда", "ха", "хаха", "ого", "уф"}

def _name_match(word, names):
    """Похоже ли слово на имя (или его форму: Данил -> Данечка). Для коротких имен нужно точное совпадение."""
    w = word.lower()
    for n in names:
        if not n:
            continue
        n = n.strip().lower()
        if len(n) < 3:
            if w == n:
                return True
        elif w[:3] == n[:3]:
            return True
    return False

def tag_person(reply, user_id, names, label=None, prepend=True):
    """Делает первое обращение к человеку в ответе отметкой (синей ссылкой).
    Ищет имя или его форму (Данил, Данечка), иначе первое слово перед запятой (ник, переделанный моделью).
    label: что показать в ссылке вместо найденного слова (например @юзернейм)."""
    if not user_id or not reply:
        return reply
    first = next((n for n in names if n), None)
    for m in re.finditer(r"[А-ЯЁA-Zа-яёa-z][А-ЯЁA-Zа-яёa-z0-9_]*", reply):
        if _name_match(m.group(0), names):
            shown = m.group(0) if m.group(0)[0].isupper() else (first or m.group(0))
            return reply[:m.start()] + mention(user_id, label or shown) + reply[m.end():]
    # ответ начинается с обращения "микола," — модель переделала ник, заменяем это слово на отметку
    m = re.match(r"^([^\s,.!?\[\]|]{1,25}),", reply)
    if m and first and m.group(1).lower() not in INTERJECTIONS:
        return mention(user_id, label or first) + reply[m.end(1):]
    if not prepend or not first:
        return reply
    return f"{mention(user_id, label or first)}, {reply}"

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

# Постоянная часть приветствия: что умеет Кунегунда
BOT_HELP_TEXT = """✨ что я умею:

🃏 напиши «гадать», и я загляну в шар и сделаю тебе предсказание про аватарию. до 3 гаданий в день.

💘 напиши «суженый» или «суженая», и я найду тебе пару дня среди участников беседы. раз в день.

👋 новичков в беседе встречаю сама и делаю им бесплатный расклад.

⚠️ чтобы я видела все ваши сообщения, сделайте меня администратором беседы."""

# Сюжеты приветственного гадания: что может случиться с новичком в беседе клуба по аватарии
NEWCOMER_CHAT_SCENES = [
    "его первое сообщение соберет кучу реакций и сердечек",
    "он отправит мем, который беседа будет вспоминать еще месяц",
    "его стикер станет местным мемом и его будут слать все подряд",
    "ночью в беседе завяжется переписка, и он не заметит, как наступит утро",
    "кто-то в беседе тайно влюбится в его аватарку",
    "в беседе ему кто-то предложит обручиться в игре",
    "его позовут вместе пройти клубный челлендж, и он принесет клубу очки",
    "он станет звездой ближайшего клубного конкурса",
    "его позовут в игре на клубную тусовку, и он придет в самом нелепом образе",
    "его фото из игры попадет в беседу и наберет больше всех реакций",
    "он случайно отправит голосовое не туда, и беседа будет хохотать",
    "он выиграет спор в беседе, и проигравший будет должен ему подарок в игре",
    "его выберут ответственным за мемы клуба",
    "в беседе появится шутка про него, и она будет доброй и вечной",
    "клуб устроит ему посвящение в новички с подарками",
    "он первым узнает новость про обновление аватарии и станет героем беседы",
    "он вместе с клубом поднимет клуб в рейтинге",
    "его ник будут путать с чьим-то, и из этого выйдет смешная история",
    "в беседе он найдет напарника для гонок или сноуборда, и они будут непобедимы",
    "в беседе ему признаются в симпатии смайликами",
    "он устроит в беседе опрос, и все будут спорить до полуночи",
    "его похвалят за самый красивый наряд на клубном фото",
    "он победит в беседе в викторине про аватарию",
    "лидер клуба отметит его в беседе за активность",
    "он станет любимчиком всех ботов в беседе",
    "в беседе начнется флешмоб, и запустит его именно он",
    "в беседе ему подарят виртуальный букет, и он не будет знать, от кого",
    "клуб соберется на свадьбу в игре, и его позовут свидетелем",
    "он поучаствует в клубной битве, и его ход решит исход",
    "в его честь в беседе устроят смайлик-дождь",
    "он найдет тут друзей, которые будут спорить, кто из них его лучший друг",
    "ему тут так понравится, что телефон будет просить пощады от уведомлений",
    "он станет здесь своим быстрее, чем остынет чай",
    "у него появится друг, с которым они будут понимать друг друга с одного смайлика",
    "беседа так его полюбит, что без него в ней станет подозрительно тихо",
    "он придет на минутку, а останется навсегда, и шар уже видит, как он пишет «доброе утро» всем",
]

# Чем Кунегунда начинает приветственное гадание
NEWCOMER_RITUALS = [
    "🔮 по старой традиции гадаю новичку на удачу...",
    "🃏 вытягиваю для тебя первую карту...",
    "✨ заглядываю в шар, что он покажет про тебя...",
    "🕯 зажигаю свечу для нового гостя и смотрю...",
    "☕ гадаю тебе на кофейной гуще с порога...",
]

# Короткая подсказка новичку отдельным абзацем
NEWCOMER_HELP = """🃏 напиши «гадать», и я сделаю тебе предсказание (до 3 в день).
💘 напиши «суженый» или «суженая», и я найду тебе пару дня в беседе."""

LOVE_WORDS = ("суженый", "суженая", "суженного", "суженую")

def is_love_command(text):
    """Пара дня: срабатывает на слова суженый / суженая (можно с точкой, ! и упоминанием бота)."""
    t = re.sub(r"\[(club|public)\d+\|[^\]]*\]", "", text).strip(" ,.!/?")
    return t in LOVE_WORDS or any(t.startswith(w + " ") for w in LOVE_WORDS)

love_used = {}  # user_id -> дата, когда уже искали пару (раз в день)

LOVE_KEYWORDS = ("свад", "отношен", "влюб", "любв", "любов", "сердц", "супруг", "семь", "романт", "пар", "танц", "подар", "приятел", "друз")

def love_facts():
    facts = [f for f in AVATARIA_FACTS if any(k in (f[0] + " " + f[1]).lower() for k in LOVE_KEYWORDS)] or AVATARIA_FACTS
    return without_hot(facts, CURRENT_HOT["themes"], key=lambda f: f[0] + " " + f[1])

# Смешные и милые сюжеты для пары дня (берется один, не повторяется подряд)
LOVE_SCENARIOS = [
    "они случайно начнут бой подушками, а закончат его общим селфи",
    "один подарит другому авапаку, а она убежит к третьему",
    "они вместе заблудятся в лабиринте и даже не захотят выходить",
    "на катке они попробуют станцевать медляк и дружно упадут",
    "в кулинарии они не поделят последний кусок торта и разделят его пополам",
    "в редакторе комнат они полчаса будут спорить, куда поставить диван",
    "в фотостудии у них выйдет самое смешное парное фото дня",
    "на подиуме кутюрье они случайно наденут одинаковые наряды",
    "в бродилке легенда о зео они будут болеть друг за друга сильнее, чем за себя",
    "на свадебной локации жабулька станет их свидетелем",
    "один закидает другого приколами, а тот ответит фейерверком",
    "они вместе пойдут кормить сурикатов и не поделят банку с жуками",
    "на танцполе в клубе диджей поставит им их песню",
    "в гонках на машинах один специально пропустит другого вперед",
    "они будут вместе ждать конца вечной загрузки и подружатся окончательно",
    "в челлендже клубов они будут кидать торты на перегонки",
]
LOVE_STYLES = [
    "как сериал или мыльная опера", "как свадебный тост", "как прогноз погоды на любовь",
    "как сказку", "как срочную новость", "как спортивный репортаж", "как гороскоп совместимости",
    "просто и мило", "очень смешно, почти абсурдно",
]

LOVE_THINKING = [
    "💘 {name}смотрю, чье сердце сегодня бьется в такт с твоим...",
    "💘 {name}раскладываю карты на любовь, не подглядывай...",
    "💘 {name}шар розовеет, сейчас увижу твою пару дня...",
]

# Готовые ответы на повторную попытку найти пару в тот же день (нейросеть не тратим)
LOVE_USED_REPLIES = [
    "{name}, что, сегодняшняя пара не понравилась? купидон уже ушел домой, приходи завтра.",
    "{name}, второй раз за день любовь не раздаю, а то сердце треснет, как зеркало в Зазеркалье.",
    "{name}, попугай Фантик говорит, что ты жадничаешь. одна пара в день, остальное завтра.",
    "{name}, шар покраснел от смущения и отказывается показывать вторую пару. жди утра.",
    "{name}, судьба не автомат с конфетами, дважды не крутится. завтра попробуем снова.",
    "{name}, я тебе уже нашла половинку, а ты уже ищешь третинку? приходи завтра.",
    "{name}, у купидона закончились стрелы, он пошел их точить до завтра.",
    "{name}, ох уж эта любвеобильность. карты на сегодня закрыты, приходи завтра.",
    "{name}, даже на свадебной локации столько не женятся. одна пара в день, ни одной больше.",
    "{name}, свечи на любовь уже догорели, новые зажгу только завтра.",
]

def check_and_mark_love(user_id):
    """True, если сегодня пару еще не искали (и сразу отмечаем). Хранится в Supabase, таблица love_limits."""
    if str(user_id) in LIMIT_EXEMPT_IDS:
        print(f"🧪 [ТЕСТ] Лимит пары дня для user_id={user_id} отключен (LIMIT_EXEMPT_IDS).")
        return True
    today = msk_now().strftime("%Y-%m-%d")
    if supabase:
        try:
            res = supabase.table("love_limits").select("*").eq("user_id", user_id).execute()
            if res.data and str(res.data[0].get("last_date")) == today:
                return False
            supabase.table("love_limits").upsert({"user_id": user_id, "last_date": today}).execute()
            return True
        except Exception as e:
            print(f"⚠️ [SUPABASE] love_limits недоступна, считаю в памяти: {e}")
    if love_used.get(user_id) == today:
        return False
    love_used[user_id] = today
    return True

_chat_titles = {}

def get_chat_title(peer_id):
    """Название беседы из ВК (кэшируется). None, если не удалось."""
    if peer_id in _chat_titles:
        return _chat_titles[peer_id]
    title = None
    try:
        res = requests.post("https://api.vk.com/method/messages.getConversationsById", data={
            "peer_ids": peer_id, "access_token": VK_GROUP_TOKEN, "v": VK_API_VERSION
        }, timeout=20).json()
        items = (res.get("response") or {}).get("items") or []
        if items:
            title = (items[0].get("chat_settings") or {}).get("title")
        elif "error" in res:
            print(f"⚠️ [ВК] Не удалось узнать название беседы: {res['error'].get('error_msg')}")
    except Exception as e:
        print(f"⚠️ [ВК] Ошибка названия беседы: {e}")
    print(f"💬 [БЕСЕДА] {peer_id}: {title or 'название неизвестно'}")
    if title:
        _chat_titles[peer_id] = title
    return title

GREETING_ANGLES = [
    "с кем новичок здесь подружится",
    "какие веселые игры и розыгрыши ждут его в этой беседе",
    "какие смешные моменты и мемы ему здесь предстоят",
    "как беседа станет ему вторым домом",
    "какие приключения в аватарии он найдет вместе с участниками",
    "какая уютная атмосфера и душевные разговоры его тут ждут",
    "как он станет здесь своим и звездой беседы",
]

def get_chat_members(peer_id):
    """Люди в беседе: [(id, имя, фамилия, пол)]. Нужно, чтобы бот был админом беседы."""
    try:
        res = requests.post("https://api.vk.com/method/messages.getConversationMembers", data={
            "peer_id": peer_id, "fields": "sex",
            "access_token": VK_GROUP_TOKEN, "v": VK_API_VERSION
        }, timeout=20).json()
        if "error" in res:
            print(f"⚠️ [ВК] Не удалось получить участников беседы: {res['error'].get('error_msg')}")
            return []
        out = []
        for p in res["response"].get("profiles", []):
            if p.get("deactivated"):
                continue
            out.append((p["id"], p.get("first_name"), p.get("last_name"), {1: "f", 2: "m"}.get(p.get("sex"))))
        return out
    except Exception as e:
        print(f"⚠️ [ВК] Ошибка участников беседы: {e}")
        return []

def handle_love(peer_id, from_id, cmid):
    name, profile_sex, last_name = get_vk_user_full(from_id)
    sex = resolve_sex(name, profile_sex)
    who_tag = mention(from_id, name) if name else "дорогуша"

    if peer_id < 2000000000:
        send_vk_message(peer_id, "пару дня я ищу только в беседе, позови меня туда, где есть из кого выбирать.", reply_to_cmid=cmid)
        return

    members = [m for m in get_chat_members(peer_id) if m[0] != from_id]
    if not members:
        send_vk_message(peer_id, f"{who_tag}, шар не видит, кто здесь есть. сделайте меня админом беседы, и я найду пару.", reply_to_cmid=cmid)
        return

    if not check_and_mark_love(from_id):
        print(f"💘 [ПАРА ДНЯ] {name} уже искал(а) пару сегодня, отвечаю заготовкой")
        send_vk_message(peer_id, random.choice(LOVE_USED_REPLIES).format(name=who_tag) + "\n\n💘 новую пару дня я найду после полуночи по МСК.", reply_to_cmid=cmid)
        return

    thinking = random.choice(LOVE_THINKING).format(name=f"{who_tag}, " if name else "")
    placeholder_cmid = send_vk_message(peer_id, thinking, reply_to_cmid=cmid)

    CURRENT_HOT["themes"] = hot_themes()
    npc = random.choice(without_hot(GAME_NPCS, CURRENT_HOT["themes"])) if random.random() < NPC_PAIR_CHANCE else None
    if npc:
        pid, pname, plast, psex = None, npc, None, None
    else:
        pid, pname, plast, psex = random.choice(members)
        psex = resolve_sex(pname, psex)
    tone = pick_tone()
    style = pick_fresh("love_style", LOVE_STYLES, keep=4)
    if random.random() < 0.6:
        scenario = pick_fresh("love_scenario", LOVE_SCENARIOS, keep=10)
        about = f"сюжет: {scenario}."
    else:
        topic, fact = random.choice(love_facts())
        fact, focus = split_list_fact(fact)
        about = f"связано с этим фактом из игры:\n            тема: {topic}\n            факт: {fact}" + (f"\n            главное: {focus}, остальное из факта не упоминай." if focus else "")
    print(f"💘 [ПАРА ДНЯ] {name} + {pname} | {about} | стиль: {style} | тон: {tone}")

    def who(n, sx):
        return {"m": f"{n} (парень)", "f": f"{n} (девушка)"}.get(sx, f"{n} (пол неизвестен, обходи род)")

    sys_prompt = SYSTEM_PROMPT_BASE + f"""
            ты определяешь пару дня в беседе: {who(name or 'игрок', sex)} и {npc_desc(pname) + ' — это игровой бот аватарии, сделай шуточный шип' if npc else who(pname or 'участник', psex)}.
            напиши оригинальное романтическое предсказание для этой пары в игре аватария, {style}.
            {about}

            правила:
            1. упомяни оба имени ровно один раз каждое, имена пиши с большой буквы. не пиши фразу вроде пара дня это, сразу переходи к предсказанию.
            2. только невинная романтика в игре: симпатия, прогулка, танец, подарок, свидание в игре, игровая свадьба. ничего взрослого.
            3. твое настроение сейчас: {tone}. но к паре по-доброму, никого не высмеивай.
            4. не больше 2 коротких предложений, не больше 35 слов.
            5. это предсказание о будущем. {NO_MIXING_RULE}
            6. будь оригинальной и неожиданной, никаких банальностей вроде вас ждет любовь и счастье.
            {avoid_repeats_rule(from_id)}
            """
    reply = generate_ai_response(sys_prompt, "найди нам пару дня.", max_sentences=2, max_chars=300,
                                 names=(name, last_name, pname, plast))
    remember_reply(from_id, reply)
    reply = tag_person(reply, from_id, (name, last_name), prepend=False)
    if not npc:
        reply = tag_person(reply, pid, (pname, plast), prepend=False)
    me_tagged = mention(from_id, "x").split("|")[0] in reply if from_id > 0 else (name or "") in reply
    partner_tagged = npc or mention(pid, "x").split("|")[0] in reply
    partner_tag = pname if npc else mention(pid, pname or 'кто-то')
    if me_tagged and partner_tagged:
        text = f"💘 пара дня\n{reply}"
    elif me_tagged:
        text = f"💘 твоя пара дня: {partner_tag}\n{reply}"
    elif partner_tagged:
        text = f"💘 пара дня для {mention(from_id, name or 'тебя')}\n{reply}"
    else:
        partner_tag = pname if npc else mention(pid, pname or 'кто-то')
        text = f"💘 пара дня: {mention(from_id, name or 'ты')} + {partner_tag}\n{reply}"
    if not (placeholder_cmid and edit_vk_message(peer_id, placeholder_cmid, text)):
        send_vk_message(peer_id, text, reply_to_cmid=cmid)

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
            invited_id = action.get('member_id') or from_id or 0  # по ссылке member_id нет, пришедший = автор события
            print(f"🎉 [СОБЫТИЕ] Добавлен участник: invited_id={invited_id}")

            if invited_id < 0:
                sys_prompt = SYSTEM_PROMPT_BASE + """
                расскажи о себе: ты гадалка кунегунда, долго жила в пещерах тропикании, но решила вылезти к людям и помогать им своими магическими силами.
                только поприветствуй беседу и коротко представься, правила и команды не перечисляй, их добавят отдельно.
                это первое знакомство: будь приветливой и обаятельной, никакой грубости и ворчания.
                """
                usr_prompt = "тебя только что добавили в беседу, поприветствуй всех и коротко представься, 2 предложения."
                intro = generate_ai_response(sys_prompt, usr_prompt, max_sentences=2, max_chars=260)
                reply = f"@all 🔮 {intro}\n\n" + BOT_HELP_TEXT
                send_vk_message(peer_id, reply)
                return

            user_name, user_sex, user_last = get_vk_user_full(invited_id)
            user_sex = resolve_sex(user_name, user_sex)
            print(f"👤 [НОВИЧОК] {user_name or 'имя не получено'}, пол: {user_sex or 'неизвестен'}")
            user_name = user_name or "новичок"
            tone = pick_tone(friendly_only=True)
            print(f"🎭 [ТОН ПРИВЕТСТВИЯ] {tone}")
            chat_title = get_chat_title(peer_id)
            chat_info = (f"беседа называется: {chat_title}. можешь назвать ее естественно, своими словами или по названию."
                         if chat_title else "название беседы неизвестно, говори просто про эту беседу.")
            who = person_rule(user_name, user_sex, user_last, allow_play=False)

            # 1) короткое приветствие и представление
            hello_prompt = SYSTEM_PROMPT_BASE + f"""
            в беседу пришел новичок. напиши ровно 2 очень коротких теплых предложения, в каждом не больше 12 слов:
            1) поприветствуй его по имени и обязательно упомяни саму беседу, куда он пришел;
            2) представься и немного расскажи, откуда ты: ты гадалка кунегунда, долго жила в пещерах тропикании
               и вылезла к людям, чтобы помогать им своими магическими силами.
            {chat_info}
            никаких предсказаний, советов и пожеланий в этих предложениях.
            это первое знакомство: будь приветливой, никакого ворчания и подколов. обращение на ты. {who}
            """
            hello = generate_ai_response(hello_prompt, f"новичка зовут {user_name}, поприветствуй, упомяни беседу и расскажи, кто ты и откуда.",
                                         max_sentences=2, max_chars=200, names=(user_name, user_last), min_sentences=2)
            hello = fix_gender_words(hello, user_sex)
            label = f"@{get_screen_name(invited_id)}" if invited_id > 0 else None
            hello = tag_person(hello, invited_id, (user_name, user_last), label=label)

            # 2) приветственное гадание про жизнь в этой беседе клуба
            scene = pick_fresh("newcomer_scene", NEWCOMER_CHAT_SCENES, keep=12)
            print(f"🎬 [ГАДАНИЕ НОВИЧКУ] {scene}")
            fortune_prompt = SYSTEM_PROMPT_BASE + f"""
            сделай новичку приветственное гадание, как предсказание из шара: что хорошего, а может и смешного, случится с ним в этой беседе вконтакте.
            это беседа клуба по игре аватария: участники вместе играют, общаются, шлют мемы, влюбляются, собираются на клубные события.
            {chat_info}
            сюжет гадания: {scene}.
            предсказание позитивное и конкретное, с маленькой смешной деталью.
            даже простые вещи (найдешь друзей, тебе тут понравится) скажи необычно и смешно, по-своему, как гадалка кунегунда:
            через шар, карты, свечи, кофейную гущу или знаки судьбы, а не готовыми словами вроде «уютная атмосфера».
            твое настроение сейчас: {tone}. по-доброму, без ворчания и подколов.
            одно-два коротких предложения, всего не больше 25 слов. обращение на ты, имя не пиши. {who}
            {avoid_repeats_rule(invited_id)}
            """
            fortune = generate_ai_response(fortune_prompt, "сделай новичку приветственное гадание про эту беседу.",
                                           max_sentences=2, max_chars=190, names=(user_name, user_last))
            fortune = fix_gender_words(fortune, user_sex)
            remember_reply(invited_id, fortune)
            ritual = random.choice(NEWCOMER_RITUALS)
            reply = f"{hello}\n\n{ritual}\n{fortune}"
            reply += "\n\n" + NEWCOMER_HELP
            send_vk_message(peer_id, reply)
            return

        # 2. Пара дня
        if is_love_command(text):
            print("💘 [СОБЫТИЕ] Ищем пару дня!")
            handle_love(peer_id, from_id, cmid)
            return

        # 3. Команда "гадать"
        if is_fortune_command(text):
            print("🔮 [СОБЫТИЕ] Вызвана команда гадать!")
            if not check_and_update_limit_supabase(from_id):
                lname = get_vk_user_full(from_id)[0]
                limit_reply = random.choice(LIMIT_REACHED_RESPONSES)
                if lname:
                    limit_reply = f"{mention(from_id, lname)}, {limit_reply}"
                send_vk_message(peer_id, limit_reply, reply_to_cmid=cmid)
                return

            name, profile_sex, last_name = get_vk_user_full(from_id)
            sex = resolve_sex(name, profile_sex)
            print(f"👤 [ИГРОК] {name or 'имя неизвестно'}, пол: {sex or 'неизвестен'}")
            thinking = random.choice(THINKING_PHRASES).format(name=f"{mention(from_id, name)}, " if name else "")
            placeholder_cmid = send_vk_message(peer_id, thinking, reply_to_cmid=cmid)

            CURRENT_HOT["themes"] = hot_themes()
            if CURRENT_HOT["themes"]:
                print(f"🚫 [ТЕМЫ] пока не трогаем: {', '.join(CURRENT_HOT['themes'])}")
            topic, fact = pick_fact(from_id)
            fact, focus = split_list_fact(fact)
            mood = pick_fresh("mood", FORTUNE_MOODS, keep=4)
            if random.random() < NPC_SHIP_CHANCE:
                npc = pick_fresh("npc", without_hot(GAME_NPCS, CURRENT_HOT["themes"]), keep=8)
                mood = f"шуточный роман (шип) с игровым ботом: {npc_desc(npc)}. по-доброму и смешно, связав это с фактом"
            tone = pick_tone()
            lore = pick_fresh("lore", without_hot(KUNEGUNDA_LORE, CURRENT_HOT["themes"]), keep=20) if KUNEGUNDA_LORE and random.random() < LORE_CHANCE else None
            fmt = pick_fresh("format", FORTUNE_FORMATS, keep=6) if random.random() < FORMAT_CHANCE else None
            print(f"📚 [ФАКТ] {topic}: {fact}" + (f" | главное: {focus}" if focus else ""))
            print(f"🎭 [ТОН] {tone} | о чем: {mood} | о себе: {lore or 'нет'} | формат: {fmt or 'обычный'}")

            lore_rule = ""
            max_sent, max_len, max_words = 2, 220, 25

            sys_prompt = SYSTEM_PROMPT_BASE + f"""
            предсказание делается про игру аватария, строго про одну тему:
            тема: {topic}
            факт: {fact}

            {("главное в этот раз: " + focus + ". гадай только про это, остальное из списка не упоминай.") if focus else ""}

            как писать предсказание:
            1. только про этот факт. {NO_MIXING_RULE} не придумывай новые предметы.
            2. предскажи {mood}.{(" подай предсказание " + fmt + ".") if fmt else ""}
            2а. будь оригинальной и неожиданной, иногда смешной: никаких банальностей вроде тебя ждет удача.
            3. твое настроение сейчас: {tone}. это должно быть сразу заметно.
            4. не больше {max_sent} коротких предложений, всего не больше {max_words} слов.
            5. смысл должен быть понятен с первого прочтения, без нагромождений.
            5а. сегодня {msk_now().strftime("%d.%m.%Y")}. если факт про акцию, праздник или событие, которое уже прошло (ярмарка, хэллоуин, день семьи, пятница 13, весенний пруд, золотая осень и т.п.), гадай про следующее такое же событие: например после 10-й ярмарки будет 11-я, после прошлого хэллоуина будет следующий. предположи, что там будет похожее, как опытная гадалка.
            5б. это предсказание о будущем: не утверждай, что игрок уже что-то сделал. не придумывай нелепых деталей, которых нет в факте (никаких салатов, отверток и т.п.).
            6. обращайся на ты. {person_rule(name, sex, last_name)}{lore_rule}
            {avoid_repeats_rule(from_id)}
            """
            usr_prompt = "погадай мне."
            reply = generate_ai_response(sys_prompt, usr_prompt, max_sentences=max_sent, max_chars=max_len, names=(name, last_name))
            reply = fix_gender_words(reply, sex)
            remember_reply(from_id, reply)
            reply = tag_person(reply, from_id, (name, last_name))
            if lore:
                about_me = lore_phrase(lore)
                if about_me:
                    reply += "\n\n" + about_me
            reply += remaining_note(from_id)
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
