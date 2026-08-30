import os
import re
import asyncio
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from google import genai

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Настройки из переменных окружения
MODERATOR_TOKEN = os.getenv("MODERATOR_BOT_TOKEN")
POSTER_TOKEN = os.getenv("POSTER_BOT_TOKEN", MODERATOR_TOKEN)
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@lipidogram")
ADMIN_ID = int(os.getenv("ADMIN_USER_ID", "0"))

if not MODERATOR_TOKEN:
    raise ValueError("MODERATOR_BOT_TOKEN не задан в переменных окружения!")

# Инициализация ботов и ИИ
bot_moderator = Bot(token=MODERATOR_TOKEN)
bot_poster = Bot(token=POSTER_TOKEN) if POSTER_TOKEN else bot_moderator
ai_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

dp = Dispatcher()
user_warnings = {}

# Регулярные выражения для фильтрации комментариев
BAD_WORDS_PATTERN = re.compile(
    r'\b(ху[йиеяё]|пизд|бл[яе]|еб[аелиотс]|сук[аи]|муд[ао]|говно|залуп|чмо|дерьм|шлюх|гандон)\w*',
    re.IGNORECASE
)
SPAM_LINKS_PATTERN = re.compile(
    r'(https?://\S+|t\.me/\S+|telegram\.me/\S+|@[a-zA-Z0-9_]{5,})',
    re.IGNORECASE
)

CATEGORIES = [
    "научный дайджест (свежее исследование PubMed/ESC/AHA про ЛПНП, АпоВ, триглицериды или растворимую клетчатку)",
    "гиполипидемический кулинарный рецепт (насыщенные жиры < 2г на порцию, растворимая клетчатка > 6г, бета-глюкан/пектин)",
    "разбор популярного ЗОЖ-мифа о холестерине, яйцах, статинах, омега-3 или чистке сосудов",
    "кардио-тренировки и зоны пульса: как физическая активность меняет липидный профиль и сосуды"
]

SYSTEM_PROMPT = """
Ты — главный научный редактор Telegram-канала «Липидограм» (@lipidogram).
Твоя цель — создавать интересные, строго научно достоверные и привлекательные посты по доказательной кардиологии, нутрициологии и контролю уровня ЛПНП (холестерина).

Структура публикации:
1. Заголовок с яркими эмодзи (в теге <b>Заголовок</b>).
2. Суть исследования или темы простым и увлекательным языком (без сухой академической воды).
3. Практический вывод для читателя (что съесть, как скорректировать привычки).
4. Указание авторитетного первоисточника (например: ESC Guidelines, JACC, Circulation, The Lancet, PubMed).
5. Тематические хештеги в конце (#Липидограм_Наука, #Рецепт_ЛПНП, #ЗдоровьеСердца).
6. Никакой лженауки — только доказательная медицина. Форматируй строго в HTML.
"""

async def generate_and_publish_post(category: str = None):
    if not ai_client:
        logging.warning("GEMINI_API_KEY не установлен, генерация пропущена.")
        return
    
    import random
    selected_topic = category or random.choice(CATEGORIES)
    logging.info(f"Генерация поста на тему: {selected_topic}")

    prompt = f"Напиши готовый для публикации пост для Telegram-канала на тему: {selected_topic}. Длина 900-1400 символов. Используй HTML-теги (<b>, <i>, <code>)."
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.7,
            )
        )
        post_text = response.text
        await bot_poster.send_message(
            chat_id=CHANNEL_ID,
            text=post_text,
            parse_mode="HTML",
            disable_web_page_preview=False
        )
        logging.info("Пост успешно опубликован в канал!")
    except Exception as e:
        logging.error(f"Ошибка при генерации или отправке поста: {e}")

# --- Обработчики команд и модерации ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.reply("🛡️ Бот-модератор «Липидограм» активен и готов защищать комментарии.")

@dp.message(Command("post_now"))
async def cmd_post_now(message: types.Message):
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        return
    await message.reply("⏳ Генерирую и отправляю пост в канал...")
    await generate_and_publish_post()
    await message.reply("✅ Пост успешно опубликован!")

@dp.message(F.text)
async def handle_comment(message: types.Message):
    # Игнорируем посты самого канала
    if message.sender_chat and message.sender_chat.type == "channel":
        return

    # Игнорируем администраторов группы
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
            logging.error(f"Не удалось удалить сообщение: {e}")

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
                    f"⛔ {user_mention} получает мут на 24 часа. Предупреждение: <b>2/3</b>.",
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

async def main():
    # Настройка планировщика автопостинга (в 10:00 и 18:00 по МСК)
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(generate_and_publish_post, "cron", hour=10, minute=0)
    scheduler.add_job(generate_and_publish_post, "cron", hour=18, minute=0)
    scheduler.start()

    logging.info("Службы модерации и автопостинга успешно запущены!")
    await dp.start_polling(bot_moderator)

if __name__ == "__main__":
    asyncio.run(main())
