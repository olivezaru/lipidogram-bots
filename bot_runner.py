import os
import re
import imaplib
import email
from email.header import decode_header
import asyncio
import logging
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from google import genai
from google.genai import types as genai_types

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

MODERATOR_TOKEN = os.getenv("MODERATOR_BOT_TOKEN", "").strip()
POSTER_TOKEN = os.getenv("POSTER_BOT_TOKEN", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "@lipidogram").strip()
PORT = int(os.getenv("PORT", 8080))

EMAIL_HOST = os.getenv("EMAIL_HOST", "imap.gmail.com").strip()
EMAIL_USER = os.getenv("EMAIL_USER", "").strip()
EMAIL_PASS = os.getenv("EMAIL_PASS", "").strip()

if not MODERATOR_TOKEN:
    logging.error("КРИТИЧЕСКАЯ ОШИБКА: MODERATOR_BOT_TOKEN не задан!")

bot_moderator = Bot(token=MODERATOR_TOKEN) if MODERATOR_TOKEN else None

if POSTER_TOKEN and POSTER_TOKEN != MODERATOR_TOKEN:
    bot_poster = Bot(token=POSTER_TOKEN)
else:
    bot_poster = bot_moderator

ai_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

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

GLOBAL_HEALTH_CHANNELS = [
    {"name": "Dr. Peter Attia (США, эксперт по долголетию и липидологии)", "handle": "@PeterAttiaMD"},
    {"name": "Huberman Lab (Стэнфорд, США)", "handle": "@hubermanlab"},
    {"name": "Dr. Gil Carvalho (Nutrition Made Simple, США)", "handle": "@NutritionMadeSimple"},
    {"name": "Dr. Rhonda Patrick (FoundMyFitness, США)", "handle": "@FoundMyFitness"},
    {"name": "Dr. Brad Stanfield (Новая Зеландия / США)", "handle": "@DrBradStanfield"},
    {"name": "Simon Hill / The Proof (Великобритания / Австралия)", "handle": "@TheProofWithSimonHill"},
    {"name": "Доктор Утин (кардиохирург Алексей Утин)", "handle": "@DoctorUtin"},
    {"name": "Кардиолог Тамаз Гаглошвили", "handle": "@doctor_tamaz"},
    {"name": "СМТ — Научный подход (Борис Цацулин)", "handle": "@CavemanTech"}
]

# Набор рубрик с акцентом на научпоп, лайфстайл и развлечения
RUBRIC_RECIPES = {
    "category": "🥗 ГИПОЛИПИДЕМИЧЕСКАЯ КУХНЯ / РЕЦЕПТ ДНЯ",
    "source_type": "pubmed",
    "query": '("dietary fiber" OR "beta-glucan" OR "legumes" OR "flaxseed") AND ("LDL cholesterol" OR "lipids") AND ("trial" OR "randomized")',
    "ru_theme": "Вкусный и легкий кулинарный рецепт для снижения ЛПНП (насыщенные жиры менее 1.5г, клетчатка более 6г, овсянка, нут, ягоды)",
    "hashtags": "#Рецепт_ЛПНП #УмнаяЗамена #ПитаниеСердца #Клетчатка"
}

RUBRIC_YOUTUBE = {
    "category": "📺 МИРОВОЙ НАУЧПОП / ВЫЖИМКА ИЗ ВИДЕО",
    "source_type": "youtube",
    "ru_theme": "Увлекательная и понятная выжимка из популярного видео мировых экспертов (Attia, Huberman, Утин, Rhonda Patrick)",
    "hashtags": "#Липидограм_Видео #Научпоп #Долголетие #ЗдоровьеСосудов"
}

RUBRIC_MYTHS = {
    "category": "💡 РАЗБОР МИФОВ И ЗАБЛУЖДЕНИЙ",
    "source_type": "pubmed",
    "query": '("dietary cholesterol" OR "eggs" OR "statins" OR "omega-3 fatty acids") AND ("atherosclerosis" OR "cardiovascular") AND ("meta-analysis" OR "systematic review")',
    "ru_theme": "Увлекательный разбор мифов простым языком (яйца и холестерин, статины, кофе, чистки сосудов, омега-3)",
    "hashtags": "#Мифы_Липидограм #Доказательно #Холестерин"
}

RUBRIC_SPORT = {
    "category": "🏃 СПОРТ, ЗОНА 2 И ЭНДОТЕЛИЙ",
    "source_type": "pubmed",
    "query": '("aerobic exercise" OR "resistance training" OR "interval training") AND ("flow-mediated dilation" OR "endothelial" OR "HDL-C" OR "lipid profile") AND ("trial" OR "randomized")',
    "ru_theme": "Простые советы по активности: пульсовая Зона 2, 8000 шагов, силовые для повышения защитного ЛПВП и молодости артерий",
    "hashtags": "#СпортИСосуды #ЗдоровьеСердца #Кардиотренировки #Эндотелий"
}

# Академическая рубрика (строго до 3 раз в неделю)
RUBRIC_ACADEMIC_SCIENCE = {
    "category": "🔬 НАУЧНЫЙ ДАЙДЖЕСТ (РКО / PUBMED)",
    "source_type": "rko",
    "query": "липиды холестерин",
    "ru_theme": "Клинические новости Российского кардиологического общества (РКО) и новейшие мета-анализы",
    "hashtags": "#Липидограм_Наука #РКО #Кардиология #ЛПНП"
}

SYSTEM_PROMPT = """
Ты — главный редактор и автор увлекательного русскоязычного Telegram-канала «Липидограм» (@lipidogram).
Твой стиль: дружелюбный, живой, позитивный, с легким юмором, без сложного академического занудства, но с безупречной доказательной точностью!

Правила оформления:
1. Заголовок: Цепляющий, с яркими тематическими эмодзи (в тегах <b>Заголовок</b>).
2. Введение: 1-2 предложения, почему это интересно и важно для каждого из нас.
3. Суть: 3-4 емких, живых тезиса с понятными цифрами, фактами и аналогиями.
4. Практический лайфхак/совет: Простое действие (в тарелке, на тренировке или в жизни).
5. Первоисточник: Кликабельная ссылка: <a href="ТОЧНЫЙ_URL">Смотреть источник / Видео / Статья</a>.
6. Хештеги в самом конце.

Используй только разрешенные теги Telegram: <b>, </b>, <i>, </i>, <code>, </code>, <a href="...">.
Знаки «меньше»/«больше» пиши словами («менее», «более») или экранируй (&lt; и &gt;).
"""

def fetch_global_youtube_video() -> dict:
    import random
    channel = random.choice(GLOBAL_HEALTH_CHANNELS)
    try:
        channel_url = f"https://www.youtube.com/{channel['handle']}/videos"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(channel_url, headers=headers, timeout=8)
        
        video_ids = []
        if resp.status_code == 200:
            matches = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
            video_ids = list(dict.fromkeys(matches))

        random.shuffle(video_ids)

        for vid in video_ids[:8]:
            try:
                transcript_list = YouTubeTranscriptApi.get_transcript(vid, languages=['en', 'en-US', 'ru'])
                full_text = " ".join([t['text'] for t in transcript_list])
                keywords = ["cholesterol", "ldl", "apob", "artery", "atherosclerosis", "heart", "diet", "zone 2", "exercise", "lipids", "statins", "omega-3", "холестерин", "сосуд", "сердц", "лпнп", "давлен", "питан", "статины", "жир", "тренировк", "спорт"]
                if len(full_text) > 400 and any(kw in full_text.lower() for kw in keywords):
                    return {
                        "title": f"Разбор эксперта: {channel['name']}",
                        "journal": f"YouTube-канал {channel['name']}",
                        "year": "2025-2026",
                        "content": full_text[:3500],
                        "url": f"https://www.youtube.com/watch?v={vid}"
                    }
            except Exception:
                continue
    except Exception as e:
        logging.warning(f"Ошибка YouTube {channel['name']}: {e}")

    return {
        "title": f"Популярный видеоразбор о здоровье сердца и сосудов",
        "journal": f"YouTube-канал {channel['name']}",
        "year": "2025-2026",
        "content": "Подробный разбор факторов риска, холестерина, тренировок 2-й пульсовой зоны и оптимизации питания.",
        "url": f"https://www.youtube.com/{channel['handle']}"
    }

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

        return {
            "title": subject,
            "journal": "Официальная рассылка Российского кардиологического общества (РКО)",
            "year": "2025-2026",
            "content": body[:2500],
            "url": found_links[0] if found_links else "https://scardio.ru/news/novosti_obschestva/"
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
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(base_section_url, headers=headers, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            links = soup.find_all("a", href=True)
            valid_news = []
            for a in links:
                href = a["href"]
                title = a.get_text(strip=True)
                if "/news/novosti_obschestva/" in href and len(title) > 20 and href != "/news/novosti_obschestva/":
                    full_url = f"https://scardio.ru{href}" if href.startswith("/") else href
                    valid_news.append({"title": title, "url": full_url})
            
            if valid_news:
                import random
                selected = random.choice(valid_news[:10])
                content_desc = ""
                try:
                    art_resp = requests.get(selected["url"], headers=headers, timeout=5)
                    if art_resp.status_code == 200:
                        art_soup = BeautifulSoup(art_resp.text, "html.parser")
                        paragraphs = [p.get_text(strip=True) for p in art_soup.find_all("p") if len(p.get_text(strip=True)) > 30]
                        content_desc = "\n".join(paragraphs[:4])
                except Exception:
                    pass

                return {
                    "title": selected["title"],
                    "journal": "Российское кардиологическое общество (РКО)",
                    "year": "2025-2026",
                    "content": content_desc if content_desc else selected["title"],
                    "url": selected["url"]
                }
    except Exception as e:
        logging.warning(f"Ошибка новостей РКО: {e}")

    return {
        "title": "Новости Российского кардиологического общества",
        "journal": "РКО (scardio.ru)",
        "year": "2025-2026",
        "content": "Актуальные новости кардиологии и клинические стандарты.",
        "url": base_section_url
    }

def fetch_pubmed_study_with_abstract(query: str) -> dict:
    try:
        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        params = {
            "db": "pubmed",
            "term": query,
            "mindate": "2023/01/01",
            "maxdate": "2026/12/31",
            "datetype": "pdat",
            "retmax": 10,
            "sort": "pub_date",
            "retmode": "json"
        }
        res = requests.get(search_url, params=params, timeout=7)
        id_list = res.json().get("esearchresult", {}).get("idlist", [])

        if not id_list:
            params.pop("mindate", None)
            params.pop("maxdate", None)
            res = requests.get(search_url, params=params, timeout=7)
            id_list = res.json().get("esearchresult", {}).get("idlist", [])

        if not id_list:
            return None

        import random
        random.shuffle(id_list)

        for pmid in id_list[:4]:
            fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
            xml_res = requests.get(fetch_url, params={"db": "pubmed", "id": pmid, "retmode": "xml"}, timeout=7)
            if xml_res.status_code != 200:
                continue

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
                "pmid": pmid,
                "title": title,
                "journal": journal,
                "year": year,
                "abstract": abstract[:2500],
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            }
    except Exception as e:
        logging.error(f"Ошибка PubMed API: {e}")
    return None

def sanitize_html_for_telegram(text: str) -> str:
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
    """Умный баланс: строгая наука 3 раза в неделю (Пн, Чт, Сб утро), остальное — яркий научпоп!"""
    now = datetime.now()
    weekday = now.weekday()  # 0: Пн, 1: Вт, 2: Ср, 3: Чт, 4: Пт, 5: Сб, 6: Вс
    hour = now.hour

    # Понедельник, Четверг, Суббота (утренний слот) — Научный дайджест РКО/PubMed (3 раза в неделю)
    if weekday in [0, 3, 5] and hour < 14:
        return RUBRIC_ACADEMIC_SCIENCE

    # Вечерние слоты: кулинарные рецепты и спорт
    if hour >= 14:
        if weekday in [0, 2, 5]:
            return RUBRIC_RECIPES
        else:
            return RUBRIC_SPORT

    # Все остальные утренние слоты: яркий YouTube-научпоп и разбор мифов
    if weekday in [1, 6]:
        return RUBRIC_YOUTUBE
    else:
        return RUBRIC_MYTHS

async def generate_and_publish_post(custom_rubric: dict = None) -> tuple[bool, str]:
    if not ai_client:
        err = "GEMINI_API_KEY не установлен в переменных Render!"
        logging.error(err)
        return False, err

    if not bot_poster:
        err = "Бот для отправки не настроен!"
        logging.error(err)
        return False, err

    rubric = custom_rubric or pick_rubric_by_schedule()
    logging.info(f"Запуск рубрики: {rubric['category']}")

    if rubric.get("source_type") == "youtube":
        study = fetch_global_youtube_video()
        prompt = (
            f"Напиши легкий, живой, увлекательный пост НА РУССКОМ ЯЗЫКЕ для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
            f"ТЕКСТ ВЫСТУПЛЕНИЯ СПИКЕРА (ТРАНСКРИПТ YOUTUBE):\n{study.get('content', '')}\n\n"
            f"Сделай захватывающую, понятную выжимку ключевых мыслей (без академической скуки).\n"
            f"В блоке Первоисточник поставь ТОЧНО эту ссылку на видео: <a href='{study['url']}'>{study['title']} ({study['journal']})</a>.\n"
            f"В самом конце обязательно добавь хештеги: {rubric['hashtags']}"
        )
    elif rubric.get("source_type") == "rko":
        study = fetch_rko_news()
        prompt = (
            f"Напиши интересный и понятный пост НА РУССКОМ ЯЗЫКЕ для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
            f"МАТЕРИАЛ РКО:\nЗаголовок: {study['title']}\nТекст: {study.get('content', '')}\n\n"
            f"В блоке Первоисточник поставь ТОЧНО эту ссылку: <a href='{study['url']}'>{study['title']} / {study['journal']}</a>.\n"
            f"В самом конце добавь хештеги: {rubric['hashtags']}"
        )
    else:
        study = fetch_pubmed_study_with_abstract(rubric['query'])
        if study:
            prompt = (
                f"Напиши увлекательный, легкий для чтения пост НА РУССКОМ ЯЗЫКЕ для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
                f"Тема: {rubric['ru_theme']}\n\n"
                f"ДАННЫЕ ИССЛЕДОВАНИЯ:\nЗаголовок: {study['title']}\nЖурнал: {study['journal']} ({study['year']})\nPMID: {study['pmid']}\n"
                f"Аннотация:\n{study['abstract']}\n\n"
                f"В блоке Первоисточник поставь ТОЧНО эту ссылку: <a href='{study['url']}'>{study['title']} / {study['journal']} (PMID: {study['pmid']})</a>.\n"
                f"В самом конце добавь хештеги: {rubric['hashtags']}"
            )
        else:
            prompt = (
                f"Напиши классный пост НА РУССКОМ ЯЗЫКЕ для Telegram-канала «Липидограм» в рубрику «{rubric['category']}» на тему: {rubric['ru_theme']}.\n"
                f"В первоисточнике укажи ссылку: <a href='https://scardio.ru/news/novosti_obschestva/'>Новости Российского кардиологического общества (РКО)</a>.\n"
                f"В самом конце добавь хештеги: {rubric['hashtags']}"
            )

    models_to_try = ['gemini-2.5-flash', 'gemini-2.5-flash-lite', 'gemini-2.0-flash', 'gemini-3.6-flash', 'gemini-3.7-flash']
    post_text = None
    last_error = None

    for model_name in models_to_try:
        for attempt in range(2):
            try:
                logging.info(f"Запрос к модели {model_name}...")
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        temperature=0.7,
                    )
                )
                if response and response.text:
                    post_text = response.text
                    break
            except Exception as e:
                last_error = e
                await asyncio.sleep(1.5)
        if post_text:
            break

    if not post_text:
        return False, f"Ошибка генерации: {last_error}"

    try:
        clean_html = sanitize_html_for_telegram(post_text)
        try:
            sent_msg = await bot_poster.send_message(
                chat_id=CHANNEL_ID,
                text=clean_html,
                parse_mode="HTML",
                disable_web_page_preview=False
            )
        except Exception as html_err:
            raw_text = re.sub(r'<[^>]+>', '', post_text)
            sent_msg = await bot_poster.send_message(
                chat_id=CHANNEL_ID,
                text=raw_text,
                disable_web_page_preview=False
            )

        logging.info(f"Пост «{rubric['category']}» опубликован в {CHANNEL_ID}! ID: {sent_msg.message_id}")
        return True, f"Опубликован пост рубрики «{rubric['category']}»!"
    except Exception as e:
        return False, f"Ошибка отправки: {e}"

# --- Команды бота ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.reply(
        "🫀 Медиа-бот «Липидограм» активен!\n\n"
        "Сетка контента:\n"
        "• 80% — развлекательный научпоп (YouTube мировых экспертов, рецепты, спорт, мифы)\n"
        "• 20% (до 3 раз в неделю) — авторитетная фундаментальная наука РКО/PubMed.\n\n"
        "Команды:\n"
        "• /post_now — публикация следующего поста по расписанию.\n"
        "• /post_youtube — немедленный пост с разбором популярного видео.\n"
        "• /post_recipe — немедленный гиполипидемический рецепт.\n"
        "• /post_myth — разбор популярного мифа."
    )

@dp.message(Command("post_now"))
async def cmd_post_now(message: types.Message):
    await message.reply("⏳ Формирую пост по актуальной контентной сетке...")
    success, res = await generate_and_publish_post()
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_youtube"))
async def cmd_post_yt(message: types.Message):
    await message.reply("🎬 Запрашиваю популярное видео (Attia, Huberman, Утин) и готовлю выжимку...")
    success, res = await generate_and_publish_post(RUBRIC_YOUTUBE)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_recipe"))
async def cmd_post_rec(message: types.Message):
    await message.reply("🥗 Готовлю аппетитный гиполипидемический рецепт...")
    success, res = await generate_and_publish_post(RUBRIC_RECIPES)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_myth"))
async def cmd_post_my(message: types.Message):
    await message.reply("💡 Развенчиваю популярный миф доказательной медициной...")
    success, res = await generate_and_publish_post(RUBRIC_MYTHS)
    await message.reply("✅ " + res if success else "❌ " + res)

# --- Модерация комментариев ---
@dp.message(F.text)
async def handle_comment(message: types.Message):
    if message.chat.type == "private":
        return
    if message.sender_chat and message.sender_chat.type == "channel":
        return

    if bot_moderator:
        try:
            chat_member = await bot_moderator.get_chat_member(message.chat.id, message.from_user.id)
            if chat_member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                return
        except Exception:
            pass

    text = message.text.lower()
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
            logging.error(f"Ошибка удаления: {e}")

        warnings = user_warnings.get(user_id, 0) + 1
        user_warnings[user_id] = warnings

        if warnings == 1:
            await message.answer(
                f"⚠️ {user_mention}, ваше сообщение удалено (причина: {reason}). Предупреждение: <b>1/3</b>.",
                parse_mode="HTML"
            )
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
                logging.error(f"Ошибка мута: {e}")
        else:
            try:
                await bot_moderator.ban_chat_member(chat_id=message.chat.id, user_id=user_id)
                await message.answer(
                    f"🚫 {user_mention} заблокирован за систематическое нарушение правил (3/3).",
                    parse_mode="HTML"
                )
                user_warnings.pop(user_id, None)
            except Exception as e:
                logging.error(f"Ошибка бана: {e}")

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

    # График автопостинга: в 10:00 (утренний слот) и 18:30 (вечерний слот) по МСК
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(generate_and_publish_post, "cron", hour=10, minute=0)
    scheduler.add_job(generate_and_publish_post, "cron", hour=18, minute=30)
    scheduler.start()

    logging.info("Служба расписания и боты успешно запущены!")

    if bot_poster:
        if bot_moderator and bot_moderator != bot_poster:
            await asyncio.gather(
                dp.start_polling(bot_poster),
                dp.start_polling(bot_moderator)
            )
        else:
            await dp.start_polling(bot_poster)
    else:
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
