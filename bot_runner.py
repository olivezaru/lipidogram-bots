import os
import re
import html
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
    "гиполипидемический кулинарный рецепт для снижения ЛПНП (насыщенные жиры менее 2г на порцию, растворимая клетчатка более 6г, овсяный бета-глюкан, бобовые, пектины, омега-3)",
    "разбор популярного мифа доказательной медициной на русском языке (яйца и холестерин, статины и печень, кофе и сосуды, омега-3, чесночные чистки)",
    "сон, стресс и биомаркеры сердца (вариабельность сердечного ритма, кортизол и их связь с липидным обменом)"
]

SYSTEM_PROMPT = """
Ты — ведущий научный редактор русскоязычного Telegram-канала «Липидограм» (@lipidogram).
Твоя цель — писать увлекательные, высокопрофессиональные, научно достоверные посты ИСКЛЮЧИТЕЛЬНО НА РУССКОМ ЯЗЫКЕ.

База авторитетных источников:
1. Отечественные стандарты: РКО (Российское кардиологическое общество, scardio.ru), Российский кардиологический журнал, клинические рекомендации НОА и Минздрава РФ.
2. Мировые кардио-ассоциации: ESC (European Society of Cardiology), AHA/ACC (Circulation, JACC), The Lancet.
3. Доказательный спорт: British Journal of Sports Medicine (BJSM), ACSM.
4. База рецензируемых статей: PubMed / NCBI.

ВАЖНЫЕ ПРАВИЛА ОФОРМЛЕНИЯ И ССЫЛОК:
1. Используй ТОЛЬКО следующие HTML-теги: <b>, </b>, <i>, </i>, <code>, </code>, <a href="URL">текст</a>.
2. Если пишешь знаки «меньше» или «больше» (например: менее 1.4 ммоль/л или более 6г), пиши их словами («менее», «более») или экранируй, чтобы не ломать HTML.
3. Обязательно указывай прямую ссылку на конкретную статью или гайдлайн:
- Для PubMed: прямая ссылка с реальным PMID: https://pubmed.ncbi.nlm.nih.gov/32132159/ или https://pubmed.ncbi.nlm.nih.gov/31597828/
- Для РКО: https://scardio.ru/rekomendacii/rekomendacii_rko/ или архив https://russjcardiol.elpub.ru/
- Для ESC / DOI: https://doi.org/10.1093/eurheartj/ehz455

Формат поста:
• Заголовок: Яркий, с тематическими эмодзи (в тегах <b>Заголовок</b>).
• Введение: 1-2 предложения, актуальность для сосудов и здоровья.
• Научная суть: 3-4 четких тезиса простым языком с цифрами и фактами.
• Практический совет: Что конкретно делать читателю.
• Первоисточник: Кликабельная ссылка: <a href="ПРЯМАЯ_ССЫЛКА_НА_СТАТЬЮ">Название статьи / Журнал / PMID</a>.
• Хештеги: #Липидограм_Наука #РКО #ЗдоровьеСердца #СпортИСосуды #ЛПНП.

Никакого шарлатанства и лженауки — только строгая доказательная медицина.
"""

def sanitize_html_for_telegram(text: str) -> str:
    """Безопасно экранирует невалидные символы, сохраняя разрешенные теги Telegram."""
    # Сохраняем ссылки и теги
    # Заменяем случайные медицинские знаки < и >, которые не являются частью валидных тегов
    # Защищаем <a href="...">, </a>, <b>, </b>, <i>, </i>, <code>, </code>
    allowed_tags = ['<b>', '</b>', '<i>', '</i>', '<code>', '</code>', '</a>']
    
    # Временно маскируем валидные теги
    placeholders = {}
    
    def repl_tag(match):
        key = f"__TAG_{len(placeholders)}__"
        placeholders[key] = match.group(0)
        return key
    
    # Маскируем <a href="...">
    text = re.sub(r'<a\s+href=["\'][^"\']+["\']>', repl_tag, text, flags=re.IGNORECASE)
    
    # Маскируем остальные валидные теги
    for tag in allowed_tags:
        pattern = re.escape(tag)
        text = re.sub(pattern, repl_tag, text, flags=re.IGNORECASE)
    
    # Все оставшиеся < и > экранируем
    text = text.replace('<', '&lt;').replace('>', '&gt;')
    
    # Возвращаем валидные теги обратно
    for key, val in placeholders.items():
        text = text.replace(key, val)
        
    return text

def verify_and_fix_urls(html_text: str) -> str:
    """Проверяет ссылки. Если ссылка содержит прямой идентификатор (PMID / DOI / раздел РКО), сохраняет её."""
    urls = re.findall(r'href=["\'](https?://[^"\']+)["\']', html_text)
    headers = {"User-Agent": "Mozilla/5.0"}
    
    for url in urls:
        if re.search(r'pubmed\.ncbi\.nlm\.nih\.gov/\d+/?', url) or 'doi.org' in url or 'scardio.ru/rekomendacii' in url or 'russjcardiol' in url:
            continue
            
        try:
            resp = requests.head(url, headers=headers, timeout=5, allow_redirects=True)
            if resp.status_code >= 400:
                fallback = "https://scardio.ru/rekomendacii/rekomendacii_rko/" if "scardio" in url else "https://pubmed.ncbi.nlm.nih.gov/31597828/"
                html_text = html_text.replace(url, fallback)
        except Exception:
            fallback = "https://pubmed.ncbi.nlm.nih.gov/31597828/"
            html_text = html_text.replace(url, fallback)
            
    return html_text

async def generate_and_publish_post(category: str = None) -> tuple[bool, str]:
    if not ai_client:
        err = "GEMINI_API_KEY не установлен в переменных Render!"
        logging.error(err)
        return False, err

    if not bot_poster:
        err = "Бот для отправки не настроен (проверьте токены)!"
        logging.error(err)
        return False, err
    
    import random
    selected_topic = category or random.choice(CATEGORIES)
    logging.info(f"Генерация поста: {selected_topic}")

    prompt = (
        f"Напиши готовый экспертный пост НА РУССКОМ ЯЗЫКЕ для Telegram-канала «Липидограм» на тему: {selected_topic}. "
        "Длина: 900-1300 символов. Обязательно вставь прямую ссылку на конкретную статью (с точным номером PMID на pubmed.ncbi.nlm.nih.gov/НОМЕР/ или прямую ссылку на рекомендации РКО/ESC) через <a href='URL'>Название статьи / Журнал</a>. "
        "Опирайся на доказательную медицину, клинические рекомендации РКО и гайдлайны ESC/AHA."
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
                    logging.info(f"Успешная генерация с моделью: {model_name}")
                    break
            except Exception as e:
                last_error = e
                logging.warning(f"Модель {model_name} (попытка {attempt+1}) вернула ошибку: {e}")
                await asyncio.sleep(2)
                
        if post_text:
            break

    if not post_text:
        return False, f"Ошибка Gemini API: {last_error}"

    try:
        # 1. Проверяем валидность ссылок
        verified_text = verify_and_fix_urls(post_text)
        # 2. Очищаем и защищаем HTML для Telegram
        clean_html = sanitize_html_for_telegram(verified_text)

        try:
            sent_msg = await bot_poster.send_message(
                chat_id=CHANNEL_ID,
                text=clean_html,
                parse_mode="HTML",
                disable_web_page_preview=False
            )
        except Exception as html_err:
            logging.warning(f"Ошибка HTML-разметки ({html_err}), отправка в чистом текстовом виде...")
            # Если разметка всё равно вызвала сбой, отправляем как чистый текст
            raw_text = re.sub(r'<[^>]+>', '', verified_text)
            sent_msg = await bot_poster.send_message(
                chat_id=CHANNEL_ID,
                text=raw_text,
                disable_web_page_preview=False
            )

        logging.info(f"Пост опубликован в {CHANNEL_ID}! ID: {sent_msg.message_id}")
        return True, "Пост успешно опубликован в канал @lipidogram!"
    except Exception as e:
        err_msg = f"Ошибка отправки сообщения в Telegram: {e}"
        logging.error(err_msg)
        return False, err_msg

# --- Хэндлеры команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.reply("🫀 Бот «Липидограм» активен.\n\nОтправьте команду /post_now для немедленной генерации и публикации поста в канал!")

@dp.message(Command("post_now"))
async def cmd_post_now(message: types.Message):
    await message.reply("⏳ Генерирую пост с прямой ссылкой на статью (РКО/PubMed/ESC) и публикую в канал...")
    success, result_text = await generate_and_publish_post()
    if success:
        await message.reply("✅ " + result_text)
    else:
        await message.reply("❌ Не удалось опубликовать пост:\n" + result_text)

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

    # График публикаций: 10:00 и 18:30 по МСК
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(generate_and_publish_post, "cron", hour=10, minute=0)
    scheduler.add_job(generate_and_publish_post, "cron", hour=18, minute=30)
    scheduler.start()

    logging.info("Служба расписания и бот запущены!")

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
