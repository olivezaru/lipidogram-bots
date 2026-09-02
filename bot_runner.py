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
from aiogram.exceptions import TelegramRetryAfter
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

# Официальные YouTube каналы с прямыми Channel ID для RSS ленты (без блокировок со стороны YouTube)
YOUTUBE_CHANNELS_RSS = [
    {"name": "Dr. Peter Attia", "channel_id": "UCF_fDSgblvyC-hltP1t4gTg", "expert": "Питер Аттия (эксперт по липидологии и превентивной кардиологии)"},
    {"name": "Huberman Lab", "channel_id": "UC2D2CMWXMOVWx7giW1n3LIg", "expert": "Эндрю Хуберман (нейробиолог Стэнфордского университета)"},
    {"name": "Dr. Gil Carvalho / Nutrition Made Simple", "channel_id": "UCosmc75v-4N3A7OHr8G25Ew", "expert": "Гил Карвальо (исследователь доказательной диетологии)"},
    {"name": "Dr. Rhonda Patrick / FoundMyFitness", "channel_id": "UCWF9aXYms1JpTf_bkW_X27g", "expert": "Ронда Патрик (биомедицинский исследователь)"},
    {"name": "Dr. Brad Stanfield", "channel_id": "UCpcvPevmCfu_UhyvCc_1K8A", "expert": "Брэд Стэнфилд (врач превентивной медицины)"},
    {"name": "Simon Hill / The Proof", "channel_id": "UCE0f85hX8Qz2e0n_W1i5qKw", "expert": "Саймон Хилл (физиолог и автор доказательных обзоров)"},
    {"name": "Доктор Утин", "channel_id": "UCe1Qc_VqL_8rLq9a-tM_HwQ", "expert": "Алексей Утин (кардиохирург)"},
    {"name": "СМТ — Научный подход", "channel_id": "UCi1p7P6-O3sV3h98rW2g6mA", "expert": "Борис Цацулин (научно-популярный аналитик)"}
]

# Темы поиска реальных исследований на PubMed и Europe PMC
PUBMED_SEARCH_TOPICS = [
    {
        "query": '("LDL-C" OR "ApoB" OR "triglycerides") AND ("dietary intervention" OR "Mediterranean diet" OR "fiber") AND ("clinical trial" OR "meta-analysis")',
        "category": "🥗 КЛИНИЧЕСКАЯ ДИЕТОЛОГИЯ И ЛИПИДЫ",
        "hashtags": "#Липидограм_Питание #ЛПНП #Доказательно #Диета"
    },
    {
        "query": '("statins" OR "ezetimibe" OR "PCSK9" OR "bempedoic acid") AND ("cardiovascular risk" OR "atherosclerosis") AND ("randomized controlled trial" OR "meta-analysis")',
        "category": "💊 ФАРМАКОТЕРАПИЯ И АТЕРОСКЛЕРОЗ",
        "hashtags": "#Липидограм_Фарма #Статины #Атеросклероз #Кардиология"
    },
    {
        "query": '("exercise" OR "resistance training" OR "aerobic capacity" OR "step count") AND ("arterial stiffness" OR "endothelial function" OR "HDL") AND ("trial" OR "meta-analysis")',
        "category": "🏃 АКТИВНОСТЬ И ЭЛАСТИЧНОСТЬ СОСУДОВ",
        "hashtags": "#Липидограм_Движение #ЭластичностьСосудов #Сердце #Спорт"
    },
    {
        "query": '("coronary artery calcium" OR "lipoprotein(a)" OR "plaque regression" OR "imaging") AND ("atherosclerosis" OR "infarction") AND ("prospective" OR "trial")',
        "category": "🔬 ДИАГНОСТИКА И ФАКТОРЫ РИСКА",
        "hashtags": "#Липидограм_Диагностика #Лп_а #КальцийСосудов #Чекап"
    },
    {
        "query": '("dietary cholesterol" OR "eggs" OR "omega-3" OR "saturated fatty acids" OR "coffee") AND ("cardiovascular" OR "mortality") AND ("meta-analysis" OR "systematic review")',
        "category": "💡 РАЗБОР МИФОВ И ИССЛЕДОВАНИЙ",
        "hashtags": "#Липидограм_Мифы #НаучныйПодход #Холестерин"
    }
]

RECIPE_IDEAS_QUERIES = [
    '("soluble fiber" OR "legumes" OR "lentils" OR "chickpeas" OR "barley") AND ("LDL cholesterol") AND ("human")',
    '("extra virgin olive oil" OR "walnuts" OR "almonds" OR "polyphenols") AND ("endothelial" OR "lipid profile")',
    '("psyllium" OR "oat beta-glucan" OR "flaxseed") AND ("apolipoprotein B" OR "cholesterol lowering")'
]

SYSTEM_PROMPT = """
Ты — профессиональный медицинский научный журналист и главный редактор Telegram-канала «Липидограм» (@lipidogram).
Твоя задача — проанализировать предоставленный РЕАЛЬНЫЙ первоисточник (научную статью или выступление эксперта) и написать авторскую, глубокую и понятную выжимку.

ГЛАВНЫЕ ПРАВИЛА:
1. Пиши ТОЛЬКО на основе фактов из предоставленного текста первоисточника. Никаких выдуманных общих фраз.
2. Текст должен быть живым, конкретным: с указанием точных цифр, механизмов (например: как клетчатка связывает желчные кислоты, почему важен ApoB, как снизились маркеры в исследовании).
3. ЗАПРЕЩЕНЫ заезженные клише: «боул», «суперфуд», «чудо-средство», а также шаблонные абстрактные советы.
4. Объем текста: 600-850 символов (идеально для чтения под фото).
5. Разрешенные HTML теги: <b>, </b>, <i>, </i>, <code>, </code>, <a href="...">.
6. В самом конце обязательно кликабельная ссылка на первоисточник и хештеги.

ТРЕБОВАНИЯ К IMAGE PROMPT ("image_prompt"):
- На АНГЛИЙСКОМ языке.
- Описывай строго реалистичный кадр (например: "Close-up of fresh steamed salmon with asparagus and extra virgin olive oil, fine dining photography, 8k, natural light").

ВЕРНИ ОТВЕТ СТРОГО В JSON:
{
  "post_text": "...",
  "image_prompt": "..."
}
"""

def parse_model_json(raw_text: str) -> tuple[str, str]:
    if not raw_text:
        return None, None
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
        try:
            parsed = json.loads(match.group(0))
            if "code" in parsed and "msg" in parsed:
                return None, None
            post_text = parsed.get("post_text") or parsed.get("text") or parsed.get("post") or parsed.get("content")
            image_prompt = parsed.get("image_prompt") or parsed.get("prompt") or parsed.get("image")
            if post_text and len(str(post_text).strip()) > 50:
                return str(post_text).strip(), str(image_prompt or "healthy cardiology food lifestyle").strip()
        except Exception:
            pass
    return None, None

async def generate_kie_text_and_prompt(user_prompt: str) -> tuple[str, str]:
    if not KIE_KEY:
        raise ValueError("KIE_API_KEY не установлен в переменных окружения!")

    headers = {
        "Authorization": f"Bearer {KIE_KEY}",
        "x-goog-api-key": KIE_KEY,
        "Content-Type": "application/json"
    }

    gemini_payload = {
        "contents": [
            {
                "parts": [
                    {"text": f"{SYSTEM_PROMPT}\n\nЗАДАНИЕ НА ОСНОВЕ ПЕРВОИСТОЧНИКА:\n{user_prompt}"}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "responseMimeType": "application/json"
        }
    }

    openai_payload = {
        "model": "gemini-3.7-flash",
        "messages": [
            {"role": "system", "content": f"{SYSTEM_PROMPT}\n\nВерни ТОЛЬКО валидный JSON с ключами post_text и image_prompt."},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.7
    }

    endpoints = [
        ("gemini_native", f"https://api.kie.ai/gemini/v1beta/models/gemini-3.7-flash:generateContent?key={KIE_KEY}", gemini_payload),
        ("gemini_native", f"https://api.kie.ai/gemini/v1/models/gemini-3.7-flash:generateContent?key={KIE_KEY}", gemini_payload),
        ("chat_completions", "https://api.kie.ai/api/v1/chat/completions", openai_payload),
        ("chat_completions", "https://api.kie.ai/v1/chat/completions", openai_payload)
    ]

    last_error = ""
    async with aiohttp.ClientSession() as session:
        for retry in range(1, 3):
            for mode, target_url, payload in endpoints:
                try:
                    async with session.post(target_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=45)) as resp:
                        if resp.status == 200:
                            raw_json = await resp.json()
                            if isinstance(raw_json, dict) and (raw_json.get("code") in [500, 400, 401, 403] or "Server exception" in str(raw_json)):
                                last_error = f"KIE error: {raw_json.get('msg', 'Server exception')}"
                                continue

                            if mode == "gemini_native":
                                candidates = raw_json.get("candidates", []) if isinstance(raw_json, dict) else []
                                if candidates:
                                    parts = candidates[0].get("content", {}).get("parts", [])
                                    if parts:
                                        post_t, img_p = parse_model_json(parts[0].get("text", ""))
                                        if post_t:
                                            return post_t, img_p
                            elif mode == "chat_completions":
                                choices = raw_json.get("choices", [])
                                if choices:
                                    content = choices[0].get("message", {}).get("content", "")
                                    post_t, img_p = parse_model_json(content)
                                    if post_t:
                                        return post_t, img_p
                        else:
                            text_err = await resp.text()
                            last_error = f"HTTP {resp.status}: {text_err[:100]}"
                except Exception as e:
                    last_error = str(e)
                    continue

            if retry < 2:
                await asyncio.sleep(2)

    raise Exception(f"KIE.ai Gemini ошибка: {last_error}")

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

# --- ИСТОЧНИК 1: YouTube через официальные RSS-фиды каналов (без капчи и блокировок) ---
async def fetch_real_youtube_video() -> dict:
    selected_channel = random.choice(YOUTUBE_CHANNELS_RSS)
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={selected_channel['channel_id']}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(rss_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    xml_text = await resp.text()
                    root = ET.fromstring(xml_text)
                    ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "media": "http://search.yahoo.com/mrss/"}

                    entries = root.findall("atom:entry", ns)
                    if entries:
                        entry = random.choice(entries[:5])
                        video_id = entry.find("yt:videoId", ns).text
                        title = entry.find("atom:title", ns).text
                        link = entry.find("atom:link", ns).attrib.get("href")
                        
                        desc_elem = entry.find(".//media:description", ns)
                        description = desc_elem.text if desc_elem is not None else ""

                        # Пытаемся получить реальный транскрипт видео
                        transcript_text = ""
                        try:
                            t_list = YouTubeTranscriptApi.get_transcript(video_id, languages=['en', 'ru', 'en-US'])
                            transcript_text = " ".join([t['text'] for t in t_list[:100]])
                        except Exception:
                            transcript_text = description

                        content_summary = f"Название видео: {title}\nОписание/Транскрипт:\n{transcript_text[:2500]}"
                        yt_img = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

                        return {
                            "title": title,
                            "expert": selected_channel["expert"],
                            "journal": f"YouTube-канал {selected_channel['name']}",
                            "content": content_summary,
                            "image_url": yt_img,
                            "url": link
                        }
    except Exception as e:
        logging.warning(f"Ошибка парсинга YouTube RSS: {e}")

    # Резервный поиск по PubMed кардиологического видео-доклада
    return await fetch_pubmed_study('("Cardiovascular" OR "Atherosclerosis") AND ("Clinical Review" OR "Lecture")')

# --- ИСТОЧНИК 2: PubMed / NCBI с полным абстрактом ---
async def fetch_pubmed_study(custom_query: str = None) -> dict:
    if custom_query:
        query = custom_query
    else:
        topic = random.choice(PUBMED_SEARCH_TOPICS)
        query = topic["query"]

    search_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={urllib.parse.quote(query)}&mindate=2023/01/01&maxdate=2026/12/31&retmax=15&sort=pub_date&retmode=json"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(search_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as res:
                if res.status == 200:
                    data = await res.json()
                    id_list = data.get("esearchresult", {}).get("idlist", [])
                    if id_list:
                        random.shuffle(id_list)
                        for pmid in id_list[:5]:
                            fetch_url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={pmid}&retmode=xml"
                            async with session.get(fetch_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as xml_res:
                                if xml_res.status == 200:
                                    xml_bytes = await xml_res.read()
                                    root = ET.fromstring(xml_bytes)
                                    article = root.find(".//Article")
                                    if article is None:
                                        continue

                                    title_elem = article.find("ArticleTitle")
                                    title = "".join(title_elem.itertext()) if title_elem is not None else "Cardiovascular Clinical Study"

                                    journal_elem = article.find(".//Journal/Title")
                                    journal = journal_elem.text if journal_elem is not None else "PubMed"

                                    year_elem = article.find(".//JournalIssue/PubDate/Year")
                                    year = year_elem.text if year_elem is not None else "2024-2026"

                                    abstract_texts = root.findall(".//Abstract/AbstractText")
                                    abstract = "\n".join(["".join(elem.itertext()) for elem in abstract_texts if elem is not None])

                                    if len(abstract) > 120:
                                        return {
                                            "pmid": pmid,
                                            "title": title,
                                            "journal": f"{journal} ({year})",
                                            "content": f"Title: {title}\nJournal: {journal} ({year})\nAbstract:\n{abstract[:3000]}",
                                            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                                        }
    except Exception as e:
        logging.error(f"Ошибка PubMed API: {e}")

    return None

# --- ИСТОЧНИК 3: Российское кардиологическое общество (РКО) ---
async def fetch_rko_news() -> dict:
    base_section_url = "https://scardio.ru/news/novosti_obschestva/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(base_section_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    html_text = await resp.text()
                    soup = BeautifulSoup(html_text, "html.parser")
                    links = soup.find_all("a", href=True)
                    valid_news = []
                    for a in links:
                        href = a["href"]
                        title = a.get_text(strip=True)
                        if "/news/novosti_obschestva/" in href and len(title) > 25 and href != "/news/novosti_obschestva/":
                            full_url = f"https://scardio.ru{href}" if href.startswith("/") else href
                            valid_news.append({"title": title, "url": full_url})

                    if valid_news:
                        selected = random.choice(valid_news[:8])
                        article_text = selected["title"]
                        try:
                            async with session.get(selected["url"], headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as art_resp:
                                if art_resp.status == 200:
                                    art_html = await art_resp.text()
                                    art_soup = BeautifulSoup(art_html, "html.parser")
                                    paras = [p.get_text(strip=True) for p in art_soup.find_all("p") if len(p.get_text(strip=True)) > 40]
                                    if paras:
                                        article_text = "\n".join(paras[:5])
                        except Exception:
                            pass

                        return {
                            "title": selected["title"],
                            "journal": "Российское кардиологическое общество (РКО)",
                            "content": f"Заголовок: {selected['title']}\nТекст статьи:\n{article_text[:2500]}",
                            "url": selected["url"]
                        }
    except Exception as e:
        logging.warning(f"Ошибка РКО: {e}")

    # Если сайт РКО недоступен, берем рецензируемую статью по кардиологии из PubMed
    return await fetch_pubmed_study('("Russian" OR "guidelines" OR "cardiology") AND ("dyslipidemia" OR "atherosclerosis")')

def sanitize_html_for_telegram(text: str) -> str:
    if not text:
        return ""
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

async def generate_and_publish_post(mode: str = "auto", with_image: bool = True) -> tuple[bool, str]:
    if not KIE_KEY:
        return False, "KIE_API_KEY не установлен!"
    if not bot_poster:
        return False, "Бот для отправки не настроен!"

    study = None
    category = ""
    hashtags = ""
    img_bytes = None

    if mode == "youtube":
        study = await fetch_real_youtube_video()
        category = "📺 МИРОВОЙ НАУЧПОП / ВЫЖИМКА"
        hashtags = "#Липидограм_Видео #Научпоп #Долголетие #ЗдоровьеСосудов"
        if with_image and study and study.get("image_url"):
            try:
                async with aiohttp.ClientSession() as yt_s:
                    async with yt_s.get(study["image_url"], timeout=aiohttp.ClientTimeout(total=8)) as yt_r:
                        if yt_r.status == 200:
                            img_bytes = await yt_r.read()
            except Exception:
                pass
    elif mode == "recipe":
        rec_query = random.choice(RECIPE_IDEAS_QUERIES)
        study = await fetch_pubmed_study(rec_query)
        category = "🥗 ГИПОЛИПИДЕМИЧЕСКАЯ КУХНЯ И ПИТАНИЕ"
        hashtags = "#Рецепт_ЛПНП #УмнаяЗамена #ПитаниеСердца #Клетчатка"
    elif mode == "science":
        study = await fetch_rko_news()
        category = "🔬 НАУЧНЫЙ ДАЙДЖЕСТ (РКО / PUBMED)"
        hashtags = "#Липидограм_Наука #РКО #Кардиология #ЛПНП"
    elif mode == "myth":
        study = await fetch_pubmed_study('("dietary cholesterol" OR "eggs" OR "statins" OR "omega-3" OR "coffee") AND ("meta-analysis")')
        category = "💡 РАЗБОР МИФОВ ДОКАЗАТЕЛЬНОЙ МЕДИЦИНОЙ"
        hashtags = "#Мифы_Липидограм #Доказательно #Холестерин"
    else:
        # Авто-ротация случайной свежей темы
        modes = ["youtube", "recipe", "science", "myth", "pubmed_random"]
        chosen = random.choice(modes)
        return await generate_and_publish_post(mode=chosen, with_image=with_image)

    if not study:
        study = await fetch_pubmed_study()

    if not study:
        return False, "Не удалось получить первоисточник для поста."

    prompt = (
        f"Рубрика канала: «{category}».\n"
        f"ДАННЫЕ РЕАЛЬНОГО ПЕРВОИСТОЧНИКА:\n{study.get('content', '')}\n\n"
        f"Сделай емкую, глубокую авторскую выжимку этого материала.\n"
        f"В самом конце обязательно укажи первоисточник с ТОЧНОЙ ссылкой:\n"
        f"🔗 <a href='{study['url']}'>{study['title']} ({study.get('journal', 'Источник')})</a>\n"
        f"{hashtags}"
    )

    try:
        post_text, image_prompt = await generate_kie_text_and_prompt(prompt)
        if not post_text:
            return False, "Модель вернула пустой ответ."
    except Exception as e:
        return False, f"Ошибка Gemini 3.7: {e}"

    if with_image and not img_bytes and image_prompt:
        img_bytes = await generate_kie_image_bytes(image_prompt)

    clean_html = sanitize_html_for_telegram(post_text)

    # Безопасная отправка с защитой от Flood Control
    try:
        if img_bytes and len(img_bytes) > 2000:
            photo_file = BufferedInputFile(img_bytes, filename="lipidogram_art.jpg")
            if len(clean_html) <= 1024:
                await bot_poster.send_photo(chat_id=CHANNEL_ID, photo=photo_file, caption=clean_html, parse_mode="HTML")
            else:
                await bot_poster.send_photo(chat_id=CHANNEL_ID, photo=photo_file)
                await bot_poster.send_message(chat_id=CHANNEL_ID, text=clean_html, parse_mode="HTML", disable_web_page_preview=False)
            return True, f"Опубликован пост («{category}») с иллюстрацией!"

        await bot_poster.send_message(chat_id=CHANNEL_ID, text=clean_html, parse_mode="HTML", disable_web_page_preview=False)
        return True, f"Опубликован пост («{category}») без фото."
    except TelegramRetryAfter as flood_err:
        await asyncio.sleep(flood_err.retry_after + 1)
        await bot_poster.send_message(chat_id=CHANNEL_ID, text=clean_html, parse_mode="HTML")
        return True, f"Опубликован пост («{category}») после ожидания Flood Control."
    except Exception as e:
        return False, f"Ошибка отправки в Telegram: {e}"

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.reply(
        "🫀 <b>Медиа-бот «Липидограм» 2.0</b>\n\n"
        "<b>Команды с артом (Gemini 3.7 + Nano Banana 2 Lite):</b>\n"
        "• /post_now — случайный пост из свежих источников с фото\n"
        "• /post_youtube — реальная выжимка свежего YouTube видео\n"
        "• /post_recipe — доказательный рецепт с фото\n"
        "• /post_science — клинический дайджест РКО/PubMed\n"
        "• /post_myth — разбор мифа\n\n"
        "<b>🧪 Тестовые команды БЕЗ картинки (0 кредитов, мгновенно):</b>\n"
        "• /test_text — случайный пост (только текст)\n"
        "• /test_youtube — проверка реального YouTube видео\n"
        "• /test_recipe — проверка рецепта (только текст)\n"
        "• /test_science — проверка статьи РКО / PubMed\n"
        "• /test_myth — проверка мифа",
        parse_mode="HTML"
    )

@dp.message(Command("post_now"))
async def cmd_post_now(message: types.Message):
    await message.reply("⏳ Ищу свежую научную статью и генерирую пост с иллюстрацией...")
    success, res = await generate_and_publish_post(mode="auto", with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_youtube"))
async def cmd_post_yt(message: types.Message):
    await message.reply("🎬 Забираю свежий ролик через RSS экспертов и создаю пост с фото...")
    success, res = await generate_and_publish_post(mode="youtube", with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_recipe"))
async def cmd_post_rec(message: types.Message):
    await message.reply("🥗 Ищу клинические данные по гиполипидемической диете...")
    success, res = await generate_and_publish_post(mode="recipe", with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_science"))
async def cmd_post_sci(message: types.Message):
    await message.reply("🔬 Анализирую материалы РКО / PubMed...")
    success, res = await generate_and_publish_post(mode="science", with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("post_myth"))
async def cmd_post_my(message: types.Message):
    await message.reply("💡 Разбираю миф по мета-анализам...")
    success, res = await generate_and_publish_post(mode="myth", with_image=True)
    await message.reply("✅ " + res if success else "❌ " + res)

# --- Тестовые команды без картинок ---
@dp.message(Command("test_text"))
async def cmd_test_text(message: types.Message):
    await message.reply("⚡ Gemini 3.7 анализирует случайную статью (без фото)...")
    success, res = await generate_and_publish_post(mode="auto", with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_youtube"))
async def cmd_test_yt(message: types.Message):
    await message.reply("⚡ Парсю RSS эксперта YouTube и генерирую выжимку конкретного видео...")
    success, res = await generate_and_publish_post(mode="youtube", with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_recipe"))
async def cmd_test_rec(message: types.Message):
    await message.reply("⚡ Генерирую пост по питанию без арта...")
    success, res = await generate_and_publish_post(mode="recipe", with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_science"))
async def cmd_test_sci(message: types.Message):
    await message.reply("⚡ Забираю реальную научную публикацию РКО / PubMed...")
    success, res = await generate_and_publish_post(mode="science", with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(Command("test_myth"))
async def cmd_test_my(message: types.Message):
    await message.reply("⚡ Разбор мифа по свежему мета-анализу...")
    success, res = await generate_and_publish_post(mode="myth", with_image=False)
    await message.reply("✅ " + res if success else "❌ " + res)

@dp.message(F.text)
async def handle_comment(message: types.Message):
    if message.chat.type == "private" or (message.sender_chat and message.sender_chat.type == "channel"):
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
        reason = "нецензурная лексика"
    elif SPAM_LINKS_PATTERN.search(text) and "lipidogram" not in text:
        is_violation = True
        reason = "реклама / спам-ссылка"

    if is_violation and bot_moderator:
        try:
            await message.delete()
        except Exception:
            pass

        warnings = user_warnings.get(user_id, 0) + 1
        user_warnings[user_id] = warnings

        if warnings == 1:
            await message.answer(f"⚠️ {user_mention}, сообщение удалено ({reason}). Предупреждение: 1/3.", parse_mode="HTML")
        elif warnings == 2:
            until_date = datetime.now() + timedelta(days=1)
            try:
                await bot_moderator.restrict_chat_member(chat_id=message.chat.id, user_id=user_id, permissions=types.ChatPermissions(can_send_messages=False), until_date=until_date)
                await message.answer(f"⛔ {user_mention} переведен в режим чтения на 24 часа. (2/3).", parse_mode="HTML")
            except Exception:
                pass
        else:
            try:
                await bot_moderator.ban_chat_member(chat_id=message.chat.id, user_id=user_id)
                await message.answer(f"🚫 {user_mention} заблокирован (3/3).", parse_mode="HTML")
                user_warnings.pop(user_id, None)
            except Exception:
                pass

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
    scheduler.add_job(generate_and_publish_post, "cron", hour=10, minute=0, args=["auto", True])
    scheduler.add_job(generate_and_publish_post, "cron", hour=18, minute=30, args=["auto", True])
    scheduler.start()

    logging.info("Служба расписания и боты успешно запущены на KIE.ai!")

    if bot_poster:
        if bot_moderator and bot_moderator != bot_poster:
            await asyncio.gather(dp.start_polling(bot_poster), dp.start_polling(bot_moderator))
        else:
            await dp.start_polling(bot_poster)
    else:
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
