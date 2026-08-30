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
current_rubric_index = 0

BAD_WORDS_PATTERN = re.compile(
    r'\b(ху[йиеяё]|пизд|бл[яе]|еб[аелиотс]|сук[аи]|муд[ао]|говно|залуп|чмо|дерьм|шлюх|гандон)\w*',
    re.IGNORECASE
)
SPAM_LINKS_PATTERN = re.compile(
    r'(https?://\S+|t\.me/\S+|telegram\.me/\S+|@[a-zA-Z0-9_]{5,})',
    re.IGNORECASE
)

# RSS-каналы доказательной кардиологии на YouTube
YOUTUBE_CHANNELS = [
    {"name": "Российское кардиологическое общество (РКО)", "id": "UCe1997q9K009Qj_7eS_eP-A", "handle": "@scardioru"},
    {"name": "НМИЦ ТПМ Минздрава России", "id": "UCk0mY3fT6vR3VpG-x4m2t7Q", "handle": "@gnicpm"},
]

RUBRICS = [
    {
        "category": "📺 ВЫЖИМКА ЛЕКЦИИ РКО / YOUTUBE",
        "source_type": "youtube",
        "ru_theme": "Главные тезисы из видеолекций ведущих кардиологов и экспертов РКО",
        "hashtags": "#Липидограм_Видео #ЛекцияРКО #Кардиология #Практика"
    },
    {
        "category": "🥗 ГИПОЛИПИДЕМИЧЕСКАЯ КУХНЯ",
        "source_type": "pubmed",
        "query": '("dietary fiber" OR "beta-glucan" OR "legumes" OR "flaxseed") AND ("LDL cholesterol" OR "lipids") AND ("trial" OR "randomized")',
        "ru_theme": "Растворимая клетчатка, продукты и нутриенты для снижения ЛПНП и синтеза желчных кислот",
        "hashtags": "#Рецепт_ЛПНП #УмнаяЗамена #ПитаниеСердца #Клетчатка"
    },
    {
        "category": "🏃 СПОРТ И СОСУДЫ",
        "source_type": "pubmed",
        "query": '("aerobic exercise" OR "resistance training" OR "interval training") AND ("flow-mediated dilation" OR "endothelial" OR "HDL-C" OR "lipid profile") AND ("trial" OR "randomized")',
        "ru_theme": "Влияние аэробных/силовых тренировок на сосудистый эндотелий, ЛПВП и триглицериды",
        "hashtags": "#СпортИСосуды #ЗдоровьеСердца #Кардиотренировки #Эндотелий"
    },
    {
        "category": "💡 РАЗБОР МИФОВ ДОКАЗАТЕЛЬНОЙ МЕДИЦИНОЙ",
        "source_type": "pubmed",
        "query": '("dietary cholesterol" OR "eggs" OR "statins" OR "omega-3 fatty acids") AND ("atherosclerosis" OR "cardiovascular") AND ("meta-analysis" OR "systematic review")',
        "ru_theme": "Разбор фактов и мифов: холестерин в еде, статины, добавки омега-3, чистки сосудов",
        "hashtags": "#Мифы_Липидограм #Доказательно #Холестерин"
    },
    {
        "category": "🔬 СВЕЖАЯ НАУКА И АНАЛИЗЫ",
        "source_type": "pubmed",
        "query": '("Apolipoprotein B" OR "LDL-C lowering" OR "PCSK9" OR "SCORE2") AND ("cardiovascular risk" OR "atherosclerosis") AND ("guidelines" OR "trial" OR "meta-analysis")',
        "ru_theme": "Клинические маркеры атеросклероза (АпоВ, ЛПНП, триглицериды, шкала риска SCORE-2)",
        "hashtags": "#Липидограм_Наука #Кардиология #ЛПНП #PubMed"
    },
    {
        "category": "🇷🇺 НОВОСТИ ИЗ РАССЫЛКИ И САЙТА РКО",
        "source_type": "rko",
        "query": "новости общества",
        "ru_theme": "Новости и клинические события Российского кардиологического общества (РКО)",
        "hashtags": "#Липидограм_РКО #КардиологияРФ #РКО #ЗдоровьеСердца"
    }
]

SYSTEM_PROMPT = """
Ты — ведущий научный редактор русскоязычного Telegram-канала «Липидограм» (@lipidogram).
Твоя задача — писать экспертный, понятный и полезный пост НА РУССКОМ ЯЗЫКЕ на основе предоставленного первоисточника.

Если тебе передан транскрипт видео с YouTube:
- Выдели ключевую суть и практические рекомендации лектора (без вводных слов и приветствий).
- Оформи в виде структурированного дайджеста видеолекции.

Формат публикации (HTML):
• Заголовок: Яркий, привлекательный, с тематическими эмодзи (в тегах <b>Заголовок</b>).
• Введение: 1-2 предложения, почему тема видео/статьи важна для сохранения чистоты сосудов.
• Ключевые тезисы: 3-4 четких пункта с цифрами, нормами и объяснениями.
• Практический совет: Четкое действие для читателя.
• Первоисточник: Кликабельная ссылка СТРОГО на предоставленный URL: <a href="ТОЧНЫЙ_URL">Название / Источник</a>.
• Хештеги рубрики в самом конце.

Используй только валидные теги: <b>, </b>, <i>, </i>, <code>, </code>, <a href="...">.
Все знаки «меньше» или «больше» пиши словами («менее», «более») или экранируй (&lt; и &gt;).
"""

def fetch_youtube_lecture() -> dict:
    """Ищет видео на официальном канале РКО и скачивает транскрипт лекции."""
    try:
        # Открываем официальный канал РКО @scardioru
        channel_url = "https://www.youtube.com/@scardioru/videos"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(channel_url, headers=headers, timeout=8)
        
        video_ids = []
        if resp.status_code == 200:
            # Извлекаем ID видео из страницы канала
            matches = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
            video_ids = list(dict.fromkeys(matches))  # Удаляем дубликаты
            
        if not video_ids:
            # Популярные проверенные видеолекции РКО по дислипидемиям и атеросклерозу
            video_ids = ["e7B1W6vHq3A", "5Xk_wGqL2bY", "3N8jK9P7V2M", "X9vLm7Z4K1Q"]

        import random
        random.shuffle(video_ids)

        for vid in video_ids[:5]:
            try:
                # Получаем субтитры на русском языке
                transcript_list = YouTubeTranscriptApi.get_transcript(vid, languages=['ru'])
                full_text = " ".join([t['text'] for t in transcript_list])
                
                if len(full_text) > 300:
                    video_url = f"https://www.youtube.com/watch?v={vid}"
                    return {
                        "title": "Клиническая видеолекция экспертов РКО",
                        "journal": "Официальный YouTube-канал РКО (@scardioru)",
                        "year": "2025-2026",
                        "content": full_text[:3500],  # Передаем выдержку из лекции
                        "url": video_url
                    }
            except Exception:
                continue

    except Exception as e:
        logging.warning(f"Ошибка получения видео РКО ({e})")

    return {
        "title": "Официальный образовательный видеоканал Российского кардиологического общества",
        "journal": "YouTube-канал РКО (@scardioru)",
        "year": "2025-2026",
        "content": "Лекции и разборы клинических рекомендаций по липидологии, ИБС и кардиоваскулярной профилактике.",
        "url": "https://www.youtube.com/@scardioru"
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

        email_ids = messages[0].split()
        latest_id = email_ids[-1]

        res, msg_data = mail.fetch(latest_id, "(RFC822)")
        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = decode_mime_words(msg["Subject"])
        body = ""
        found_links = []

        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                elif ctype == "text/html":
                    html_content = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    soup = BeautifulSoup(html_content, "html.parser")
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

        logging.info(f"Найдено письмо от РКО: {subject}")
        return {
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
    email_study = fetch_rko_from_email()
    if email_study:
        return email_study

    base_section_url = "https://scardio.ru/news/novosti_obschestva/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
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
                    if not any(x in full_url for x in ["page=", "category=", "archive"]):
                        valid_news.append({"title": title, "url": full_url})
            
            if valid_news:
                import random
                selected = random.choice(valid_news[:10])
                content_desc = ""
                try:
                    article_resp = requests.get(selected["url"], headers=headers, timeout=5)
                    if article_resp.status_code == 200:
                        art_soup = BeautifulSoup(article_resp.text, "html.parser")
                        paragraphs = art_soup.find_all("p")
                        p_texts = [p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 30]
                        content_desc = "\n".join(p_texts[:4])
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
        logging.warning(f"Ошибка парсинга раздела novosti_obschestva: {e}")

    return {
        "title": "Новости Российского кардиологического общества",
        "journal": "РКО (scardio.ru)",
        "year": "2025-2026",
        "content": "Актуальные новости кардиологии, клинические стандарты и профилактика сердечно-сосудистых заболеваний.",
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
        data = res.json()
        id_list = data.get("esearchresult", {}).get("idlist", [])
        
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
            fetch_params = {
                "db": "pubmed",
                "id": pmid,
                "retmode": "xml"
            }
            xml_res = requests.get(fetch_url, params=fetch_params, timeout=7)
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
        logging.error(f"Ошибка NCBI PubMed API: {e}")
        
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

async def generate_and_publish_post() -> tuple[bool, str]:
    global current_rubric_index

    if not ai_client:
        err = "GEMINI_API_KEY не установлен в переменных Render!"
        logging.error(err)
        return False, err

    if not bot_poster:
        err = "Бот для отправки не настроен!"
        logging.error(err)
        return False, err

    rubric = RUBRICS[current_rubric_index]
    current_rubric_index = (current_rubric_index + 1) % len(RUBRICS)

    logging.info(f"Запуск рубрики: {rubric['category']} ({rubric['ru_theme']})")

    # Обработка источника YouTube
    if rubric.get("source_type") == "youtube":
        study = fetch_youtube_lecture()
        prompt = (
            f"Напиши готовый пост НА РУССКОМ ЯЗЫКЕ для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
            f"Тема: {rubric['ru_theme']}\n\n"
            f"ТЕКСТ ВЫСТУПЛЕНИЯ ЛЕКТОРА (ТРАНСКРИПТ ВИДЕО YOUTUBE):\n{study.get('content', '')}\n\n"
            f"Сделай краткую, емкую, практичную выжимку ключевых тезисов лектора.\n"
            f"В блоке Первоисточник поставь ТОЧНО эту ссылку на видео: <a href='{study['url']}'>{study['title']} / {study['journal']}</a>.\n"
            f"В самом конце обязательно добавь хештеги: {rubric['hashtags']}"
        )
    elif rubric.get("source_type") == "rko":
        study = fetch_rko_news()
        prompt = (
            f"Напиши готовый пост НА РУССКОМ ЯЗЫКЕ для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
            f"Тема: {rubric['ru_theme']}\n\n"
            f"РЕАЛЬНЫЕ ДАННЫЕ РКО (из официальной рассылки/сайта scardio.ru):\n"
            f"Заголовок: {study['title']}\n"
            f"Организация: {study['journal']} ({study['year']})\n"
            f"Текст:\n{study.get('content', '')}\n\n"
            f"Напиши пост СТРОГО по содержанию этого материала РКО.\n"
            f"В блоке Первоисточник поставь ТОЧНО эту ссылку: <a href='{study['url']}'>{study['title']} / {study['journal']}</a>.\n"
            f"В самом конце обязательно добавь хештеги: {rubric['hashtags']}"
        )
    else:
        study = fetch_pubmed_study_with_abstract(rubric['query'])
        if study:
            prompt = (
                f"Напиши готовый пост НА РУССКОМ ЯЗЫКЕ для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
                f"Тема публикации: {rubric['ru_theme']}\n\n"
                f"СТАТЬЯ ИЗ PUBMED ДЛЯ ОСНОВЫ ПОСТА:\n"
                f"Заголовок: {study['title']}\n"
                f"Журнал: {study['journal']} ({study['year']})\n"
                f"PMID: {study['pmid']}\n"
                f"URL: {study['url']}\n\n"
                f"АННОТАЦИЯ (ABSTRACT) СТАТЬИ:\n{study['abstract']}\n\n"
                f"Напиши пост СТРОГО по этой аннотации. Опиши выводы ученых, методику и конкретные цифры.\n"
                f"В блоке Первоисточник поставь ТОЧНО эту ссылку: <a href='{study['url']}'>{study['title']} / {study['journal']} (PMID: {study['pmid']})</a>.\n"
                f"В самом конце добавь хештеги: {rubric['hashtags']}"
            )
        else:
            prompt = (
                f"Напиши готовый пост НА РУССКОМ ЯЗЫКЕ для Telegram-канала «Липидограм» в рубрику «{rubric['category']}» на тему: {rubric['ru_theme']}.\n"
                "Опирайся на доказательную медицину и клинические рекомендации РКО/ESC.\n"
                "В первоисточнике укажи ссылку на открытые новости РКО: <a href='https://scardio.ru/news/novosti_obschestva/'>Новости Российского кардиологического общества (РКО)</a>.\n"
                f"В самом конце добавь хештеги: {rubric['hashtags']}"
            )

    models_to_try = [
        'gemini-2.5-flash',
        'gemini-2.5-flash-lite',
        'gemini-2.0-flash',
        'gemini-3.6-flash',
        'gemini-3.7-flash'
    ]
    
    post_text = None
    last_error = None

    for model_name in models_to_try:
        for attempt in range(2):
            try:
                logging.info(f"Запрос к модели {model_name} (попытка {attempt+1})...")
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
                logging.warning(f"Модель {model_name} вернула: {e}")
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
            logging.warning(f"HTML error ({html_err}), sending plain text...")
            raw_text = re.sub(r'<[^>]+>', '', post_text)
            sent_msg = await bot_poster.send_message(
                chat_id=CHANNEL_ID,
                text=raw_text,
                disable_web_page_preview=False
            )

        logging.info(f"Пост рубрики «{rubric['category']}» опубликован в {CHANNEL_ID}! ID: {sent_msg.message_id}")
        return True, f"Опубликован пост рубрики «{rubric['category']}»!"
    except Exception as e:
        err_msg = f"Ошибка отправки: {e}"
        logging.error(err_msg)
        return False, err_msg

# --- Хэндлеры команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.reply("🫀 Медиа-бот «Липидограм» активен.\n\nКоманды:\n• /post_now — публикация следующего поста из контент-плана (YouTube-лекции, Рецепты, Спорт, Мифы, Наука, РКО).\n• /check_email — проверить почту на свежие письма от РКО.")

@dp.message(Command("check_email"))
async def cmd_check_email(message: types.Message):
    await message.reply("📬 Проверяю ваш почтовый ящик на свежие письма от РКО...")
    email_data = fetch_rko_from_email()
    if email_data:
        await message.reply(f"Найдено письмо: «{email_data['title']}»! Генерирую пост...")
        global current_rubric_index
        current_rubric_index = 5  # Рубрика рассылки РКО
        success, result_text = await generate_and_publish_post()
        await message.reply("✅ " + result_text if success else "❌ " + result_text)
    else:
        await message.reply("Новых писем от РКО в ящике сейчас нет. Бот будет использовать открытые новости сайта, YouTube и PubMed.")

@dp.message(Command("post_now"))
async def cmd_post_now(message: types.Message):
    await message.reply("⏳ Запрашиваю материал (YouTube-лекция РКО / PubMed Abstract / РКО) и формирую пост...")
    success, result_text = await generate_and_publish_post()
    if success:
        await message.reply("✅ " + result_text)
    else:
        await message.reply("❌ Ошибка:\n" + result_text)

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
                f"⚠️ {user_mention}, ваше сообщение удалено (причина: {reason}).\n"
                f"Пожалуйста, соблюдайте правила сообщества. Предупреждение: <b>1/3</b>.",
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
                logging.error(f"Ошибка ограничения: {e}")
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
    logging.info(f"Веб-сервер успешно слушает порт {PORT}")

async def main():
    await run_server()

    # График автопостинга: в 10:00 и 18:30 по МСК
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(generate_and_publish_post, "cron", hour=10, minute=0)
    scheduler.add_job(generate_and_publish_post, "cron", hour=18, minute=30)
    scheduler.start()

    logging.info("Служба расписания и боты запущены!")

    if bot_poster:
        if bot_moderator and bot_moderator != bot_poster:
            await asyncio.gather(
                dp.start_polling(bot_poster),
                dp.start_polling(bot_moderator)
            )
        else:
            await dp.start_polling(bot_poster)
    else:
        logging.error("Нет доступных ботов для запуска!")
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
