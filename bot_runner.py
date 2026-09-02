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

MODERATOR_TOKEN = os.getenv("MODERATOR_BOT_TOKEN", "").strip()
POSTER_TOKEN = os.getenv("POSTER_BOT_TOKEN", "").strip()
KIE_KEY = os.getenv("KIE_API_KEY", "").strip()
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

RUBRIC_RECIPES = {
    "category": "🥗 ГИПОЛИПИДЕМИЧЕСКАЯ КУХНЯ",
    "source_type": "pubmed",
    "style_type": "recipe_card",
    "query": '("dietary fiber" OR "beta-glucan" OR "legumes" OR "flaxseed" OR "olive oil") AND ("LDL cholesterol" OR "lipids") AND ("trial" OR "randomized")',
    "ru_theme": "Кулинарный рецепт для снижения ЛПНП (насыщенные жиры менее 1.5г, клетчатка более 6г)",
    "hashtags": "#Рецепт_ЛПНП #УмнаяЗамена #ПитаниеСердца #Клетчатка"
}

RUBRIC_YOUTUBE = {
    "category": "📺 МИРОВОЙ НАУЧПОП / ВЫЖИМКА",
    "source_type": "youtube",
    "style_type": "story_or_interview",
    "ru_theme": "Увлекательная выжимка из популярного видео мировых экспертов (Attia, Huberman, Утин, Rhonda Patrick)",
    "hashtags": "#Липидограм_Видео #Научпоп #Долголетие #ЗдоровьеСосудов"
}

RUBRIC_MYTHS = {
    "category": "💡 МИФ ИЛИ РЕАЛЬНОСТЬ",
    "source_type": "pubmed",
    "style_type": "myth_buster",
    "query": '("dietary cholesterol" OR "eggs" OR "statins" OR "omega-3 fatty acids" OR "coffee") AND ("atherosclerosis" OR "cardiovascular") AND ("meta-analysis" OR "systematic review")',
    "ru_theme": "Разбор популярного мифа: яйца, кофе, статины, чистки сосудов доказательной медициной",
    "hashtags": "#Мифы_Липидограм #Доказательно #Холестерин"
}

RUBRIC_SPORT = {
    "category": "🏃 АКТИВНОСТЬ И ЭЛАСТИЧНОСТЬ СОСУДОВ",
    "source_type": "pubmed",
    "style_type": "practical_guide",
    "query": '("aerobic exercise" OR "resistance training" OR "walking") AND ("flow-mediated dilation" OR "endothelial" OR "HDL-C" OR "lipid profile") AND ("trial" OR "randomized")',
    "ru_theme": "Простые советы по движению: быстрая прогулка после еды, 8000 шагов в день, легкий бег в комфортном разговорном темпе без одышки, домашняя зарядка для сосудов",
    "hashtags": "#Движение_Липидограм #ЗдоровьеСердца #Прогулки #ЭластичностьСосудов"
}

RUBRIC_ACADEMIC_SCIENCE = {
    "category": "🔬 НАУЧНЫЙ ДАЙДЖЕСТ (РКО / PUBMED)",
    "source_type": "rko",
    "style_type": "expert_review",
    "query": "липиды холестерин",
    "ru_theme": "Клинические новости Российского кардиологического общества (РКО) и новейшие мета-анализы",
    "hashtags": "#Липидограм_Наука #РКО #Кардиология #ЛПНП"
}

SYSTEM_PROMPT = """
Ты — главный редактор русскоязычного Telegram-канала «Липидограм» (@lipidogram).
Твоя задача — написать яркий, легкий и увлекательный пост простым человеческим языком.

КАТЕГОРИЧЕСКИЙ ЗАПРЕТ:
- ЗАПРЕЩЕНО писать «Зона 2» без понятного объяснения. Пиши: «прогулка быстрым шагом», «бег в разговорном темпе без одышки», «8000 шагов».

ТРЕБОВАНИЯ К ТЕКСТУ ("post_text"):
- Объем: строго 500-750 символов (для идеального размещения в подписи к фото).
- Разрешенные теги: <b>, </b>, <i>, </i>, <code>, </code>, <a href="...">.
- Знаки < и > пиши словами («менее», «более») или экранируй (&lt; и &gt;).
- В самом конце кликабельная ссылка на предоставленный URL первоисточника и хештеги.

ТРЕБОВАНИЯ К IMAGE PROMPT ("image_prompt"):
- На АНГЛИЙСКОМ языке.
- Описывай конкретную фотореалистичную сцену для генерации (например: "Close-up of fresh grilled salmon with avocado and spinach salad on black ceramic plate, professional restaurant food photography, 8k resolution, soft studio lighting").

ВЕРНИ ОТВЕТ СТРОГО В JSON:
{
  "post_text": "...",
  "image_prompt": "..."
}
"""

async def generate_kie_text_and_prompt(user_prompt: str) -> tuple[str, str]:
    if not KIE_KEY:
        raise ValueError("KIE_API_KEY не установлен в переменных окружения!")

    urls = [
        f"https://api.kie.ai/gemini/v1/models/gemini-3.7-flash:generateContent?key={KIE_KEY}",
        f"https://api.kie.ai/gemini/v1/models/gemini-3-7-flash:generateContent?key={KIE_KEY}"
    ]
    
    headers = {
        "Authorization": f"Bearer {KIE_KEY}",
        "x-goog-api-key": KIE_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": f"{SYSTEM_PROMPT}\n\nЗАДАНИЕ:\n{user_prompt}"}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.75,
            "responseMimeType": "application/json"
        }
    }

    last_error = ""
    async with aiohttp.ClientSession() as session:
        for target_url in urls:
            try:
                logging.info("Отправка запроса в KIE.ai Gemini 3.7 Flash...")
                async with session.post(target_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=40)) as resp:
                    if resp.status == 200:
                        raw_json = await resp.json()
                        
                        if isinstance(raw_json, dict) and (raw_json.get("code") in [500, 400, 401, 403] or "Server exception" in str(raw_json)):
                            last_error = f"KIE error: {raw_json.get('msg', 'Server exception')}"
                            continue

                        candidates = raw_json.get("candidates", []) if isinstance(raw_json, dict) else []
                        if not candidates:
                            continue

                        content_parts = candidates[0].get("content", {}).get("parts", [])
                        if not content_parts:
                            continue

                        raw_text = content_parts[0].get("text", "")
                        
                        clean_str = raw_text.strip()
                        if clean_str.startswith("```json"):
                            clean_str = clean_str[7:]
                        if clean_str.startswith("```"):
                            clean_str = clean_str[3:]
                        if clean_str.endswith("```"):
                            clean_str = clean_str[:-3]
                        clean_str = clean_str.strip()

                        match = re.search(r'\{[\s\S]*\}', clean_str)
                        if match:
                            parsed = json.loads(match.group(0))
                            
                            if "code" in parsed and "msg" in parsed:
                                continue

                            post_text = parsed.get("post_text") or parsed.get("text") or parsed.get("post") or parsed.get("content")
                            image_prompt = parsed.get("image_prompt") or parsed.get("prompt") or parsed.get("image")
                            
                            if post_text and len(str(post_text).strip()) > 50:
                                return str(post_text).strip(), str(image_prompt or "healthy cardiology food lifestyle").strip()
                    else:
                        text_err = await resp.text()
                        last_error = f"HTTP {resp.status}: {text_err[:120]}"
            except Exception as e:
                last_error = str(e)
                continue

    raise Exception(f"KIE.ai Gemini 3.7 ошибка: {last_error}")

def extract_all_urls_from_any_json(obj) -> list:
    """Рекурсивно ищет любые прямые ссылки на изображения в структуре ответа."""
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
    """Создает задачу в Nano Banana 2 Lite и ожидает готовности файла до 100 секунд."""
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
                img_url = immediate_urls[0]
                logging.info(f"Картинка получена сразу: {img_url}")
                async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=25)) as dl:
                    if dl.status == 200:
                        return await dl.read()

            task_id = (
                res_data.get("data", {}).get("taskId") 
                or res_data.get("data", {}).get("id") 
                or res_data.get("taskId") 
                or res_data.get("id")
            )

            if not task_id:
                logging.warning(f"Не найден taskId в ответе KIE: {res_data}")
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
                                    final_img_url = urls_in_status[0]
                                    logging.info(f"УСПЕХ! Найдена готовая картинка KIE: {final_img_url}. Скачивание...")
                                    async with session.get(final_img_url, timeout=aiohttp.ClientTimeout(total=30)) as dl:
                                        if dl.status == 200:
                                            b_data = await dl.read()
                                            if len(b_data) > 2000:
                                                logging.info(f"Картинка успешно скачана! Размер: {len(b_data)} байт")
                                                return b_data

                                result_data = s_data.get("data", {}) if isinstance(s_data.get("data"), dict) else s_data
                                state = str(result_data.get("state") or result_data.get("status") or s_data.get("status") or "").lower()
                                logging.info(f"Статус Nano Banana (шаг {attempt}/25): {state}")

                                if state in ["failed", "error", "-1"]:
                                    logging.warning(f"Ошибка генерации на стороне KIE: {s_data}")
                                    return None
                    except Exception as loop_e:
                        logging.debug(f"Опрос статуса: {loop_e}")
                        continue

            logging.warning("Превышен таймаут ожидания Nano Banana (100 секунд).")
    except Exception as e:
        logging.warning(f"Ошибка вызова Nano Banana 2 Lite: {e}")

    return None

def fetch_global_youtube_video() -> dict:
    channel = random.choice(GLOBAL_HEALTH_CHANNELS)
    try:
        import urllib.request
        headers = {"User-Agent": "Mozilla/5.0"}
        req = urllib.request.Request(f"https://www.youtube.com/{channel['handle']}/videos", headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            html = response.read().decode('utf-8', errors='ignore')
            video_ids = list(dict.fromkeys(re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)))

        random.shuffle(video_ids)

        for vid in video_ids[:8]:
            try:
                transcript_list = YouTubeTranscriptApi.get_transcript(vid, languages=['en', 'en-US', 'ru'])
                full_text = " ".join([t['text'] for t in transcript_list])
                keywords = ["cholesterol", "ldl", "apob", "artery", "atherosclerosis", "heart", "diet", "walking", "exercise", "lipids", "statins", "omega-3", "холестерин", "сосуд", "сердц", "лпнп", "давлен", "питан", "статины", "жир", "ходьба", "спорт"]
                if len(full_text) > 400 and any(kw in full_text.lower() for kw in keywords):
                    yt_img = f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
                    return {
                        "title": f"Разбор эксперта: {channel['name']}",
                        "journal": f"YouTube-канал {channel['name']}",
                        "year": "2025-2026",
                        "content": full_text[:3500],
                        "image_url": yt_img,
                        "url": f"https://www.youtube.com/watch?v={vid}"
                    }
            except Exception:
                continue
    except Exception as e:
        logging.warning(f"Ошибка YouTube {channel['name']}: {e}")

    return {
        "title": "Популярный видеоразбор о здоровье сердца и сосудов",
        "journal": f"YouTube-канал {channel['name']}",
        "year": "2025-2026",
        "content": "Подробный разбор факторов риска, холестерина, пользы ежедневных прогулок быстрым шагом и оптимизации питания.",
        "image_url": None,
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
        import urllib.request
        req = urllib.request.Request(base_section_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            soup = BeautifulSoup(resp.read().decode('utf-8', errors='ignore'), "html.parser")
            links = soup.find_all("a", href=True)
            valid_news = []
            for a in links:
                href = a["href"]
                title = a.get_text(strip=True)
                if "/news/novosti_obschestva/" in href and len(title) > 20 and href != "/news/novosti_obschestva/":
                    full_url = f"https://scardio.ru{href}" if href.startswith("/") else href
                    valid_news.append({"title": title, "url": full_url})
            
            if valid_news:
                selected = random.choice(valid_news[:10])
                content_desc = ""
                try:
                    art_req = urllib.request.Request(selected["url"], headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(art_req, timeout=5) as art_resp:
                        art_soup = BeautifulSoup(art_resp.read().decode('utf-8', errors='ignore'), "html.parser")
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
        import urllib.request
        search_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={urllib.parse.quote(query)}&retmax=10&sort=pub_date&retmode=json"
        req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=7) as res:
            data = json.loads(res.read().decode('utf-8'))
            id_list = data.get("esearchresult", {}).get("idlist", [])

        if not id_list:
            return None

        random.shuffle(id_list)

        for pmid in id_list[:4]:
            fetch_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={pmid}&retmode=xml"
            fetch_req = urllib.request.Request(fetch_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(fetch_req, timeout=7) as xml_res:
                xml_content = xml_res.read()

            root = ET.fromstring(xml_content)
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
    weekday = now.weekday()
    hour = now.hour

    if weekday in [0, 3, 5] and hour < 14:
        return RUBRIC_ACADEMIC_SCIENCE

    if hour >= 14:
        if weekday in [0, 2, 5]:
            return RUBRIC_RECIPES
        else:
            return RUBRIC_SPORT

    if weekday in [1, 6]:
        return RUBRIC_YOUTUBE
    else:
        return RUBRIC_MYTHS

async def generate_and_publish_post(custom_rubric: dict = None, with_image: bool = True) -> tuple[bool, str]:
    if not KIE_KEY:
        err = "KIE_API_KEY не установлен в переменных окружения!"
        logging.error(err)
        return False, err

    if not bot_poster:
        err = "Бот для отправки не настроен!"
        logging.error(err)
        return False, err

    rubric = custom_rubric or pick_rubric_by_schedule()
    style = rubric.get("style_type", "expert_review")
    logging.info(f"Запуск рубрики: {rubric['category']} (стиль: {style}, генерация арта: {with_image})")

    img_bytes = None

    if rubric.get("source_type") == "youtube":
        study = fetch_global_youtube_video()
        if with_image and study.get("image_url"):
            try:
                async with aiohttp.ClientSession() as yt_s:
                    async with yt_s.get(study["image_url"], timeout=aiohttp.ClientTimeout(total=10)) as yt_r:
                        if yt_r.status == 200:
                            img_bytes = await yt_r.read()
            except Exception:
                pass

        prompt = (
            f"Напиши пост в стиле «{style}» для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
            f"ТЕКСТ ВЫСТУПЛЕНИЯ СПИКЕРА:\n{study.get('content', '')}\n\n"
            f"В блоке Первоисточник поставь ТОЧНО эту ссылку на видео: <a href='{study['url']}'>{study['title']} ({study['journal']})</a>.\n"
            f"В самом конце добавь хештеги: {rubric['hashtags']}"
        )
    elif rubric.get("source_type") == "rko":
        study = fetch_rko_news()
        prompt = (
            f"Напиши понятный и актуальный пост в стиле «{style}» для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
            f"МАТЕРИАЛ РКО:\nЗаголовок: {study['title']}\nТекст: {study.get('content', '')}\n\n"
            f"В блоке Первоисточник поставь ТОЧНО эту ссылку: <a href='{study['url']}'>{study['title']} / {study['journal']}</a>.\n"
            f"В самом конце добавь хештеги: {rubric['hashtags']}"
        )
    else:
        study = fetch_pubmed_study_with_abstract(rubric['query'])
        if study:
            prompt = (
                f"Напиши пост в стиле «{style}» для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
                f"Тема: {rubric['ru_theme']}\n\n"
                f"ДАННЫЕ ИССЛЕДОВАНИЯ:\nЗаголовок: {study['title']}\nЖурнал: {study['journal']} ({study['year']})\nPMID: {study['pmid']}\n"
                f"Аннотация:\n{study['abstract']}\n\n"
                f"В блоке Первоисточник поставь ТОЧНО эту ссылку: <a href='{study['url']}'>{study['title']} / {study['journal']} (PMID: {study['pmid']})</a>.\n"
                f"В самом конце добавь хештеги: {rubric['hashtags']}"
            )
        else:
            prompt = (
                f"Напиши классный пост в стиле «{style}» для Telegram-канала «Липидограм» в рубрику «{rubric['category']}» на тему: {rubric['ru_theme']}.\n"
                f"В первоисточнике укажи ссылку: <a href='https://scardio.ru/news/novosti_obschestva/'>Новости Российского кардиологического общества (РКО)</a>.\n"
                f"В самом конце добавь хештеги: {rubric['hashtags']}"
            )

    try:
        post_text, image_prompt = await generate_kie_text_and_prompt(prompt)
        if not post_text:
            return False, "Ошибка: модель вернула пустой текст поста."
    except Exception as e:
        return False, f"Ошибка генерации текста: {e}"

    if with_image and not img_bytes and image_prompt:
        img_bytes = await generate_kie_image_bytes(image_prompt)

    try:
        clean_html = sanitize_html_for_telegram(post_text)

        if img_bytes and len(img_bytes) > 2000:
            photo_file = BufferedInputFile(img_bytes, filename="lipidogram_art.jpg")
            
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
            logging.info(f"Пост с фото опубликован! ID: {sent_msg.message_id}")
            return True, f"Опубликован пост («{rubric['category']}») с иллюстрацией Nano Banana!"

        sent_msg = await bot_poster.send_message(
            chat_id=CHANNEL_ID,
            text=clean_html,
            parse_mode="HTML",
            disable_web_page_preview=False
        )
        return True, f"Опубликован пост (только текст Gemini 3.7) в рубрике «{rubric['category']}»."
    except Exception as e:
        return False, f"Ошибка отправки в Telegram: {e}"

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.reply(
        "🫀 <b>Медиа-бот «Липидограм»</b>\n\n"
        "<b>Команды с генерацией арта (Nano Banana 2 Lite + Gemini 3.7):</b>\n"
        "• /post_now — публикация по текущему расписанию с фото\n"
        "• /post_youtube — видеовыжимка мировых экспертов\n"
        "• /post_recipe — кулинарная карточка с фото\n"
        "• /post_myth — разбор мифа с фото\n\n"
        "<b>🧪 Тестовые команды БЕЗ картинки (только Gemini 3.7, 0 кредитов арта):</b>\n"
        "• /test_text — быстрый пост по расписанию (только текст)\n"
        "• /test_youtube — проверка выжимки YouTube\n"
        "• /test_recipe — проверка рецепта (только текст)\n"
        "• /test_myth — проверка мифа (только текст)\n"
        "• /test_science — научный дайджест РКО / PubMed",
        parse_mode="HTML"
    )

# --- Команды с полной генерацией арта ---
@dp.message(Command("post_now"))
async def cmd_post_now(message: types.Message):
    await message.reply("⏳ Gemini 3.7 формирует пост, а Nano Banana 2 Lite создает арт (ожидание до 60-90 сек)...")
    success, res = await generate_and_publish_post(with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_youtube"))
async def cmd_post_yt(message: types.Message):
    await message.reply("🎬 Забираю видео и формирую выжимку...")
    success, res = await generate_and_publish_post(RUBRIC_YOUTUBE, with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_recipe"))
async def cmd_post_rec(message: types.Message):
    await message.reply("🥗 Nano Banana 2 Lite генерирует фото блюда и рецепт (ожидание до 60-90 сек)...")
    success, res = await generate_and_publish_post(RUBRIC_RECIPES, with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_myth"))
async def cmd_post_my(message: types.Message):
    await message.reply("💡 Gemini 3.7 развенчивает миф, Nano Banana генерирует арт...")
    success, res = await generate_and_publish_post(RUBRIC_MYTHS, with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

# --- Тестовые команды БЕЗ генерации картинки (быстро, 0 кредитов) ---
@dp.message(Command("test_text"))
async def cmd_test_text(message: types.Message):
    await message.reply("⚡ Gemini 3.7 генерирует тестовый пост без картинки...")
    success, res = await generate_and_publish_post(with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_youtube"))
async def cmd_test_yt(message: types.Message):
    await message.reply("⚡ Забираю YouTube-транскрипт и генерирую выжимку (без арта)...")
    success, res = await generate_and_publish_post(RUBRIC_YOUTUBE, with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_recipe"))
async def cmd_test_rec(message: types.Message):
    await message.reply("⚡ Генерирую рецепт через Gemini 3.7 (без арта)...")
    success, res = await generate_and_publish_post(RUBRIC_RECIPES, with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_myth"))
async def cmd_test_my(message: types.Message):
    await message.reply("⚡ Разбор мифа через Gemini 3.7 (без арта)...")
    success, res = await generate_and_publish_post(RUBRIC_MYTHS, with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_science"))
async def cmd_test_sci(message: types.Message):
    await message.reply("⚡ Научный дайджест РКО / PubMed через Gemini 3.7 (без арта)...")
    success, res = await generate_and_publish_post(RUBRIC_ACADEMIC_SCIENCE, with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

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

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(generate_and_publish_post, "cron", hour=10, minute=0)
    scheduler.add_job(generate_and_publish_post, "cron", hour=18, minute=30)
    scheduler.start()

    logging.info("Служба расписания и боты успешно запущены на KIE.ai!")

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
