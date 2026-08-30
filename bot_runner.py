async def generate_and_publish_post(category: str = None) -> tuple[bool, str]:
    if not ai_client:
        err = "GEMINI_API_KEY не установлен!"
        logging.error(err)
        return False, err
    
    import random
    selected_topic = category or random.choice(CATEGORIES)
    logging.info(f"Генерация поста: {selected_topic}")

    prompt = (
        f"Напиши готовый экспертный пост НА РУССКОМ ЯЗЫКЕ для Telegram-канала «Липидограм» на тему: {selected_topic}. "
        "Длина: 900-1300 символов. Обязательно вставь кликабельную ссылку на первоисточник через <a href='URL'>Источник</a> "
        "(используй официальные ресурсы: scardio.ru, pubmed.ncbi.nlm.nih.gov или escardio.org). "
        "Опирайся на доказательную медицину, клинические рекомендации РКО и гайдлайны ESC/AHA."
    )

    models_to_try = ['gemini-3.6-flash', 'gemini-3.7-flash', 'gemini-3.5-flash']
    post_text = None
    last_error = None

    for model_name in models_to_try:
        try:
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
            logging.warning(f"Модель {model_name} вернула ошибку: {e}, пробуем следующую...")

    if not post_text:
        return False, f"Ошибка генерации: {last_error}"

    try:
        verified_text = verify_and_fix_urls(post_text)

        # Публикуем в канал через bot_poster
        sent_msg = await bot_poster.send_message(
            chat_id=CHANNEL_ID,
            text=verified_text,
            parse_mode="HTML",
            disable_web_page_preview=False
        )
        logging.info(f"Пост успешно опубликован в {CHANNEL_ID}! ID: {sent_msg.message_id}")
        return True, "Пост успешно опубликован в канал @lipidogram!"
    except Exception as e:
        err_msg = f"Ошибка отправки сообщения в канал: {e}"
        logging.error(err_msg)
        return False, err_msg
