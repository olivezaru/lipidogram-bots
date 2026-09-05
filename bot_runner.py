import os
import re
import io
import time
import json
import urllib.parse
import imaplib
import email
from email.header import decode_header
import asyncio
import logging
import requests
import random
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi
from datetime import datetime, timedelta
from dotenv import load_dotenv
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import BufferedInputFile
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DEFAULT_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or os.getenv("BOT_TOKEN", "").strip()
MODERATOR_TOKEN = os.getenv("MODERATOR_BOT_TOKEN", "").strip() or DEFAULT_BOT_TOKEN
POSTER_TOKEN = os.getenv("POSTER_BOT_TOKEN", "").strip() or DEFAULT_BOT_TOKEN or MODERATOR_TOKEN
if not MODERATOR_TOKEN and POSTER_TOKEN:
    MODERATOR_TOKEN = POSTER_TOKEN

KIE_KEY = os.getenv("KIE_API_KEY", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
KIE_MODEL = os.getenv("KIE_MODEL", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "@lipidogram").strip()
PORT = int(os.getenv("PORT", 8080))

EMAIL_HOST = os.getenv("EMAIL_HOST", "imap.gmail.com").strip()
EMAIL_USER = os.getenv("EMAIL_USER", "").strip()
EMAIL_PASS = os.getenv("EMAIL_PASS", "").strip()

HISTORY_FILE = "published_history.json"
MAX_HISTORY_SIZE = 300

if not MODERATOR_TOKEN and not POSTER_TOKEN:
    logging.error("КРИТИЧЕСКАЯ ОШИБКА: Ни TELEGRAM_BOT_TOKEN, ни MODERATOR_BOT_TOKEN, ни POSTER_BOT_TOKEN не заданы!")

bot_moderator = Bot(token=MODERATOR_TOKEN) if MODERATOR_TOKEN else None

if POSTER_TOKEN and POSTER_TOKEN != MODERATOR_TOKEN:
    bot_poster = Bot(token=POSTER_TOKEN)
else:
    bot_poster = bot_moderator

dp = Dispatcher()
user_warnings = {}

BAD_WORDS_PATTERN = re.compile(
    r'\b(ху[йиеяё]|пизд|бл[яе]|еб[аелиотс]|сук[аи]|муд[ао]|говно|залуп|чмо|дерьм|шлюх|гандон)\w*',
    re.IGNORECASE
)
SPAM_LINKS_PATTERN = re.compile(
    r'(https?://\S+|t\.me/\S+|telegram\.me/\S+|@[a-zA-Z0-9_]{5,})',
    re.IGNORECASE
)

def load_history() -> set:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(data)
        except Exception as e:
            logging.warning(f"Не удалось прочитать {HISTORY_FILE}: {e}")
    return set()

def save_history(history_set: set):
    try:
        items = list(history_set)[-MAX_HISTORY_SIZE:]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Не удалось сохранить {HISTORY_FILE}: {e}")

published_history = load_history()

def is_already_published(item_id_or_url: str) -> bool:
    if not item_id_or_url:
        return False
    clean = str(item_id_or_url).strip().lower()
    return clean in published_history

def mark_as_published(item_id_or_url: str):
    if not item_id_or_url:
        return
    clean = str(item_id_or_url).strip().lower()
    published_history.add(clean)
    save_history(published_history)

# Вспомогательная функция для безопасного поиска XML-тегов
def find_xml_elem(parent, tag_names: list):
    """
    Безопасный поиск первого существующего тега в XML ElementTree.
    В Python bool(Element) возвращает False, если у тега нет дочерних узлов!
    Поэтому конструкция 'elem.find("a") or elem.find("b")' всегда ломается.
    Данная функция проверяет 'is not None'.
    """
    for tag in tag_names:
        elem = parent.find(tag)
        if elem is not None:
            return elem
    return None

# 1. Российские научно-популярные и медицинские журналы и ленты
RU_JOURNALS_RSS = [
    {
        "id": "biomolecula",
        "aliases": ["био", "биомолекула", "bio", "biomolecula"],
        "type": "biomolecula",
        "name": "Журнал «Биомолекула» (молекулярная медицина и биохимия)",
        "category": "🔬 НАУЧНЫЙ ДАЙДЖЕСТ: БИОХИМИЯ И СОСУДЫ",
        "hashtags": "#Липидограм_Наука #Биомолекула #Биохимия #Кардиология"
    },
    {
        "id": "zozhnik",
        "aliases": ["зожник", "zozhnik", "zozh"],
        "type": "rss",
        "name": "Журнал доказательного фитнеса и питания «Зожник»",
        "rss": "https://zozhnik.ru/feed",
        "category": "🥗 ДОКАЗАТЕЛЬНОЕ ПИТАНИЕ И ЗДОРОВЬЕ",
        "hashtags": "#Липидограм_Наука #Зожник #ПитаниеСердца #Клетчатка"
    },
    {
        "id": "habr_health",
        "aliases": ["хабр", "habr", "здоровье", "health", "habr_health"],
        "type": "rss",
        "name": "Хабр Научпоп (Медицина, здоровье и биохимия)",
        "rss": "https://habr.com/ru/rss/hubs/health/?fl=ru",
        "category": "🔬 НАУЧНЫЙ ДАЙДЖЕСТ: МЕДИЦИНА И ЗДОРОВЬЕ",
        "hashtags": "#Липидограм_Наука #ХабрНаука #ЗдоровьеСосудов #Кардиология"
    },
    {
        "id": "habr_biotech",
        "aliases": ["биотех", "biotech", "генетика", "habr_biotech"],
        "type": "rss",
        "name": "Хабр Биотехнологии и генетика",
        "rss": "https://habr.com/ru/rss/hubs/biotech/?fl=ru",
        "category": "🧬 БИОТЕХНОЛОГИИ И МОЛЕКУЛЯРНАЯ МЕДИЦИНА",
        "hashtags": "#Липидограм_Наука #Биотехнологии #Генетика #Кардиология"
    },
    {
        "id": "nplus1",
        "aliases": ["n1", "nplus1", "н1", "нплюсодин", "n+1"],
        "type": "rss",
        "name": "N+1 (Наука, физиология и медицина)",
        "rss": "https://nplus1.ru/rss",
        "category": "🔬 ДОКАЗАТЕЛЬНАЯ МЕДИЦИНА И НАУКА",
        "hashtags": "#Липидограм_Наука #NPlus1 #Доказательно #Кардиология"
    },
    {
        "id": "naked_science",
        "aliases": ["ns", "naked", "naked_science", "нейкед", "нейкед_сайнс"],
        "type": "rss",
        "name": "Naked Science (Медицина и здоровье)",
        "rss": "https://naked-science.ru/article/medicine/feed",
        "category": "🔬 ДОКАЗАТЕЛЬНАЯ МЕДИЦИНА И НАУКА",
        "hashtags": "#Липидограм_Наука #NakedScience #Медицина #Кардиология"
    }
]

# 2. Российские YouTube каналы
RU_HEALTH_CHANNELS = [
    {"name": "Российское кардиологическое общество (РКО / scardioru)", "channel_id": "UCjWbK_tC3vD6vf3qZ1jH8tw", "handle": "@scardioru"},
    {"name": "Доктор Утин (кардиохирург Алексей Утин)", "channel_id": "UCe1Qc_VqL_8rLq9a-tM_HwQ", "handle": "@DoctorUtin"},
    {"name": "Кардиолог Тамаз Гаглошвили", "channel_id": "UCk46gLhW1lM3Jp-vjZ_L5HQ", "handle": "@doctor_tamaz"},
    {"name": "СМТ — Научный подход (Борис Цацулин)", "channel_id": "UCi1p7P6-O3sV3h98rW2g6mA", "handle": "@CavemanTech"}
]

# 3. Зарубежные эксперты YouTube
GLOBAL_HEALTH_CHANNELS = [
    {"name": "Dr. Peter Attia (липидология и долголетие)", "channel_id": "UCF_fDSgblvyC-hltP1t4gTg", "handle": "@PeterAttiaMD"},
    {"name": "Dr. Gil Carvalho (Nutrition Made Simple)", "channel_id": "UCosmc75v-4N3A7OHr8G25Ew", "handle": "@NutritionMadeSimple"},
    {"name": "Dr. Rhonda Patrick (FoundMyFitness)", "channel_id": "UCWF9aXYms1JpTf_bkW_X27g", "handle": "@FoundMyFitness"},
    {"name": "Dr. Brad Stanfield (превентивная медицина)", "channel_id": "UCpcvPevmCfu_UhyvCc_1K8A", "handle": "@DrBradStanfield"},
    {"name": "Simon Hill / The Proof", "channel_id": "UCE0f85hX8Qz2e0n_W1i5qKw", "handle": "@TheProofWithSimonHill"},
    {"name": "Huberman Lab (Стэнфорд)", "channel_id": "UC2D2CMWXMOVWx7giW1n3LIg", "handle": "@hubermanlab"}
]

HEALTH_KEYWORDS = [
    "холестерин", "сосуд", "сердц", "лпнп", "давлен", "гипертон", "питан", "диета", "статины",
    "жир", "ходьба", "спорт", "атеросклероз", "бляшк", "долголетие", "кардио", "триглицерид",
    "рко", "инфаркт", "инсульт", "апоб", "apob", "липидограм", "анализ", "чекап", "клетчатк",
    "омега-3", "омега 3", "липид", "пульс", "аэробн", "выносливост", "рецепт", "блюд",
    "cholesterol", "ldl", "hdl", "apob", "artery", "atherosclerosis", "heart", "cardio",
    "blood pressure", "hypertension", "diet", "fasting", "exercise", "longevity", "lipids",
    "statins", "triglycerides", "omega-3", "nutrition", "vessel", "plaque", "glucose", "metabolism", "recipe"
]

# Ключевые слова и стоп-слова для строгого отбора статей из российских научных журналов (Хабр, N+1, Naked Science, Биомолекула)
RU_HIGH_PRIO_KEYWORDS = [
    "холестерин", "сосуд", "сердц", "лпнп", "лпвп", "давлен", "гипертон", "гипертензи",
    "атеросклероз", "бляшк", "артери", "инфаркт", "инсульт", "апоб", "apob", "триглицерид",
    "липид", "кардио", "статин", "омега", "клетчатк"
]

RU_HEALTH_KEYWORDS = RU_HIGH_PRIO_KEYWORDS + [
    "питан", "диет", "жир", "метаболизм", "глюкоз", "диабет", "инсулин",
    "микробиом", "печен", "ожирени", "спорт", "тренировк", "ходьб", "сон",
    "бег", "пульс", "долголети", "старени", "кров", "ген", "днк", "лекарств",
    "препарат", "пациент", "клиническ", "воспалени", "биохими", "нутриент",
    "калори", "белок", "углевод", "соль", "натрий", "сосудист", "эндотел"
]

# Стоп-слова: приложения, IT, программирование, сканеры, штрихкоды, космос и нерелевантные темы
RU_STOP_WORDS = [
    "штрихкод", "сканер", "сканирован", "руководител", "стартап", "репозитор", "devops", 
    "киборг", "ваканси", "карьер", "астероид", "галактик", "луна", "марс", "космос",
    "динозавр", "палеонтолог", "квантов", "телескоп", "дрон", "оружи",
    "приложени", "obsidian", "гаджет", "смартфон", "чат-бот", "нейросеть",
    "программист", "разработк", "личную систему", "crm", "бэкэнд", "фронтенд",
    "flutter", "swift", "backend", "frontend", "linux", "архитектур", "микросервис",
    "релиз", "баг", "код", "ocr", "распознаван"
]

def is_relevant_ru_article(title: str, text: str) -> tuple[bool, int]:
    """
    Проверяет релевантность статьи тематике канала «Липидограм» (кардиология, сосуды, питание, метаболизм).
    Возвращает (is_relevant: bool, score: int).
    score: 2 = кардио / липиды / сосуды (наивысший приоритет)
    score: 1 = доказательная медицина / биология / ЗОЖ / метаболизм
    score: 0 = не релевантна / содержит IT-стоп-слова
    """
    combined = f"{title} {text}".lower()
    title_lower = title.lower()

    # Жесткий фильтр стоп-слов: если в заголовке или тексте софт/приложение/штрихкод
    for sw in RU_STOP_WORDS:
        if sw in title_lower:
            return False, 0
        if sw in combined and not any(h in combined for h in RU_HIGH_PRIO_KEYWORDS):
            return False, 0

    has_high = any(kw in combined for kw in RU_HIGH_PRIO_KEYWORDS)
    if has_high:
        return True, 2

    has_health = any(kw in combined for kw in RU_HEALTH_KEYWORDS)
    if has_health:
        return True, 1

    return False, 0

RUBRIC_RECIPES = {
    "category": "🥗 ГИПОЛИПИДЕМИЧЕСКАЯ КУХНЯ",
    "source_type": "recipe_source",
    "style_type": "recipe_card",
    "ru_theme": "Реальное кулинарное блюдо из медицинских и диетологических исследований для снижения ЛПНП",
    "hashtags": "#Рецепт_ЛПНП #УмнаяЗамена #ПитаниеСердца #Клетчатка"
}

RUBRIC_YOUTUBE = {
    "category": "📺 МИРОВОЙ И РОССИЙСКИЙ НАУЧПОП / ВЫЖИМКА",
    "source_type": "youtube",
    "style_type": "story_or_interview",
    "ru_theme": "Увлекательная выжимка из видео экспертов о холестерине, сосудах и здоровье",
    "hashtags": "#Липидограм_Видео #Научпоп #РКО #Долголетие #ЗдоровьеСосудов"
}

RUBRIC_MYTHS = {
    "category": "💡 МИФ ИЛИ РЕАЛЬНОСТЬ",
    "source_type": "pubmed",
    "style_type": "myth_buster",
    "query": '("dietary cholesterol" OR "eggs" OR "statins" OR "omega-3 fatty acids" OR "coffee") AND ("atherosclerosis" OR "cardiovascular") AND ("meta-analysis" OR "systematic review")',
    "ru_theme": "Разбор популярного мифа доказательной медициной через PubMed",
    "hashtags": "#Мифы_Липидограм #Доказательно #Холестерин"
}

RUBRIC_SPORT = {
    "category": "🏃 АКТИВНОСТЬ И ЭЛАСТИЧНОСТЬ СОСУДОВ",
    "source_type": "pubmed",
    "style_type": "practical_guide",
    "query": '("aerobic exercise" OR "resistance training" OR "walking" OR "interval training") AND ("flow-mediated dilation" OR "endothelial" OR "HDL-C" OR "lipid profile") AND ("trial" OR "randomized")',
    "ru_theme": "Разнообразные формы движения для сосудов и сердца (исследования PubMed)",
    "hashtags": "#Движение_Липидограм #ЗдоровьеСердца #Активность #ЭластичностьСосудов"
}

RUBRIC_ACADEMIC_SCIENCE = {
    "category": "🔬 НАУЧНЫЙ ДАЙДЖЕСТ (PUBMED / РКО)",
    "source_type": "pubmed",
    "style_type": "expert_review",
    "query": '("LDL cholesterol" OR "ApoB" OR "atherosclerosis" OR "statins" OR "PCSK9") AND ("cardiovascular" OR "clinical trial" OR "meta-analysis")',
    "ru_theme": "Клинические новости PubMed, мета-анализы и доказательная кардиология",
    "hashtags": "#Липидограм_Наука #PubMed #Кардиология #ЛПНП"
}

RUBRIC_RU_JOURNALS = {
    "category": "🇷🇺 РОССИЙСКАЯ ДОКАЗАТЕЛЬНАЯ МЕДИЦИНА",
    "source_type": "ru_journals",
    "style_type": "expert_review",
    "ru_theme": "Статьи из ведущих российских научных изданий (Биомолекула, Зожник, Хабр Наука, N+1, Naked Science)",
    "hashtags": "#Липидограм_Наука #Доказательно #Биохимия #Кардиология"
}

ALL_RUBRICS_POOL = [
    RUBRIC_ACADEMIC_SCIENCE,  # PubMed
    RUBRIC_MYTHS,             # PubMed (мифы)
    RUBRIC_SPORT,             # PubMed (спорт)
    RUBRIC_RU_JOURNALS,       # Российские научные издания
    RUBRIC_RECIPES,           # Рецепты гиполипидемической кухни
    RUBRIC_YOUTUBE            # Видеоролики экспертов
]

SYSTEM_PROMPT = """
Ты — главный редактор русскоязычного Telegram-канала «Липидограм» (@lipidogram).
Твоя задача — написать яркий, легкий и увлекательный пост простым человеческим языком НА ОСНОВЕ ПРЕДОСТАВЛЕННОГО ПЕРВОИСТОЧНИКА.

СТРОГИЕ ЗАПРЕТЫ И ПРАВИЛА:
1. КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ШТАМП «8000 шагов» и «прогулка быстрым шагом в разговорном темпе». Не повторяй эту фразу! Используй разнообразные синонимы и формы активности:
   - «бодрая 20-минутная прогулка после еды»
   - «лёгкая утренняя зарядка для сосудистого тонуса»
   - «комфортный бег трусцой или велосипед без одышки»
   - «подъём по лестнице вместо лифта»
   - «ежедневный объем движения»
   - «интервальная разминка на свежем воздухе»
2. ЕСЛИ ЭТО РЕЦЕПТ: опирайся СТРОГО на предоставленный список ингредиентов из первоисточника. Опиши простое пошаговое приготовление и объясни пользу для снижения ЛПНП (клетчатка, замена насыщенных жиров на оливковое/льняное масло или авокадо).
3. СТРОГАЯ ТЕМАТИЧЕСКАЯ РЕЛЕВАНТНОСТЬ: Пиши СТРОГО о здоровье, сосудах, кардиологии, липидах (холестерине), метаболизме, биохимии крови или доказательном питании. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать о приложениях, смартфонах, сканерах штрихкодов, IT-софте, программировании, гаджетах или стартапах. Не пытайся «притягивать» посторонние IT-статьи к теме сосудов.
4. Объем текста: 550-800 символов.
5. Разрешенные теги: <b>, </b>, <i>, </i>, <code>, </code>, <a href="...">.
6. Знаки < и > пиши словами («менее», «более») или экранируй (&lt; и &gt;).
7. В самом конце обязательно укажи первоисточник со ссылкой и хештеги.

ТРЕБОВАНИЯ К IMAGE PROMPT ("image_prompt"):
- На АНГЛИЙСКОМ языке.
- Описывай конкретную фотореалистичную сцену для генерации.

ВЕРНИ ОТВЕТ СТРОГО В JSON:
{
  "post_text": "...",
  "image_prompt": "..."
}
"""

def extract_text_from_ai_json(raw_json: dict) -> str:
    """Извлекает сгенерированный текст из ответа Gemini или OpenAI формата."""
    if not isinstance(raw_json, dict):
        return ""
    # OpenAI format: choices[0].message.content
    choices = raw_json.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message", {})
        content = msg.get("content", "")
        if content:
            return str(content).strip()
    # Gemini format: candidates[0].content.parts[0].text
    candidates = raw_json.get("candidates")
    if isinstance(candidates, list) and candidates:
        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        if isinstance(parts, list) and parts:
            text = parts[0].get("text", "")
            if text:
                return str(text).strip()
    return ""

def parse_ai_json_response(raw_text: str) -> tuple[str, str]:
    """Парсит JSON из ответа ИИ и возвращает (post_text, image_prompt)."""
    if not raw_text:
        return "", ""
    clean_str = raw_text.strip()
    if clean_str.startswith("```json"):
        clean_str = clean_str[7:]
    elif clean_str.startswith("```"):
        clean_str = clean_str[3:]
    if clean_str.endswith("```"):
        clean_str = clean_str[:-3]
    clean_str = clean_str.strip()

    match = re.search(r'\{[\s\S]*\}', clean_str)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict) and "code" not in parsed:
                post_text = parsed.get("post_text") or parsed.get("text") or parsed.get("post") or parsed.get("content")
                image_prompt = parsed.get("image_prompt") or parsed.get("prompt") or parsed.get("image")
                if post_text and len(str(post_text).strip()) > 40:
                    return str(post_text).strip(), str(image_prompt or "healthy cardiology lifestyle").strip()
        except Exception:
            pass
    return "", ""

def generate_kie_text_and_prompt(user_prompt: str) -> tuple[str, str]:
    if not KIE_KEY and not GEMINI_KEY:
        raise ValueError("Ни KIE_API_KEY, ни GEMINI_API_KEY не установлены в переменных окружения!")

    endpoints = []

    # 0. Если пользователь явно задал конкретную модель в KIE_MODEL
    if KIE_KEY and KIE_MODEL:
        endpoints.append({
            "desc": f"KIE.ai (пользовательская модель {KIE_MODEL})",
            "url": "https://api.kie.ai/v1/chat/completions",
            "headers": {"Authorization": f"Bearer {KIE_KEY}", "Content-Type": "application/json"},
            "payload": {
                "model": KIE_MODEL,
                "messages": [
                    {"role": "user", "content": f"{SYSTEM_PROMPT}\n\nЗАДАНИЕ:\n{user_prompt}"}
                ],
                "temperature": 0.75
            }
        })

    # СТРОГО ТОЛЬКО МОДЕЛЬ GEMINI 3.7 FLASH
    if KIE_KEY:
        # 1. KIE.ai официальный выделенный OpenAI-совместимый эндпоинт gemini-3-7-flash-openai
        endpoints.append({
            "desc": "KIE.ai Gemini 3.7 Flash (gemini-3-7-flash-openai/v1/chat/completions)",
            "url": "https://api.kie.ai/gemini-3-7-flash-openai/v1/chat/completions",
            "headers": {"Authorization": f"Bearer {KIE_KEY}", "Content-Type": "application/json"},
            "payload": {
                "model": "gemini-3.7-flash",
                "messages": [
                    {"role": "user", "content": f"{SYSTEM_PROMPT}\n\nЗАДАНИЕ:\n{user_prompt}"}
                ],
                "temperature": 0.75
            }
        })
        endpoints.append({
            "desc": "KIE.ai Gemini 3.7 Flash (gemini-3-7-flash-openai со system-ролью)",
            "url": "https://api.kie.ai/gemini-3-7-flash-openai/v1/chat/completions",
            "headers": {"Authorization": f"Bearer {KIE_KEY}", "Content-Type": "application/json"},
            "payload": {
                "model": "gemini-3.7-flash",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.75
            }
        })

        # 2. KIE.ai Gemini 3.7 Flash через прямые slug-эндпоинты
        endpoints.append({
            "desc": "KIE.ai Gemini 3.7 Flash (/gemini-3.7-flash/v1/chat/completions)",
            "url": "https://api.kie.ai/gemini-3.7-flash/v1/chat/completions",
            "headers": {"Authorization": f"Bearer {KIE_KEY}", "Content-Type": "application/json"},
            "payload": {
                "model": "gemini-3.7-flash",
                "messages": [
                    {"role": "user", "content": f"{SYSTEM_PROMPT}\n\nЗАДАНИЕ:\n{user_prompt}"}
                ],
                "temperature": 0.75
            }
        })
        endpoints.append({
            "desc": "KIE.ai Gemini 3.7 Flash (/gemini-3-7-flash/v1/chat/completions)",
            "url": "https://api.kie.ai/gemini-3-7-flash/v1/chat/completions",
            "headers": {"Authorization": f"Bearer {KIE_KEY}", "Content-Type": "application/json"},
            "payload": {
                "model": "gemini-3-7-flash",
                "messages": [
                    {"role": "user", "content": f"{SYSTEM_PROMPT}\n\nЗАДАНИЕ:\n{user_prompt}"}
                ],
                "temperature": 0.75
            }
        })

        # 3. KIE.ai базовый шлюз /v1/chat/completions
        endpoints.append({
            "desc": "KIE.ai /v1/chat/completions (model: gemini-3.7-flash)",
            "url": "https://api.kie.ai/v1/chat/completions",
            "headers": {"Authorization": f"Bearer {KIE_KEY}", "Content-Type": "application/json"},
            "payload": {
                "model": "gemini-3.7-flash",
                "messages": [
                    {"role": "user", "content": f"{SYSTEM_PROMPT}\n\nЗАДАНИЕ:\n{user_prompt}"}
                ],
                "temperature": 0.75
            }
        })
        endpoints.append({
            "desc": "KIE.ai /v1/chat/completions (model: gemini-3-7-flash)",
            "url": "https://api.kie.ai/v1/chat/completions",
            "headers": {"Authorization": f"Bearer {KIE_KEY}", "Content-Type": "application/json"},
            "payload": {
                "model": "gemini-3-7-flash",
                "messages": [
                    {"role": "user", "content": f"{SYSTEM_PROMPT}\n\nЗАДАНИЕ:\n{user_prompt}"}
                ],
                "temperature": 0.75
            }
        })

        # 4. KIE.ai нативные Gemini 3.7 Flash прокси :generateContent
        gemini_native_payload = {
            "contents": [{"parts": [{"text": f"{SYSTEM_PROMPT}\n\nЗАДАНИЕ:\n{user_prompt}"}]}],
            "generationConfig": {"temperature": 0.75}
        }
        endpoints.append({
            "desc": "KIE.ai Gemini 3.7 Flash native proxy (gemini-3.7-flash)",
            "url": f"https://api.kie.ai/gemini/v1/models/gemini-3.7-flash:generateContent?key={KIE_KEY}",
            "headers": {"Authorization": f"Bearer {KIE_KEY}", "x-goog-api-key": KIE_KEY, "Content-Type": "application/json"},
            "payload": gemini_native_payload
        })
        endpoints.append({
            "desc": "KIE.ai Gemini 3.7 Flash native proxy (gemini-3-7-flash)",
            "url": f"https://api.kie.ai/gemini/v1/models/gemini-3-7-flash:generateContent?key={KIE_KEY}",
            "headers": {"Authorization": f"Bearer {KIE_KEY}", "x-goog-api-key": KIE_KEY, "Content-Type": "application/json"},
            "payload": gemini_native_payload
        })

    # 5. Официальный Google Gemini API (строго gemini-3.7-flash)
    if GEMINI_KEY:
        google_payload = {
            "contents": [{"parts": [{"text": f"{SYSTEM_PROMPT}\n\nЗАДАНИЕ:\n{user_prompt}"}]}],
            "generationConfig": {"temperature": 0.75}
        }
        endpoints.append({
            "desc": "Official Google Gemini 3.7 Flash API (gemini-3.7-flash)",
            "url": f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.7-flash:generateContent?key={GEMINI_KEY}",
            "headers": {"Content-Type": "application/json"},
            "payload": google_payload
        })

    last_error = ""
    for ep in endpoints:
        desc = ep["desc"]
        url = ep["url"]
        headers = ep["headers"]
        payload = ep["payload"]
        try:
            logging.info(f"Отправка запроса в {desc}...")
            resp = requests.post(url, headers=headers, json=payload, timeout=60)
            if resp.status_code == 200:
                raw_json = resp.json()
                if isinstance(raw_json, dict):
                    code = raw_json.get("code")
                    msg = str(raw_json.get("msg") or raw_json.get("message") or "")
                    if (code is not None and code not in [200, 0]) or "The page does not exist" in msg or "exception" in msg.lower():
                        last_error = f"{desc}: {msg or f'code {code}'}"
                        logging.warning(f"Ошибка ответа {desc}: {last_error}, пробуем следующий вариант...")
                        continue

                raw_text = extract_text_from_ai_json(raw_json)
                post_text, image_prompt = parse_ai_json_response(raw_text)
                if post_text:
                    logging.info(f"Успешная генерация текста через {desc}!")
                    return post_text, image_prompt
                else:
                    last_error = f"{desc}: некорректный ответ модели ({raw_text[:120]}...)"
                    logging.warning(last_error)
                    continue
            else:
                last_error = f"{desc}: HTTP {resp.status_code} ({resp.text[:120]})"
                logging.warning(f"Сбой HTTP от {desc}: {last_error}, переходим к следующему...")
                continue
        except Exception as e:
            last_error = f"{desc}: {e}"
            logging.warning(f"Исключение при запросе к {desc}: {e}, переходим к следующему...")
            continue

    raise Exception(f"Все доступные варианты Gemini 3.7 завершились ошибкой. Последняя: {last_error}")

def extract_all_urls_from_any_json(obj) -> list:
    found = []
    if isinstance(obj, str):
        if obj.startswith("http") and any(ext in obj.lower() for ext in [".jpg", ".jpeg", ".png", ".webp", "kie", "cdn", "image", "oss"]):
            found.append(obj)
        elif obj.startswith("{") or obj.startswith("["):
            try:
                parsed = json.loads(obj)
                found.extend(extract_all_urls_from_any_json(parsed))
            except Exception:
                pass
    elif isinstance(obj, dict):
        for k, v in obj.items():
            found.extend(extract_all_urls_from_any_json(v))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(extract_all_urls_from_any_json(item))
    return found

async def generate_kie_image_bytes(image_prompt: str) -> bytes:
    if not KIE_KEY or not image_prompt:
        return None

    create_url = "https://api.kie.ai/api/v1/jobs/createTask"
    headers = {
        "Authorization": f"Bearer {KIE_KEY}",
        "Content-Type": "application/json"
    }

    clean_prompt = f"Professional commercial photography, 8k resolution, photorealistic, {image_prompt}"
    payload = {
        "model": "nano-banana-2-lite",
        "input": {
            "prompt": clean_prompt,
            "aspect_ratio": "1:1",
            "width": 1024,
            "height": 1024,
            "image_num": 1
        }
    }

    try:
        async with aiohttp.ClientSession() as session:
            logging.info(f"Создание задачи Nano Banana 2 Lite... Промпт: {clean_prompt[:60]}...")
            async with session.post(create_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    text_resp = await resp.text()
                    logging.warning(f"KIE createTask HTTP {resp.status}: {text_resp[:150]}")
                    return None
                res_data = await resp.json()

            immediate_urls = extract_all_urls_from_any_json(res_data)
            if immediate_urls:
                async with session.get(immediate_urls[0], timeout=aiohttp.ClientTimeout(total=25)) as dl:
                    if dl.status == 200:
                        return await dl.read()

            task_id = (
                res_data.get("data", {}).get("taskId") 
                or res_data.get("data", {}).get("id") 
                or res_data.get("taskId") 
                or res_data.get("id")
            )

            if not task_id:
                return None

            logging.info(f"Задача создана (TaskId: {task_id}). Ожидание генерации Nano Banana (до 100 сек)...")

            status_urls = [
                f"https://api.kie.ai/api/v1/jobs/recordInfo?taskId={task_id}",
                f"https://api.kie.ai/api/v1/jobs/getTask?taskId={task_id}",
                f"https://api.kie.ai/api/v1/jobs/{task_id}",
                f"https://api.kie.ai/api/v1/jobs/record/{task_id}"
            ]

            for attempt in range(1, 26):
                await asyncio.sleep(4)
                for s_url in status_urls:
                    try:
                        async with session.get(s_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as s_resp:
                            if s_resp.status == 200:
                                s_data = await s_resp.json()
                                urls_in_status = extract_all_urls_from_any_json(s_data)
                                if urls_in_status:
                                    async with session.get(urls_in_status[0], timeout=aiohttp.ClientTimeout(total=30)) as dl:
                                        if dl.status == 200:
                                            b_data = await dl.read()
                                            if len(b_data) > 2000:
                                                return b_data
                    except Exception:
                        continue
    except Exception as e:
        logging.warning(f"Ошибка вызова Nano Banana 2 Lite: {e}")

    return None

# ПОИСК РЕАЛЬНЫХ ДОКАЗАТЕЛЬНЫХ РЕЦЕПТОВ (С ФИЛЬТРОМ ИСТОРИИ)
def fetch_real_cardio_recipe() -> dict:
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        z_resp = requests.get("https://zozhnik.ru/category/eda/recepty/feed", headers=headers, timeout=8)
        if z_resp.status_code == 200:
            root = ET.fromstring(z_resp.content)
            items = root.findall(".//item")
            if items:
                random.shuffle(items)
                for it in items:
                    title_elem = find_xml_elem(it, ["title", "{http://www.w3.org/2005/Atom}title"])
                    link_elem = find_xml_elem(it, ["link", "{http://www.w3.org/2005/Atom}link"])
                    desc_elem = find_xml_elem(it, ["{http://purl.org/rss/1.0/modules/content/}encoded", "description", "{http://www.w3.org/2005/Atom}summary"])

                    if title_elem is not None and link_elem is not None:
                        t = title_elem.text.strip() if title_elem.text else ""
                        l = link_elem.text.strip() if link_elem.text else link_elem.attrib.get("href", "").strip()
                        
                        if not l or is_already_published(l):
                            continue

                        d = desc_elem.text if (desc_elem is not None and desc_elem.text) else ""
                        clean_d = BeautifulSoup(d, "html.parser").get_text(separator=" ", strip=True)
                        if any(kw in f"{t} {clean_d}".lower() for kw in ["рыб", "овсян", "чечевиц", "нут", "фасол", "авокадо", "салат", "оливк", "семен", "овощ", "клетчатк"]):
                            return {
                                "id": l,
                                "title": t,
                                "journal": "Журнал доказательного питания «Зожник»",
                                "content": f"Рецепт: {t}\nОписание и ингредиенты:\n{clean_d[:2500]}",
                                "url": l
                            }
    except Exception as e:
        logging.warning(f"Ошибка парсинга рецептов Зожника: {e}")

    query = '("Mediterranean diet" OR "Portfolio diet" OR "Oat beta-glucan" OR "Legumes" OR "Walnuts" OR "Flaxseed") AND ("LDL cholesterol" OR "lipid lowering") AND ("recipe" OR "dietary intervention" OR "trial")'
    study = fetch_pubmed_study_with_abstract(query)
    if study:
        return {
            "id": study.get("pmid", study["url"]),
            "title": f"Кардиопротективный рацион: {study['title']}",
            "journal": f"PubMed / {study['journal']}",
            "content": f"Исследование: {study['title']}\nДанные по продуктам и нутриентам:\n{study['abstract']}",
            "url": study['url']
        }

    fallback_url = "https://scardio.ru/news/novosti_obschestva/"
    return {
        "id": fallback_url,
        "title": "Средиземноморский салат с чечевицей, авокадо и льняной заправкой",
        "journal": "Клинические рекомендации по гиполипидемической диете (ESC / AHA Guidelines)",
        "content": "Ингредиенты: отварная зеленая чечевица (богата растворимой клетчаткой), спелый авокадо (мононенасыщенные жиры), свежий шпинат, оливковое масло первого отжима Extra Virgin, семена льна. Содержание насыщенных жиров менее 1.5г.",
        "url": fallback_url
    }

def fetch_biomolecula_article() -> dict:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        req = requests.get("https://biomolecula.ru/articles", headers=headers, timeout=8)
        if req.status_code == 200:
            slugs = re.findall(r'href=[\"\'](/articles/[a-z0-9\-]+)[\"\']', req.text)
            valid = list(set([s for s in slugs if s not in ['/articles/top', '/articles/archive']]))
            if valid:
                random.shuffle(valid)
                for slug in valid:
                    url = f"https://biomolecula.ru{slug}"
                    if is_already_published(url):
                        continue
                    
                    art_req = requests.get(url, headers=headers, timeout=8)
                    if art_req.status_code == 200:
                        soup = BeautifulSoup(art_req.content, "html.parser")
                        h1 = soup.find("h1")
                        title = h1.get_text(strip=True) if h1 else "Научная статья"
                        paras = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 40]
                        content = "\n".join(paras[:5])
                        
                        relevant, _ = is_relevant_ru_article(title, content)
                        if not relevant:
                            continue

                        if len(content) > 100:
                            return {
                                "id": url,
                                "title": title,
                                "journal": "Журнал «Биомолекула» (молекулярная медицина и биохимия)",
                                "category": "🔬 НАУЧНЫЙ ДАЙДЖЕСТ: БИОХИМИЯ И СОСУДЫ",
                                "content": f"Заголовок: {title}\nИздание: Журнал «Биомолекула»\n\nТекст статьи:\n{content[:2800]}",
                                "url": url,
                                "hashtags": "#Липидограм_Наука #Биомолекула #Биохимия #Кардиология"
                            }
    except Exception as e:
        logging.warning(f"Ошибка парсинга Биомолекулы: {e}")
    return None

# ПАРСИНГ РОССИЙСКИХ НАУЧНЫХ ЖУРНАЛОВ (С АДРЕСНЫМ ВЫБОРОМ ИЛИ РАНДОМОМ)
def fetch_russian_journals_rss(target_source: str = None) -> dict:
    journals = list(RU_JOURNALS_RSS)

    if target_source:
        t_clean = target_source.strip().lower()
        matched = [
            j for j in journals 
            if j.get("id") == t_clean or t_clean in j.get("aliases", [])
        ]
        if matched:
            journals = matched
        else:
            logging.warning(f"Источник '{target_source}' не распознан, используем случайный пул.")
            journals = [j for j in journals if not j.get("cloud_blocked")]
            random.shuffle(journals)
    else:
        # Исключаем источники, блокирующие зарубежные IP облачных платформ (Render/AWS/GCP)
        journals = [j for j in journals if not j.get("cloud_blocked")]
        random.shuffle(journals)

    for j in journals:
        if j.get("type") == "biomolecula":
            bio = fetch_biomolecula_article()
            if bio:
                return bio
            continue

        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            timeout_sec = j.get("timeout", 7)
            resp = requests.get(j["rss"], headers=headers, timeout=timeout_sec)
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                items = root.findall(".//item")
                if not items:
                    items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

                candidates = []
                for item in items:
                    # Корректный поиск элементов без ошибочного приведения bool(elem)
                    title_elem = find_xml_elem(item, ["title", "{http://www.w3.org/2005/Atom}title"])
                    link_elem = find_xml_elem(item, ["link", "{http://www.w3.org/2005/Atom}link"])
                    desc_elem = find_xml_elem(item, [
                        "{http://turbo.yandex.ru}content",
                        "{http://purl.org/rss/1.0/modules/content/}encoded",
                        "description",
                        "{http://www.w3.org/2005/Atom}summary",
                        "{http://www.w3.org/2005/Atom}content"
                    ])

                    if title_elem is None or link_elem is None:
                        continue

                    title = title_elem.text.strip() if (title_elem is not None and title_elem.text) else ""
                    link = link_elem.text.strip() if (link_elem is not None and link_elem.text) else link_elem.attrib.get("href", "").strip()
                    
                    if not link or not title or is_already_published(link):
                        continue

                    raw_desc = desc_elem.text if (desc_elem is not None and desc_elem.text) else ""
                    clean_desc = BeautifulSoup(raw_desc, "html.parser").get_text(separator=" ", strip=True) if raw_desc else ""

                    # Быстрая проверка на стоп-слова и релевантность по заголовку и описанию
                    relevant, score = is_relevant_ru_article(title, clean_desc)
                    if not relevant:
                        continue

                    article_text = clean_desc
                    # Если описание в RSS короткое (<450 симв.), пробуем подгрузить текст напрямую со страницы статьи
                    if len(article_text) < 450:
                        try:
                            art_resp = requests.get(link, headers=headers, timeout=5)
                            if art_resp.status_code == 200:
                                art_soup = BeautifulSoup(art_resp.content, "html.parser")
                                # Убираем скрипты, стили, навигацию, формы и шапки/подвалы
                                for tag in art_soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
                                    tag.decompose()
                                # Ищем блок с текстом статьи
                                content_box = (
                                    art_soup.find("article") or
                                    art_soup.find(class_=lambda c: c and any(k in str(c).lower() for k in ["entry-content", "post-content", "body_container", "article-body", "article__body", "article-content", "main-content"])) or
                                    art_soup
                                )
                                paras = [p.get_text(separator=" ", strip=True) for p in content_box.find_all("p") if len(p.get_text(strip=True)) > 40]
                                if paras:
                                    article_text = "\n".join(paras[:8])
                        except Exception:
                            pass

                    # Удаление скрытых служебных символов и нормализация пробелов
                    article_text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', article_text).strip()

                    if len(article_text) < 60:
                        continue

                    # Повторная строгая валидация с учетом полного текста
                    relevant, score = is_relevant_ru_article(title, article_text)
                    if not relevant:
                        continue

                    candidates.append({
                        "score": score,
                        "data": {
                            "id": link,
                            "title": title,
                            "journal": j["name"],
                            "category": j["category"],
                            "content": f"Заголовок: {title}\nИздание: {j['name']}\n\nТекст статьи:\n{article_text[:2800]}",
                            "url": link,
                            "hashtags": j.get("hashtags", "#Липидограм_Наука #Доказательно #Кардиология")
                        }
                    })

                if candidates:
                    # Приоритет: сначала материалы по кардиологии / холестерину / сосудам (score=2), затем общемедицинские (score=1)
                    candidates.sort(key=lambda x: x["score"], reverse=True)
                    best_score = candidates[0]["score"]
                    top_pool = [c["data"] for c in candidates if c["score"] == best_score]
                    return random.choice(top_pool)
        except Exception as e:
            logging.warning(f"Ошибка парсинга журнала {j['name']}: {e}")
            continue

    if target_source:
        return None

    return fetch_rko_news()

# ФУНКЦИЯ ДИАГНОСТИКИ И АДРЕСНОЙ ПРОВЕРКИ ВСЕХ РОССИЙСКИХ ИСТОЧНИКОВ
def check_russian_journals_status() -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    results = []

    for j in RU_JOURNALS_RSS:
        name = j["name"]
        j_id = j["id"]

        if j.get("type") == "biomolecula":
            t0 = time.time()
            try:
                req = requests.get("https://biomolecula.ru/articles", headers=headers, timeout=6)
                elapsed = time.time() - t0
                if req.status_code == 200:
                    slugs = re.findall(r'href=[\"\'](/articles/[a-z0-9\-]+)[\"\']', req.text)
                    valid = [s for s in set(slugs) if s not in ['/articles/top', '/articles/archive']]
                    results.append(
                        f"🟢 <b>{name}</b> (скрапер HTML)\n"
                        f"• Статус: Доступен (HTTP 200, {elapsed:.2f}с)\n"
                        f"• Найдено статей на витрине: {len(valid)}\n"
                        f"• Адресная команда: <code>/test_ru bio</code> или <code>/test_bio</code>\n"
                    )
                else:
                    results.append(f"🔴 <b>{name}</b>: HTTP {req.status_code} ({elapsed:.2f}с)\n")
            except Exception as e:
                results.append(f"🔴 <b>{name}</b>: Ошибка сети ({e})\n")
            continue

        t0 = time.time()
        timeout_sec = j.get("timeout", 5)
        try:
            resp = requests.get(j["rss"], headers=headers, timeout=timeout_sec)
            elapsed = time.time() - t0
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                items = root.findall(".//item")
                if not items:
                    items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

                if items:
                    sample_item = items[0]
                    t_elem = find_xml_elem(sample_item, ["title", "{http://www.w3.org/2005/Atom}title"])
                    l_elem = find_xml_elem(sample_item, ["link", "{http://www.w3.org/2005/Atom}link"])
                    s_title = t_elem.text.strip() if (t_elem is not None and t_elem.text) else "Без названия"
                    s_link = l_elem.text.strip() if (l_elem is not None and l_elem.text) else ""

                    results.append(
                        f"🟢 <b>{name}</b> (RSS)\n"
                        f"• Статус: Отвечает ({elapsed:.2f}с, {len(items)} ст. в фиде)\n"
                        f"• Свежая статья: «{s_title[:55]}...»\n"
                        f"• Адресная команда: <code>/test_ru {j_id}</code>\n"
                    )
                else:
                    results.append(f"🟡 <b>{name}</b>: RSS пуст\n")
            else:
                results.append(f"🔴 <b>{name}</b>: HTTP {resp.status_code} ({elapsed:.2f}с)\n")
        except requests.Timeout:
            note = " [Исключен из авто-ротации]" if j.get("cloud_blocked") else ""
            results.append(
                f"🔴 <b>{name}</b> (RSS){note}\n"
                f"• Статус: Таймаут соединения ({timeout_sec}с).\n"
            )
        except Exception as e:
            results.append(f"🔴 <b>{name}</b>: Ошибка {e}\n")

    return "\n".join(results)

# ФИЛЬТРАЦИЯ ВИДЕО (БЕЗ АНОНСОВ И БЕЗ ПОВТОРОВ ИЗ ИСТОРИИ)
def fetch_global_youtube_video() -> dict:
    if random.random() < 0.75:
        primary_pool = list(RU_HEALTH_CHANNELS)
        secondary_pool = list(GLOBAL_HEALTH_CHANNELS)
    else:
        primary_pool = list(GLOBAL_HEALTH_CHANNELS)
        secondary_pool = list(RU_HEALTH_CHANNELS)

    random.shuffle(primary_pool)
    random.shuffle(secondary_pool)
    all_channels = primary_pool + secondary_pool

    UPCOMING_STOP_WORDS = [
        "трансляция начнется", "премьера через", "прямой эфир начнется", "вебинар состоится",
        "live in", "scheduled for", "upcoming", "premieres in", "скоро начнется", "анонс вебинара"
    ]

    for channel in all_channels:
        try:
            channel_id = channel.get("channel_id")
            handle = channel.get("handle", "")
            
            rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            req = requests.get(rss_url, headers=headers, timeout=10)
            
            if req.status_code != 200 and handle:
                clean_h = handle.replace("@", "")
                rss_url = f"https://www.youtube.com/feeds/videos.xml?user={clean_h}"
                req = requests.get(rss_url, headers=headers, timeout=10)

            if req.status_code == 200:
                root = ET.fromstring(req.content)
                ns = {
                    "atom": "http://www.w3.org/2005/Atom",
                    "yt": "http://www.youtube.com/xml/schemas/2015",
                    "media": "http://search.yahoo.com/mrss/"
                }

                entries = root.findall("atom:entry", ns)
                if not entries:
                    continue

                random.shuffle(entries)

                for entry in entries:
                    vid_elem = entry.find("yt:videoId", ns)
                    title_elem = entry.find("atom:title", ns)
                    
                    if vid_elem is None or title_elem is None:
                        continue

                    vid = vid_elem.text
                    title = title_elem.text
                    video_direct_url = f"https://www.youtube.com/watch?v={vid}"

                    if is_already_published(vid) or is_already_published(video_direct_url):
                        continue

                    desc_elem = entry.find(".//media:description", ns)
                    description = desc_elem.text if desc_elem is not None else ""
                    combined_text = f"{title} {description}".lower()

                    if any(stop_w in combined_text for stop_w in UPCOMING_STOP_WORDS):
                        continue

                    is_rko = "scardio" in channel.get("name", "").lower() or "scardioru" in handle.lower()
                    if not is_rko and not any(kw in combined_text for kw in HEALTH_KEYWORDS):
                        continue

                    transcript_text = ""
                    has_real_transcript = False
                    try:
                        transcript_list = YouTubeTranscriptApi.get_transcript(vid, languages=['ru', 'en', 'en-US'])
                        if transcript_list:
                            transcript_text = " ".join([t['text'] for t in transcript_list[:140]])
                            has_real_transcript = True
                    except Exception:
                        transcript_text = description

                    if not has_real_transcript and len(description.strip()) < 120:
                        continue

                    content_for_prompt = f"Название видео: {title}\nКанал: {channel['name']}\n\nТекст / Описание видео:\n{transcript_text[:3000]}"
                    
                    if len(transcript_text) > 80:
                        yt_img = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
                        return {
                            "id": vid,
                            "title": title,
                            "journal": channel['name'],
                            "content": content_for_prompt,
                            "image_url": yt_img,
                            "url": video_direct_url
                        }
        except Exception as e:
            logging.warning(f"Ошибка получения RSS для {channel['name']}: {e}")
            continue

    return fetch_pubmed_study_with_abstract('("Cardiovascular" OR "Atherosclerosis") AND ("Clinical Review" OR "Lecture")')

def decode_mime_words(s):
    if not s:
        return ""
    decoded_parts = decode_header(s)
    result = []
    for part, enc in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="ignore"))
        else:
            result.append(part)
    return "".join(result)

def fetch_rko_from_email() -> dict:
    if not (EMAIL_USER and EMAIL_PASS):
        return None
    try:
        mail = imaplib.IMAP4_SSL(EMAIL_HOST)
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("inbox")

        status, messages = mail.search(None, '(OR (FROM "scardio") (SUBJECT "РКО"))')
        if status != "OK" or not messages[0]:
            status, messages = mail.search(None, 'UNSEEN')
            
        if not messages[0]:
            mail.logout()
            return None

        latest_id = messages[0].split()[-1]
        res, msg_data = mail.fetch(latest_id, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])

        subject = decode_mime_words(msg["Subject"])
        body = ""
        found_links = []

        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                elif ctype == "text/html":
                    soup = BeautifulSoup(part.get_payload(decode=True).decode("utf-8", errors="ignore"), "html.parser")
                    body += soup.get_text(separator="\n", strip=True)
                    for a in soup.find_all("a", href=True):
                        href = a["href"]
                        if "scardio.ru" in href or "http" in href:
                            found_links.append(href)
        else:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

        mail.store(latest_id, '+FLAGS', '\\Seen')
        mail.logout()

        target_url = found_links[0] if found_links else "https://scardio.ru/news/novosti_obschestva/"
        if is_already_published(target_url):
            return None

        return {
            "id": target_url,
            "title": subject,
            "journal": "Официальная рассылка Российского кардиологического общества (РКО)",
            "year": "2025-2026",
            "content": body[:2500],
            "url": target_url
        }
    except Exception as e:
        logging.warning(f"Ошибка проверки почты РКО: {e}")
        return None

def fetch_rko_news() -> dict:
    email_data = fetch_rko_from_email()
    if email_data:
        return email_data

    base_section_url = "https://scardio.ru/news/novosti_obschestva/"
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(base_section_url, headers=headers, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "html.parser")
            links = soup.find_all("a", href=True)
            valid_news = []
            for a in links:
                href = a["href"]
                title = a.get_text(strip=True)
                if "/news/novosti_obschestva/" in href and len(title) > 20 and href != "/news/novosti_obschestva/":
                    full_url = f"https://scardio.ru{href}" if href.startswith("/") else href
                    if not is_already_published(full_url):
                        valid_news.append({"title": title, "url": full_url})
            
            if valid_news:
                selected = random.choice(valid_news[:10])
                content_desc = selected["title"]
                try:
                    art_resp = requests.get(selected["url"], headers=headers, timeout=6)
                    if art_resp.status_code == 200:
                        art_soup = BeautifulSoup(art_resp.content, "html.parser")
                        paragraphs = [p.get_text(strip=True) for p in art_soup.find_all("p") if len(p.get_text(strip=True)) > 30]
                        if paragraphs:
                            content_desc = "\n".join(paragraphs[:4])
                except Exception:
                    pass

                return {
                    "id": selected["url"],
                    "title": selected["title"],
                    "journal": "Российское кардиологическое общество (РКО)",
                    "year": "2025-2026",
                    "content": f"Заголовок: {selected['title']}\nТекст новости:\n{content_desc}",
                    "url": selected["url"]
                }
    except Exception as e:
        logging.warning(f"Ошибка новостей РКО: {e}")

    return fetch_pubmed_study_with_abstract('("Russian" OR "guidelines") AND ("cardiology" OR "dyslipidemia")')

# ПОИСК PUBMED С ФИЛЬТРОМ ИСТОРИИ
def fetch_pubmed_study_with_abstract(query: str) -> dict:
    try:
        search_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={urllib.parse.quote(query)}&mindate=2023/01/01&maxdate=2026/12/31&retmax=25&sort=pub_date&retmode=json"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(search_url, headers=headers, timeout=8)
        if res.status_code == 200:
            data = res.json()
            id_list = data.get("esearchresult", {}).get("idlist", [])

            if id_list:
                random.shuffle(id_list)
                for pmid in id_list:
                    if is_already_published(f"pmid_{pmid}"):
                        continue

                    fetch_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={pmid}&retmode=xml"
                    xml_res = requests.get(fetch_url, headers=headers, timeout=8)
                    if xml_res.status_code == 200:
                        root = ET.fromstring(xml_res.content)
                        article = root.find(".//Article")
                        if article is None:
                            continue

                        title_elem = article.find("ArticleTitle")
                        title = "".join(title_elem.itertext()) if title_elem is not None else "Cardiovascular Study"

                        journal_elem = article.find(".//Journal/Title")
                        journal = journal_elem.text if journal_elem is not None else "PubMed"

                        year_elem = article.find(".//JournalIssue/PubDate/Year")
                        year = year_elem.text if year_elem is not None else "2024-2026"

                        abstract_texts = root.findall(".//Abstract/AbstractText")
                        if not abstract_texts:
                            continue

                        abstract = "\n".join(["".join(elem.itertext()) for elem in abstract_texts if elem is not None])
                        if len(abstract) < 100:
                            continue

                        return {
                            "id": f"pmid_{pmid}",
                            "pmid": pmid,
                            "title": title,
                            "journal": journal,
                            "year": year,
                            "content": f"Title: {title}\nJournal: {journal} ({year})\nAbstract:\n{abstract[:2800]}",
                            "abstract": abstract[:2800],
                            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                        }
    except Exception as e:
        logging.error(f"Ошибка PubMed API: {e}")
    return None

def sanitize_html_for_telegram(text: str) -> str:
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)

    allowed_tags = ['<b>', '</b>', '<i>', '</i>', '<code>', '</code>', '</a>']
    placeholders = {}
    
    def repl_tag(match):
        key = f"__TAG_{len(placeholders)}__"
        placeholders[key] = match.group(0)
        return key
    
    text = re.sub(r'<a\s+href=["\'][^"\']+["\']>', repl_tag, text, flags=re.IGNORECASE)
    for tag in allowed_tags:
        text = re.sub(re.escape(tag), repl_tag, text, flags=re.IGNORECASE)
    
    text = text.replace('<', '&lt;').replace('>', '&gt;')
    for key, val in placeholders.items():
        text = text.replace(key, val)
    return text

def pick_rubric_by_schedule() -> dict:
    now = datetime.now()
    week_number = now.isocalendar().week
    weekday = now.weekday()
    hour = now.hour

    if week_number % 2 == 1:
        if hour < 14:
            morning_plan = [RUBRIC_ACADEMIC_SCIENCE, RUBRIC_YOUTUBE, RUBRIC_RU_JOURNALS, RUBRIC_YOUTUBE, RUBRIC_ACADEMIC_SCIENCE, RUBRIC_SPORT, RUBRIC_YOUTUBE]
            return morning_plan[weekday % len(morning_plan)]
        else:
            evening_plan = [RUBRIC_RECIPES, RUBRIC_MYTHS, RUBRIC_SPORT, RUBRIC_MYTHS, RUBRIC_RECIPES, RUBRIC_MYTHS, RUBRIC_RECIPES]
            return evening_plan[weekday % len(evening_plan)]
    else:
        if hour < 14:
            morning_plan = [RUBRIC_RU_JOURNALS, RUBRIC_ACADEMIC_SCIENCE, RUBRIC_YOUTUBE, RUBRIC_RU_JOURNALS, RUBRIC_SPORT, RUBRIC_YOUTUBE, RUBRIC_ACADEMIC_SCIENCE]
            return morning_plan[weekday % len(morning_plan)]
        else:
            evening_plan = [RUBRIC_MYTHS, RUBRIC_RECIPES, RUBRIC_MYTHS, RUBRIC_RECIPES, RUBRIC_SPORT, RUBRIC_RECIPES, RUBRIC_MYTHS]
            return evening_plan[weekday % len(evening_plan)]

async def generate_and_publish_post(custom_rubric: dict = None, with_image: bool = True, source_mode: str = "auto", target_source: str = None) -> tuple[bool, str]:
    if not KIE_KEY:
        err = "KIE_API_KEY не установлен в переменных Render!"
        logging.error(err)
        return False, err

    if not bot_poster:
        err = "Бот для отправки не настроен!"
        logging.error(err)
        return False, err

    if custom_rubric:
        rubric = custom_rubric
    else:
        rubric = random.choice(ALL_RUBRICS_POOL) if source_mode == "random" else pick_rubric_by_schedule()

    style = rubric.get("style_type", "expert_review")
    img_bytes = None
    study_id = None

    if source_mode == "ru_journals" or rubric.get("source_type") == "ru_journals":
        study = fetch_russian_journals_rss(target_source=target_source)
        if not study:
            src_str = f" для «{target_source}»" if target_source else ""
            return False, f"Не удалось получить материал из российских изданий{src_str}. Возможно, источник временно недоступен."

        study_id = study.get("id") or study.get("url")
        category = study.get("category", "🔬 РОССИЙСКАЯ ДОКАЗАТЕЛЬНАЯ МЕДИЦИНА")
        hashtags = study.get("hashtags", rubric['hashtags'])
        prompt = (
            f"Напиши пост в стиле «{style}» для Telegram-канала «Липидограм» в рубрику «{category}».\n"
            f"МАТЕРИАЛ ИЗ РОССИЙСКОГО НАУЧНОГО ИЗДАНИЯ:\n{study.get('content', '')}\n\n"
            f"В блоке Первоисточник поставь ТОЧНО эту ссылку на статью: <a href='{study['url']}'>{study['title']} ({study['journal']})</a>.\n"
            f"В самом конце добавь хештеги: {hashtags}"
        )
    elif rubric.get("source_type") == "recipe_source":
        study = fetch_real_cardio_recipe()
        study_id = study.get("id") or study.get("url")
        prompt = (
            f"Напиши карточку полезного блюда в стиле «{style}» для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
            f"РЕАЛЬНЫЙ РЕЦЕПТ ИЗ ИСТОЧНИКА:\n{study.get('content', '')}\n\n"
            f"В блоке Первоисточник поставь ТОЧНО эту ссылку: <a href='{study['url']}'>{study['title']} / {study['journal']}</a>.\n"
            f"В самом конце добавь хештеги: {rubric['hashtags']}"
        )
    elif rubric.get("source_type") == "youtube":
        study = fetch_global_youtube_video()
        study_id = study.get("id") or study.get("url")
        if with_image and study.get("image_url"):
            try:
                async with aiohttp.ClientSession() as yt_s:
                    async with yt_s.get(study["image_url"], timeout=aiohttp.ClientTimeout(total=8)) as yt_r:
                        if yt_r.status == 200:
                            img_bytes = await yt_r.read()
            except Exception:
                pass

        prompt = (
            f"Напиши яркий пост в стиле «{style}» для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
            f"ДАННЫЕ РЕАЛЬНОГО ВИДЕОРОЛИКА ЭКСПЕРТА:\n{study.get('content', '')}\n\n"
            f"В блоке Первоисточник поставь ТОЧНО эту ссылку на конкретное видео: <a href='{study['url']}'>{study['title']} ({study['journal']})</a>.\n"
            f"В самом конце добавь хештеги: {rubric['hashtags']}"
        )
    elif rubric.get("source_type") == "rko":
        study = fetch_rko_news()
        study_id = study.get("id") or study.get("url")
        prompt = (
            f"Напиши понятный и актуальный пост в стиле «{style}» для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
            f"МАТЕРИАЛ ПЕРВОИСТОЧНИКА:\n{study.get('content', '')}\n\n"
            f"В блоке Первоисточник поставь ТОЧНО эту ссылку: <a href='{study['url']}'>{study['title']} ({study['journal']})</a>.\n"
            f"В самом конце добавь хештеги: {rubric['hashtags']}"
        )
    else:
        study = fetch_pubmed_study_with_abstract(rubric['query'])
        if study:
            study_id = study.get("id") or study.get("pmid")
            prompt = (
                f"Напиши пост в стиле «{style}» для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
                f"Тема: {rubric['ru_theme']}\n\n"
                f"РЕАЛЬНОЕ ИССЛЕДОВАНИЕ PUBMED:\n{study.get('content', '')}\n\n"
                f"В блоке Первоисточник поставь ТОЧНО эту ссылку: <a href='{study['url']}'>{study['title']} / {study['journal']}</a>.\n"
                f"В самом конце добавь хештеги: {rubric['hashtags']}"
            )
        else:
            return False, "Не удалось получить первоисточник PubMed."

    try:
        post_text, image_prompt = generate_kie_text_and_prompt(prompt)
        if not post_text:
            return False, "Ошибка: модель вернула пустой текст поста."
    except Exception as e:
        return False, f"Ошибка генерации текста: {e}"

    if with_image and not img_bytes and image_prompt:
        img_bytes = await generate_kie_image_bytes(image_prompt)

    try:
        clean_html = sanitize_html_for_telegram(post_text)

        if img_bytes and len(img_bytes) > 2000:
            photo_file = BufferedInputFile(img_bytes, filename="lipidogram_post.jpg")
            
            if len(clean_html) <= 1024:
                sent_msg = await bot_poster.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=photo_file,
                    caption=clean_html,
                    parse_mode="HTML"
                )
            else:
                await bot_poster.send_photo(chat_id=CHANNEL_ID, photo=photo_file)
                sent_msg = await bot_poster.send_message(
                    chat_id=CHANNEL_ID,
                    text=clean_html,
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )
            
            if study_id:
                mark_as_published(study_id)
            logging.info(f"Пост с фото опубликован! ID: {sent_msg.message_id}")
            return True, f"Опубликован пост («{rubric['category']}») с иллюстрацией Nano Banana!"

        sent_msg = await bot_poster.send_message(
            chat_id=CHANNEL_ID,
            text=clean_html,
            parse_mode="HTML",
            disable_web_page_preview=False
        )
        if study_id:
            mark_as_published(study_id)
        return True, f"Опубликован пост («{rubric['category']}») без фото."
    except Exception as e:
        return False, f"Ошибка отправки в Telegram: {e}"

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.reply(
        "🫀 <b>Медиа-бот «Липидограм» (@lipidogram)</b>\n\n"
        "🧪 <b>СКВОЗНОЕ ТЕСТИРОВАНИЕ ВСЕХ ИСТОЧНИКОВ:</b>\n"
        "• <b>/test_all</b> — 🚀 <b>последовательно протестировать ВСЕ рубрики и ВСЕ 11 источников</b> канала по очереди (PubMed, Мифы, Спорт, Рецепты, YouTube, Биомолекула, Зожник, Хабр Здоровье, Хабр Биотех, N+1, Naked Science).\n\n"
        "🎲 <b>СЛУЧАЙНЫЙ ПОСТ ИЗ ЛЮБОЙ РУБРИКИ И ИСТОЧНИКА:</b>\n"
        "• <b>/test_random</b> — 🧪 быстрый тест <b>БЕЗ картинки</b> (PubMed, РосЖурналы, YouTube, Рецепты, Мифы, Спорт). 0 кредитов, 2 сек.\n"
        "• <b>/post_random</b> — 🎨 полный пост <b>С генерацией арта</b> Nano Banana 2 Lite из случайного источника.\n\n"
        "<b>📌 Тематические команды БЕЗ картинки (быстро, 0 кредитов):</b>\n"
        "• /test_pubmed — свежее исследование PubMed (мета-анализы, липиды)\n"
        "• /test_myth — разбор мифа через PubMed\n"
        "• /test_sport — активность и сосуды через PubMed\n"
        "• /test_recipe — гиполипидемический рецепт\n"
        "• /test_youtube — выжимка видео (РКО, Утин, Гаглошвили, Attia)\n"
        "• /test_ru — случайный российский журнал (Биомолекула, Зожник, Хабр, N+1, Naked Science)\n"
        "• /test_ru_status — 🔎 <b>диагностика и статус связи со всеми российскими журналами</b>\n\n"
        "<b>🎯 Адресная проверка российских журналов:</b>\n"
        "• <code>/test_ru zozhnik</code> (или /test_zozhnik) — тест «Зожник»\n"
        "• <code>/test_ru habr</code> (или /test_habr) — тест «Хабр Здоровье»\n"
        "• <code>/test_ru biotech</code> (или /test_biotech) — тест «Хабр Биотех»\n"
        "• <code>/test_ru n1</code> (или /test_nplus1) — тест «N+1»\n"
        "• <code>/test_ru ns</code> (или /test_naked) — тест «Naked Science (Медицина)»\n"
        "• <code>/test_ru bio</code> (или /test_bio) — тест «Биомолекула»\n\n"
        "<b>🖼 Публикации С картинкой Nano Banana 2 Lite:</b>\n"
        "• /post_now — по расписанию дня\n"
        "• /post_pubmed — исследование PubMed с иллюстрацией\n"
        "• /post_ru — российские журналы с иллюстрацией (можно указать источник: <code>/post_ru zozhnik</code>)\n\n"
        "🛡️ <b>Модерация комментариев:</b>\n"
        "• <b>/mod_status</b> — 🔎 <b>проверить статус бота-модератора</b> и его права в группе комментариев",
        parse_mode="HTML"
    )

# --- Команды с полной генерацией арта ---
@dp.message(Command("post_now"))
async def cmd_post_now(message: types.Message):
    await message.reply("⏳ Gemini 3.7 формирует пост по расписанию недели, Nano Banana создает арт...")
    success, res = await generate_and_publish_post(with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_random"))
async def cmd_post_random(message: types.Message):
    await message.reply("🎲 Выбираю случайный источник (PubMed, РосЖурналы, YouTube, Рецепты, Мифы), пишу пост и генерирую арт...")
    success, res = await generate_and_publish_post(with_image=True, source_mode="random")
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_pubmed"))
async def cmd_post_pubmed(message: types.Message):
    await message.reply("🔬 Ищу исследование в PubMed и создаю арт...")
    success, res = await generate_and_publish_post(RUBRIC_ACADEMIC_SCIENCE, with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_recipe"))
async def cmd_post_rec(message: types.Message):
    await message.reply("🥗 Ищу реальный рецепт в источниках и создаю фото блюда...")
    success, res = await generate_and_publish_post(RUBRIC_RECIPES, with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_ru"))
async def cmd_post_ru(message: types.Message):
    args = message.text.split()[1:] if message.text else []
    target = args[0].strip().lower() if args else None
    label = f" ({target})" if target else ""
    await message.reply(f"🇷🇺 Анализирую российские научные журналы{label} и создаю арт...")
    success, res = await generate_and_publish_post(with_image=True, source_mode="ru_journals", target_source=target)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_youtube"))
async def cmd_post_yt(message: types.Message):
    await message.reply("🎬 Забираю свежее видео через RSS экспертов и создаю пост с превью...")
    success, res = await generate_and_publish_post(RUBRIC_YOUTUBE, with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_myth"))
async def cmd_post_my(message: types.Message):
    await message.reply("💡 Gemini 3.7 развенчивает миф, Nano Banana генерирует арт...")
    success, res = await generate_and_publish_post(RUBRIC_MYTHS, with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

# --- СПИСОК ВСЕХ РУБРИК И ИСТОЧНИКОВ ДЛЯ СКВОЗНОГО ТЕСТИРОВАНИЯ ---
ALL_TEST_CASES = [
    {"name": "🔬 PubMed (Клинический дайджест: кардиология и липиды)", "type": "rubric", "rubric": RUBRIC_ACADEMIC_SCIENCE},
    {"name": "💡 PubMed (Разбор мифа доказательной медициной)", "type": "rubric", "rubric": RUBRIC_MYTHS},
    {"name": "🏃 PubMed (Физическая активность и эластичность сосудов)", "type": "rubric", "rubric": RUBRIC_SPORT},
    {"name": "🥗 Гиполипидемическая кухня (Рецепты для снижения ЛПНП)", "type": "rubric", "rubric": RUBRIC_RECIPES},
    {"name": "📺 Научпоп и эксперты (YouTube-видео кардиологов)", "type": "rubric", "rubric": RUBRIC_YOUTUBE},
    {"name": "🇷🇺 Российские издания: «Биомолекула»", "type": "ru", "source": "biomolecula"},
    {"name": "🇷🇺 Российские издания: «Зожник»", "type": "ru", "source": "zozhnik"},
    {"name": "🇷🇺 Российские издания: «Хабр Здоровье»", "type": "ru", "source": "habr_health"},
    {"name": "🇷🇺 Российские издания: «Хабр Биотех»", "type": "ru", "source": "habr_biotech"},
    {"name": "🇷🇺 Российские издания: «N+1»", "type": "ru", "source": "nplus1"},
    {"name": "🇷🇺 Российские издания: «Naked Science (Медицина)»", "type": "ru", "source": "naked_science"}
]

# --- СКВОЗНОЙ ТЕСТ ВСЕХ РУБРИК И ВСЕХ ИСТОЧНИКОВ ПО ОЧЕРЕДИ ---
@dp.message(Command("test_all", "test_all_sources", "test_everything", "test_suite"))
async def cmd_test_all(message: types.Message):
    total = len(ALL_TEST_CASES)
    status_msg = await message.reply(
        f"🚀 <b>Запуск сквозного тестирования ВСЕХ {total} рубрик и источников!</b>\n\n"
        f"<i>Тестирование выполняется последовательно через Gemini 3.7 Flash без генерации картинок (0 кредитов KIE, безопасно для баланса).</i>\n\n"
        f"⏳ Начинаю проверку 1/{total}...",
        parse_mode="HTML"
    )

    success_count = 0
    fail_count = 0
    results_log = []

    for idx, tc in enumerate(ALL_TEST_CASES, start=1):
        name = tc["name"]
        logging.info(f"Сквозной тест [{idx}/{total}]: {name}")
        
        try:
            if tc["type"] == "rubric":
                success, res = await generate_and_publish_post(tc["rubric"], with_image=False)
            else:
                success, res = await generate_and_publish_post(with_image=False, source_mode="ru_journals", target_source=tc["source"])
        except Exception as e:
            success, res = False, str(e)

        if success:
            success_count += 1
            icon = "✅"
            short_res = "Успешно опубликован"
        else:
            fail_count += 1
            icon = "❌"
            short_res = res[:90]

        log_line = f"{icon} <b>[{idx}/{total}]</b> {name}: {short_res}"
        results_log.append(log_line)

        # Отправляем отдельное короткое сообщение по каждому источнику в чат
        await message.reply(log_line, parse_mode="HTML")

        # Обновляем статусное сообщение
        try:
            await status_msg.edit_text(
                f"🚀 <b>Сквозное тестирование: выполнено {idx}/{total}</b>\n\n"
                f"• Успешно: ✅ {success_count}\n"
                f"• Ошибок: ❌ {fail_count}\n\n"
                f"⏳ Следующий источник...",
                parse_mode="HTML"
            )
        except Exception:
            pass

        if idx < total:
            await asyncio.sleep(2)

    await message.reply(
        f"🏁 <b>Сквозное тестирование ВСЕХ {total} рубрик и источников завершено!</b>\n\n"
        f"📊 <b>ИТОГИ ПРОВЕРКИ:</b>\n"
        f"• Всего источников: {total}\n"
        f"• Успешно: ✅ <b>{success_count}</b>\n"
        f"• С ошибками: ❌ <b>{fail_count}</b>\n\n"
        f"<b>Детальный отчет:</b>\n" + "\n".join(results_log),
        parse_mode="HTML"
    )

# --- Тестовые команды БЕЗ генерации картинки (0 кредитов KIE, быстро) ---
@dp.message(Command("test_random", "test_any", "test_text"))
async def cmd_test_random(message: types.Message):
    await message.reply("⚡ 🎲 Запуск генерации из случайного источника (PubMed / РосЖурналы / YouTube / Рецепты / Мифы) без картинки...")
    success, res = await generate_and_publish_post(with_image=False, source_mode="random")
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_pubmed"))
async def cmd_test_pubmed(message: types.Message):
    await message.reply("⚡ Ищу свежее клиническое исследование в PubMed (без картинки)...")
    success, res = await generate_and_publish_post(RUBRIC_ACADEMIC_SCIENCE, with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_sport", "test_activity"))
async def cmd_test_sport(message: types.Message):
    await message.reply("⚡ Ищу исследование по физической активности и сосудам в PubMed (без картинки)...")
    success, res = await generate_and_publish_post(RUBRIC_SPORT, with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_recipe"))
async def cmd_test_rec(message: types.Message):
    await message.reply("⚡ Ищу реальный рецепт из первоисточников без арта...")
    success, res = await generate_and_publish_post(RUBRIC_RECIPES, with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

# Команда /test_ru с поддержкой аргументов: /test_ru zozhnik, /test_ru habr, /test_ru bio, /test_ru n1
@dp.message(Command("test_ru"))
async def cmd_test_ru(message: types.Message):
    args = message.text.split()[1:] if message.text else []
    target = args[0].strip().lower() if args else None

    target_labels = {
        "zozhnik": "«Зожник»",
        "зожник": "«Зожник»",
        "zozh": "«Зожник»",
        "habr": "«Хабр Здоровье»",
        "хабр": "«Хабр Здоровье»",
        "habr_health": "«Хабр Здоровье»",
        "biotech": "«Хабр Биотех»",
        "биотех": "«Хабр Биотех»",
        "habr_biotech": "«Хабр Биотех»",
        "nplus1": "«N+1»",
        "n1": "«N+1»",
        "н1": "«N+1»",
        "ns": "«Naked Science»",
        "naked": "«Naked Science»",
        "naked_science": "«Naked Science»",
        "нейкед": "«Naked Science»",
        "bio": "«Биомолекула»",
        "био": "«Биомолекула»",
        "biomolecula": "«Биомолекула»"
    }

    label = target_labels.get(target, f"«{target}»") if target else "случайный журнал (Биомолекула, Зожник, Хабр, N+1, Naked Science)"
    await message.reply(f"⚡ Анализирую российские журналы [{label}] (без арта)...")
    success, res = await generate_and_publish_post(with_image=False, source_mode="ru_journals", target_source=target)
    await message.reply("✅ " + res if success else "❌ " + res)

# Адресные команды-быстрые клавиши
@dp.message(Command("test_zozhnik", "test_zozh"))
async def cmd_test_zozhnik(message: types.Message):
    await message.reply("⚡ Запрашиваю статью из журнала «Зожник» (без арта)...")
    success, res = await generate_and_publish_post(with_image=False, source_mode="ru_journals", target_source="zozhnik")
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_habr"))
async def cmd_test_habr(message: types.Message):
    await message.reply("⚡ Запрашиваю статью из «Хабр Здоровье» (без арта)...")
    success, res = await generate_and_publish_post(with_image=False, source_mode="ru_journals", target_source="habr_health")
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_biotech"))
async def cmd_test_biotech(message: types.Message):
    await message.reply("⚡ Запрашиваю статью из «Хабр Биотех» (без арта)...")
    success, res = await generate_and_publish_post(with_image=False, source_mode="ru_journals", target_source="habr_biotech")
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_nplus1", "test_n1"))
async def cmd_test_nplus1(message: types.Message):
    await message.reply("⚡ Запрашиваю статью из «N+1» (без арта)...")
    success, res = await generate_and_publish_post(with_image=False, source_mode="ru_journals", target_source="nplus1")
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_naked", "test_ns"))
async def cmd_test_naked(message: types.Message):
    await message.reply("⚡ Запрашиваю статью из «Naked Science (Медицина)» (без арта)...")
    success, res = await generate_and_publish_post(with_image=False, source_mode="ru_journals", target_source="naked_science")
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_bio", "test_biomolecula"))
async def cmd_test_bio(message: types.Message):
    await message.reply("⚡ Запрашиваю статью из «Биомолекула» (без арта)...")
    success, res = await generate_and_publish_post(with_image=False, source_mode="ru_journals", target_source="biomolecula")
    await message.reply("✅ " + res if success else "❌ " + res)

# Команда комплексной диагностики статуса всех российских источников
@dp.message(Command("test_ru_status", "check_ru", "status_ru"))
async def cmd_test_ru_status(message: types.Message):
    await message.reply("🔎 Опрашиваю все российские источники (Биомолекула, Зожник, Хабр Здоровье, Хабр Биотех, N+1, Naked Science)...")
    report = check_russian_journals_status()
    await message.reply(
        f"📊 <b>ДИАГНОСТИКА РОССИЙСКИХ ИЗДАНИЙ:</b>\n\n{report}\n"
        f"💡 <i>Адресные команды проверки:</i>\n"
        f"• <code>/test_ru zozhnik</code> или <code>/test_zozhnik</code>\n"
        f"• <code>/test_ru habr</code> или <code>/test_habr</code>\n"
        f"• <code>/test_ru biotech</code> или <code>/test_biotech</code>\n"
        f"• <code>/test_ru n1</code> или <code>/test_nplus1</code>\n"
        f"• <code>/test_ru ns</code> или <code>/test_naked</code>\n"
        f"• <code>/test_ru bio</code> или <code>/test_bio</code>",
        parse_mode="HTML"
    )

@dp.message(Command("test_youtube"))
async def cmd_test_yt(message: types.Message):
    await message.reply("⚡ Забираю реальное видео эксперта через RSS (без арта)...")
    success, res = await generate_and_publish_post(RUBRIC_YOUTUBE, with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_myth"))
async def cmd_test_my(message: types.Message):
    await message.reply("⚡ Разбор мифа через Gemini 3.7 (без арта)...")
    success, res = await generate_and_publish_post(RUBRIC_MYTHS, with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

# --- ДИАГНОСТИКА И СТАТУС БОТА-МОДЕРАТОРА ---
@dp.message(Command("mod_status", "check_mod", "status_mod", "moderator"))
async def cmd_mod_status(message: types.Message):
    lines = ["🛡️ <b>СТАТУС И ДИАГНОСТИКА БОТА-МОДЕРАТОРА КОММЕНТАРИЕВ</b>\n"]

    if not bot_moderator:
        lines.append("❌ <b>Бот-модератор НЕ инициализирован!</b>\n"
                     "Ни одна из переменных токена (<code>TELEGRAM_BOT_TOKEN</code>, <code>MODERATOR_BOT_TOKEN</code>, <code>POSTER_BOT_TOKEN</code>) не задана.")
        await message.reply("\n".join(lines), parse_mode="HTML")
        return

    try:
        me = await bot_moderator.get_me()
        lines.append(f"🤖 <b>Бот:</b> @{me.username} (ID: <code>{me.id}</code>)")
        lines.append(f"📢 <b>Канал публикаций:</b> {CHANNEL_ID}")
        lines.append(f"💬 <b>Текущий чат:</b> {message.chat.type} (ID: <code>{message.chat.id}</code>)")
    except Exception as e:
        lines.append(f"⚠️ Ошибка получения данных бота через Telegram API: {e}")
        await message.reply("\n".join(lines), parse_mode="HTML")
        return

    if message.chat.type in ["group", "supergroup"]:
        try:
            bot_member = await bot_moderator.get_chat_member(message.chat.id, me.id)
            is_admin = bot_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
            can_delete = getattr(bot_member, "can_delete_messages", False)
            can_restrict = getattr(bot_member, "can_restrict_members", False)

            lines.append(f"\n<b>Права бота в этой группе обсуждений:</b>")
            lines.append(f"• Статус администратора: {'✅ ДА' if is_admin else '❌ НЕТ (добавьте бота в администраторы группы!)'}")
            lines.append(f"• Удаление сообщений: {'✅ ДА' if can_delete else '❌ НЕТ (включите тумблер «Удаление сообщений»)'}")
            lines.append(f"• Блокировка/мут спамеров: {'✅ ДА' if can_restrict else '❌ НЕТ (включите тумблер «Блокировка пользователей»)'}")

            if is_admin and can_delete and can_restrict:
                lines.append("\n🎉 <b>Модератор ПОЛНОСТЬЮ АКТИВЕН И РАБОТАЕТ!</b> Все сообщения и комментарии проверяются на спам и нецензурную лексику в реальном времени.")
            else:
                lines.append("\n⚠️ <b>ВНИМАНИЕ:</b> Чтобы бот мог модерировать чат, откройте: <i>Настройки группы ➔ Администраторы ➔ Добавить администратора ➔ выберите @{me.username}</i> и включите ему права на удаление сообщений и блокировку участников.")
        except Exception as e:
            lines.append(f"\n⚠️ Не удалось проверить права в чате: {e}")
    else:
        lines.append("\n💡 <i>Команда вызвана в личном диалоге с ботом. Чтобы проверить статус модерации комментариев канала, отправьте <code>/mod_status</code> прямо в привязанную группу обсуждений канала @lipidogram.</i>")

    lines.append("\n<b>Активные алгоритмы защиты:</b>")
    lines.append("• 🚫 Автоматический перехват нецензурной брани и оскорблений")
    lines.append("• 🚫 Блокировка сторонних ссылок, t.me-каналов и рекламных юзернеймов (ссылки на @lipidogram разрешены)")
    lines.append("• 🚫 Мониторинг как новых сообщений, так и отредактированных спамерами постов")
    lines.append("• ⚖️ Лестница санкций: 1-е нарушение — удаление + предупреждение (1/3) ➔ 2-е — мут на 24 часа (2/3) ➔ 3-е — бан (3/3)")

    await message.reply("\n".join(lines), parse_mode="HTML")

# --- МОДЕРАЦИЯ КОММЕНТАРИЕВ И СООБЩЕНИЙ В ГРУППЕ ОБСУЖДЕНИЙ ---
@dp.message(F.text | F.caption)
@dp.edited_message(F.text | F.caption)
async def handle_comment(message: types.Message):
    if message.chat.type == "private":
        return
    if message.sender_chat and message.sender_chat.type == "channel":
        return
    if not message.from_user:
        return

    # Пропускаем администраторов чата
    if bot_moderator:
        try:
            chat_member = await bot_moderator.get_chat_member(message.chat.id, message.from_user.id)
            if chat_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                return
        except Exception:
            pass

    raw_text = message.text or message.caption or ""
    text = raw_text.lower()
    user_id = message.from_user.id
    user_mention = message.from_user.mention_html()

    is_violation = False
    reason = ""

    if BAD_WORDS_PATTERN.search(text):
        is_violation = True
        reason = "нецензурная лексика / оскорбления"
    elif SPAM_LINKS_PATTERN.search(text) and "lipidogram" not in text:
        is_violation = True
        reason = "несогласованные ссылки / реклама"

    if is_violation and bot_moderator:
        try:
            await message.delete()
        except Exception as e:
            logging.error(f"Ошибка удаления сообщения модератором: {e}")

        warnings = user_warnings.get(user_id, 0) + 1
        user_warnings[user_id] = warnings

        if warnings == 1:
            try:
                await message.answer(
                    f"⚠️ {user_mention}, ваше сообщение удалено (причина: {reason}). Предупреждение: <b>1/3</b>.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        elif warnings == 2:
            until_date = datetime.now() + timedelta(days=1)
            try:
                await bot_moderator.restrict_chat_member(
                    chat_id=message.chat.id,
                    user_id=user_id,
                    permissions=types.ChatPermissions(can_send_messages=False),
                    until_date=until_date
                )
                await message.answer(
                    f"⛔ {user_mention} переведен в режим чтения на 24 часа. Предупреждение: <b>2/3</b>.",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Ошибка мута модератором: {e}")
        else:
            try:
                await bot_moderator.ban_chat_member(chat_id=message.chat.id, user_id=user_id)
                await message.answer(
                    f"🚫 {user_mention} заблокирован за систематическое нарушение правил (3/3).\n"
                    f"<i>ID: <code>{user_id}</code> | Разбан: <code>/unban {user_id}</code></i>",
                    parse_mode="HTML"
                )
                user_warnings.pop(user_id, None)
            except Exception as e:
                logging.error(f"Ошибка бана модератором: {e}")

# --- КОМАНДЫ УПРАВЛЕНИЯ БЛОКИРОВКАМИ И СНЯТИЯ НАКАЗАНИЙ ---
async def get_target_user_id(message: types.Message) -> int | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    parts = (message.text or "").strip().split()
    if len(parts) > 1 and parts[1].isdigit():
        return int(parts[1])
    return None

async def is_chat_admin(chat_id: int, user_id: int) -> bool:
    if not bot_moderator:
        return False
    try:
        member = await bot_moderator.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except Exception:
        return False

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if message.chat.type == "private":
        await message.reply("Эта команда предназначена для использования в группе обсуждений канала.")
        return

    if not await is_chat_admin(message.chat.id, message.from_user.id):
        await message.reply("⛔ Команда доступна только администраторам группы.")
        return

    target_id = await get_target_user_id(message)
    if not target_id:
        await message.reply(
            "ℹ️ <b>Как разбанить пользователя:</b>\n"
            "1. Ответьте командой <code>/unban</code> на любое сообщение пользователя\n"
            "2. Либо напишите с указанием ID: <code>/unban 123456789</code>",
            parse_mode="HTML"
        )
        return

    try:
        await bot_moderator.unban_chat_member(chat_id=message.chat.id, user_id=target_id, only_if_banned=True)
        user_warnings.pop(target_id, None)
        await message.reply(
            f"✅ <b>Пользователь (ID: <code>{target_id}</code>) успешно разблокирован!</b>\n"
            f"Блокировка снята, счетчик нарушений обнулен. Пользователь снова может вступить в чат.",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.reply(f"❌ Ошибка разблокировки: {e}")

@dp.message(Command("unmute"))
async def cmd_unmute(message: types.Message):
    if message.chat.type == "private":
        await message.reply("Эта команда предназначена для использования в группе обсуждений канала.")
        return

    if not await is_chat_admin(message.chat.id, message.from_user.id):
        await message.reply("⛔ Команда доступна только администраторам группы.")
        return

    target_id = await get_target_user_id(message)
    if not target_id:
        await message.reply(
            "ℹ️ <b>Как снять мут с пользователя:</b>\n"
            "1. Ответьте командой <code>/unmute</code> на сообщение пользователя\n"
            "2. Либо укажите его ID: <code>/unmute 123456789</code>",
            parse_mode="HTML"
        )
        return

    try:
        await bot_moderator.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_id,
            permissions=types.ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        user_warnings.pop(target_id, None)
        await message.reply(
            f"🔊 <b>Режим чтения с пользователя (ID: <code>{target_id}</code>) снят!</b>\n"
            f"Пользователь снова может писать сообщения и комментарии.",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.reply(f"❌ Ошибка снятия мута: {e}")

@dp.message(Command("clear_warns", "unwarn", "reset_warns"))
async def cmd_clear_warns(message: types.Message):
    if message.chat.type == "private":
        await message.reply("Эта команда предназначена для использования в группе обсуждений канала.")
        return

    if not await is_chat_admin(message.chat.id, message.from_user.id):
        await message.reply("⛔ Команда доступна только администраторам группы.")
        return

    target_id = await get_target_user_id(message)
    if not target_id:
        await message.reply(
            "ℹ️ Ответьте командой <code>/clear_warns</code> на сообщение или укажите ID: <code>/clear_warns 123456789</code>",
            parse_mode="HTML"
        )
        return

    prev_warns = user_warnings.pop(target_id, 0)
    await message.reply(
        f"🧹 <b>Предупреждения пользователя (ID: <code>{target_id}</code>) сброшены!</b>\n"
        f"Было предупреждений: {prev_warns}/3 ➔ стало: <b>0/3</b>.",
        parse_mode="HTML"
    )

async def handle_ping(request):
    return web.Response(text="Lipidogram Bot Service is live and active 24/7!")

async def run_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Веб-сервер слушает порт {PORT}")

async def main():
    await run_server()

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(generate_and_publish_post, "cron", hour=10, minute=0)
    scheduler.add_job(generate_and_publish_post, "cron", hour=18, minute=30)
    scheduler.start()

    logging.info("Служба расписания и боты успешно запущены на KIE.ai!")

    if bot_poster:
        if bot_moderator and bot_moderator != bot_poster:
            await dp.start_polling(bot_poster, bot_moderator)
        else:
            await dp.start_polling(bot_poster)
    else:
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
