import os
import re
import asyncio
import logging
import requests
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

MODERATOR_TOKEN = os.getenv("MODERATOR_BOT_TOKEN")
POSTER_TOKEN = os.getenv("POSTER_BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@lipidogram").strip()
PORT = int(os.getenv("PORT", 8080))

if not MODERATOR_TOKEN:
    raise ValueError("MODERATOR_BOT_TOKEN не задан в переменных окружения!")

# Инициализируем ботов
bot_moderator = Bot(token=MODERATOR_TOKEN)
bot_poster = Bot(token=POSTER_TOKEN) if POSTER_TOKEN else bot_moderator
ai_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

dp_moderator = Dispatcher()
dp_poster = Dispatcher()
user_warnings = {}

BAD_WORDS_PATTERN = re.compile(
    r'\b(ху[йиеяё]|пизд|бл[яе]|еб[аелиотс]|сук[аи]|муд[ао]|говно|залуп|чмо|дерьм|шлюх|гандон)\w*',
    re.IGNORECASE
)
SPAM_LINKS_PATTERN = re.compile(
    r'(https?://\S+|t\.me/\S+|telegram\.me/\S+|@[a-zA-Z0-9_]{5,})',
    re.IGNORECASE
)

CATEGORIES = [
    "научные новости кардиологии и липидологии по гайдлайнам РКО (Российского кардиологического общества, scardio.ru) и НОА (целевые нормы ЛПНП, АпоВ, триглицериды, шкала SCORE-2)",
    "влияние спорта и физической активности на сердце и сосуды (British Journal of Sports Medicine / ACSM / ESC: кардио 2-й пульсовой зоны, шаги, силовые нагрузки, влияние на ЛПВП и эндотелий)",
    "гиполипидемический кулинарный рецепт для снижения ЛПНП (насыщенные жиры < 2г на порцию, растворимая клетчатка > 6г, овсяный бета-глюкан, бобовые, пектины, омега-3)",
    "разбор популярного мифа доказательной медициной на русском языке (яйца и холестерин, статины и печень, кофе и сосуды, омега-3, чесночные чистки)",
    "сон, стресс и биомаркеры сердца (вариабельность сердечного ритма, кортизол и их связь с липидным обменом)"
]

SYSTEM_PROMPT = """
Ты — ведущий научный редактор русскоязычного Telegram-канала «Липидограм» (@lipidogram).
Твоя цель — писать увлекательные, высокопрофессиональные, строго научно достоверные посты ИСКЛЮЧИТЕЛЬНО НА РУССКОМ ЯЗЫКЕ.

База авторитетных источников:
1. Отечественные стандарты: РКО (Российское кардиологическое общество, scardio.ru), Российский кардиологический журнал, клинические рекомендации НОА и Минздрава РФ.
2. Мировые кардио-ассоциации: ESC (European Society of Cardiology), AHA/ACC (Circulation, JACC), The Lancet.
3. Доказательный спорт: British Journal of Sports Medicine (BJSM), ACSM (American College of Sports Medicine), European Journal of Preventive Cardiology.
4. Научные рецензируемые базы: PubMed / NCBI.

Правила публикации (HTML-разметка Telegram):
• Заголовок: Яркий, интригующий, с эмодзи (в тегах <b>Заголовок</b>).
• Введение: 1-2 предложения, почему этот вопрос важен для каждого человека и здоровья сосудов.
• Научная суть: 3-4 четких тезиса с цифрами, процентами и фактами на понятном русском языке.
• Практический совет: Что конкретно сделать читателю (в рационе, тренировках или контроле анализов).
• Первоисточник: Обязательно оформи в виде кликабельной ссылки: <a href="РЕАЛЬНЫЙ_URL">Название исследования / РКО / PubMed / ESC</a>.
• Хештеги в конце: #Липидограм_Наука #РКО #ЗдоровьеСердца #СпортИСосуды #ЛПНП.

Текст должен быть живым, грамотным и доступным широкому кругу читателей, без лженауки.
"""

def verify_and_fix_urls(html_text: str) -> str:
    urls = re.findall(r'href=["\'](https?://[^"\']+)["\']', html_text)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    for url in urls:
        try:
            resp = requests.head(url, headers=headers, timeout=5, allow_redirects=True)
            if resp.status_code >= 400:
                safe_fallback = "https://scardio.ru" if "scardio" in url or "rko" in url else "https://pubmed.ncbi.nlm.nih.gov"
                html_text = html_text.replace(url, safe_fallback)
                logging.warning(f"Ссылка {url} заменена на {safe_fallback}")
        except Exception:
            safe_fallback = "https://pubmed.ncbi.nlm.nih.gov"
            html_text = html_text.replace(url, safe_fallback)
            
    return html_text

async def generate_and_publish_post(category: str = None) -> tuple[bool, str]:
    if not ai_client:
        err = "GEMINI_API_KEY не установлен!"
        logging.error(err)
        return False, err
    
    import random
    selected_topic = category or random.choice(CATEGORIES)
    logging.info(f"Генерация поста: {selected_topic}")

    prompt = (
        f"Найди актуальную научную информацию или исследование за 2023-2026 годы и напиши готовый пост НА РУССКОМ ЯЗЫКЕ для Telegram на тему: {selected_topic}. "
        "Длина: 900-1300 символов. Обязательно вставь прямую кликабельную ссылку на первоисточник через <a href='URL'>Источник</a>."
    )

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.7,
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())]
            )
        )
        post_text = response.text
        verified_text = verify_and_fix_urls(post_text)

        # Публикуем в канал через bot_poster
        sent_msg = await bot_poster.send_message(
            chat_id=CHANNEL_ID,
            text=verified_text,
            parse_mode="HTML",
            disable_web_page_preview=False
        )
        logging.info(f"Пост успешно опубликован в {CHANNEL_ID}! ID сообщения: {sent_msg.message_id}")
        return True, "Пост успешно опубликован в канал!"
    except Exception as e:
        err_msg = f"Ошибка публикации в канал: {e}"
        logging.error(err_msg)
        return False, err_msg

# --- Обработчики команд для ОБОИХ ботов ---

async def handle_start(message: types.Message):
    await message.reply("🫀 Бот «Липидограм» активен.\n\nОтправьте команду /post_now для немедленной публикации поста в канал!")

async def handle_post_now(message: types.Message):
    await message.reply("⏳ Начинаю генерацию поста (РКО / PubMed / ESC) и отправку в канал...")
    success, result_text = await generate_and_publish_post()
    if success:
        await message.reply("✅ " + result_text)
    else:
        await message.reply("❌ Не удалось опубликовать пост:\n" + result_text)

# Регистрируем команды на обоих ботах
dp_poster.message.register(handle_start, Command("start"))
dp_poster.message.register(handle_post_now, Command("post_now"))
dp_moderator.message.register(handle_start, Command("start"))
dp_moderator.message.register(handle_post_now, Command("post_now"))

# --- Модерация комментариев ---
@dp_moderator.message(F.text)
async def handle_comment(message: types.Message):
    if message.chat.type == "private":
        return
    if message.sender_chat and message.sender_chat.type == "channel":
        return

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

    if is_violation:
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
    return web.Response(text="Lipidogram Bot Service is running 24/7!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {PORT}")

async def main():
    await start_web_server()

    # График публикаций: в 10:00 и 18:30 МСК
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(generate_and_publish_post, "cron", hour=10, minute=0)
    scheduler.add_job(generate_and_publish_post, "cron", hour=18, minute=30)
    scheduler.start()

    logging.info("Оба бота (Медиа и Модератор) успешно запущены в режиме Polling!")
    
    # Запускаем опрос обоих ботов одновременно
    await asyncio.gather(
        dp_moderator.start_polling(bot_moderator),
        dp_poster.start_polling(bot_poster)
    )

if __name__ == "__main__":
    asyncio.run(main())
