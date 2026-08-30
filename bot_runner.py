import os
import re
import asyncio
import logging
import requests
from bs4 import BeautifulSoup
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

RUBRICS = [
    {
        "category": "🇷🇺 ОТКРЫТЫЕ НОВОСТИ РОССИЙСКОЙ КАРДИОЛОГИИ (РКО)",
        "source_type": "rko",
        "query": "липиды холестерин атеросклероз",
        "ru_theme": "Свежие открытые новости Российского кардиологического общества (РКО) и журнала РКЖ",
        "hashtags": "#Липидограм_РКО #КардиологияРФ #РКО #ЗдоровьеСердца"
    },
    {
        "category": "🥗 ГИПОЛИПИДЕМИЧЕСКАЯ КУХНЯ",
        "source_type": "pubmed",
        "query": "(soluble dietary fiber OR beta-glucan OR Mediterranean diet) AND (cholesterol OR lipid profile)",
        "ru_theme": "Кулинарный гиполипидемический рецепт (насыщенные жиры менее 2г, растворимая клетчатка более 6г, овес, бобовые, омега-3)",
        "hashtags": "#Рецепт_ЛПНП #УмнаяЗамена #ПитаниеСердца #Клетчатка"
    },
    {
        "category": "🏃 СПОРТ И СОСУДЫ (2025-2026)",
        "source_type": "pubmed",
        "query": "(Zone 2 aerobic exercise OR resistance training) AND (endothelial function OR HDL-C OR cardiovascular risk)",
        "ru_theme": "Спорт и сосуды: кардио во 2-й пульсовой зоне, силовые тренировки, шаги и их влияние на ЛПВП и эндотелий",
        "hashtags": "#СпортИСосуды #ЗдоровьеСердца #ПульсовыеЗоны #Кардио"
    },
    {
        "category": "💡 РАЗБОР МИФОВ ДОКАЗАТЕЛЬНОЙ МЕДИЦИНОЙ",
        "source_type": "pubmed",
        "query": "(dietary cholesterol OR eggs OR statins safety OR omega-3) AND (systematic review OR trial)",
        "ru_theme": "Разбор популярного мифа (яйца и холестерин, статины и печень, кофе и сосуды, чистки сосудов)",
        "hashtags": "#Мифы_Липидограм #Доказательно #Холестерин"
    },
    {
        "category": "🔬 МЕЖДУНАРОДНЫЙ НАУЧНЫЙ ДАЙДЖЕСТ (2025-2026)",
        "source_type": "pubmed",
        "query": "(LDL-C OR Apolipoprotein B OR atherosclerosis) AND (meta-analysis OR clinical trial)",
        "ru_theme": "Свежие международные мета-анализы липидологии (ЛПНП, АпоВ, триглицериды, шкала SCORE-2)",
        "hashtags": "#Липидограм_Наука #Кардиология #ЛПНП #PubMed"
    }
]

SYSTEM_PROMPT = """
Ты — ведущий научный редактор русскоязычного Telegram-канала «Липидограм» (@lipidogram).
Твоя задача — писать увлекательные, кристально понятные, профессиональные посты ИСКЛЮЧИТЕЛЬНО НА РУССКОМ ЯЗЫКЕ.

Правила публикации:
1. Заголовок: Яркий, привлекательный, с тематическими эмодзи (в тегах <b>Заголовок</b>).
2. Введение: 1-2 предложения, почему тема важна для здоровья сердца, сосудов и долголетия.
3. Научная суть: 3-4 емких тезиса с фактами, цифрами (граммами, процентами) на понятном русском языке.
4. Практический совет: Четкое действие для читателя (в тарелке, на тренировке или в лаборатории).
5. Первоисточник: Кликабельная ссылка СТРОГО на предоставленный реальный URL: <a href="ТОЧНЫЙ_URL_СТАТЬИ">Название статьи / Журнал / Источник</a>.
6. Хештеги рубрики в самом конце.

Используй только валидные теги Telegram: <b>, </b>, <i>, </i>, <code>, </code>, <a href="...">.
Все знаки «меньше» или «больше» пиши словами («менее», «более») или экранируй, чтобы не ломать HTML-разметку.
"""

def fetch_rko_news() -> dict:
    """Парсит открытые публичные новости с сайта РКО (scardio.ru/news), доступные без регистрации."""
    try:
        url = "https://scardio.ru/news/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            links = soup.find_all("a", href=True)
            valid_news = []
            
            for a in links:
                href = a["href"]
                title = a.get_text(strip=True)
                if "/news/" in href and len(title) > 25 and not href.endswith("/news/"):
                    full_url = f"https://scardio.ru{href}" if href.startswith("/") else href
                    if not any(x in full_url for x in ["page=", "category=", "archive"]):
                        valid_news.append({"title": title, "url": full_url})
            
            if valid_news:
                import random
                selected = random.choice(valid_news[:10])
                return {
                    "title": selected["title"],
                    "journal": "Новости Российского кардиологического общества (РКО)",
                    "year": "2025-2026",
                    "url": selected["url"]
                }
    except Exception as e:
        logging.warning(f"Ошибка получения открытых новостей РКО ({e})")

    return {
        "title": "Открытые научно-клинические новости кардиологии",
        "journal": "Российское кардиологическое общество (РКО)",
        "year": "2025-2026",
        "url": "https://scardio.ru/news/"
    }

def fetch_fresh_pubmed_study(query: str) -> dict:
    """Ищет свежие статьи за 2024-2026 годы через официальный открытый NCBI Entrez API."""
    try:
        search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        params = {
            "db": "pubmed",
            "term": query,
            "mindate": "2024/01/01",
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
        pmid = random.choice(id_list)

        summary_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
        sum_res = requests.get(summary_url, params={"db": "pubmed", "id": pmid, "retmode": "json"}, timeout=7)
        sum_data = sum_res.json()
        result = sum_data.get("result", {}).get(pmid, {})

        title = result.get("title", "Cardiovascular Study")
        source = result.get("source", "PubMed")
        pubdate = result.get("pubdate", "")

        return {
            "pmid": pmid,
            "title": title,
            "journal": source,
            "year": pubdate,
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

    if rubric.get("source_type") == "rko":
        study = fetch_rko_news()
        prompt = (
            f"Напиши готовый пост НА РУССКОМ ЯЗЫКЕ для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
            f"Тема: {rubric['ru_theme']}\n"
            f"Российский открытый первоисточник: {study['title']}\n"
            f"Организация/Журнал: {study['journal']} ({study['year']})\n"
            f"Ссылка на открытый материал: {study['url']}\n\n"
            f"В блоке Первоисточник поставь ТОЧНО эту ссылку: <a href='{study['url']}'>{study['title']} / {study['journal']}</a>.\n"
            f"В самом конце обязательно добавь хештеги: {rubric['hashtags']}"
        )
    else:
        study = fetch_fresh_pubmed_study(rubric['query'])
        if study:
            prompt = (
                f"Напиши готовый пост НА РУССКОМ ЯЗЫКЕ для Telegram-канала «Липидограм» в рубрику «{rubric['category']}».\n"
                f"Тема публикации: {rubric['ru_theme']}\n"
                f"Реальные данные статьи из PubMed:\n"
                f"Название: {study['title']}\n"
                f"Журнал: {study['journal']} ({study['year']})\n"
                f"PMID: {study['pmid']}\n"
                f"URL: {study['url']}\n\n"
                f"В блоке Первоисточник поставь ТОЧНО эту ссылку: <a href='{study['url']}'>{study['title']} / {study['journal']} (PMID: {study['pmid']})</a>.\n"
                f"В самом конце добавь хештеги: {rubric['hashtags']}"
            )
        else:
            prompt = (
                f"Напиши готовый пост НА РУССКОМ ЯЗЫКЕ для Telegram-канала «Липидограм» в рубрику «{rubric['category']}» на тему: {rubric['ru_theme']}.\n"
                "Опирайся на доказательную медицину и клинические рекомендации РКО/ESC.\n"
                "В первоисточнике укажи ссылку на открытые новости РКО: <a href='https://scardio.ru/news/'>Открытые новости Российского кардиологического общества (РКО)</a>.\n"
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
    await message.reply("🫀 Медиа-бот «Липидограм» активен.\n\nКоманда /post_now — публикация следующего поста из контент-плана (Открытые новости РКО ➔ Рецепты ➔ Спорт ➔ Мифы ➔ Международная наука).")

@dp.message(Command("post_now"))
async def cmd_post_now(message: types.Message):
    await message.reply("⏳ Запрашиваю свежий материал (открытые новости РКО / PubMed) и формирую пост...")
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

    # График публикаций: в 10:00 и 18:30 по МСК
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
