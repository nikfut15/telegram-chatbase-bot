import os
import requests
from fastapi import FastAPI, Request

app = FastAPI()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHATBASE_API_KEY = os.getenv("CHATBASE_API_KEY")
CHATBASE_CHATBOT_ID = os.getenv("CHATBASE_CHATBOT_ID")

if not TELEGRAM_TOKEN or not CHATBASE_API_KEY or not CHATBASE_CHATBOT_ID:
    raise RuntimeError(
        "Missing required environment variables: "
        "TELEGRAM_TOKEN, CHATBASE_API_KEY, CHATBASE_CHATBOT_ID"
    )

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
CHATBASE_API = "https://www.chatbase.co/api/v1/chat"


def send_telegram_message(chat_id: int, text: str) -> None:
    response = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
        },
        timeout=30,
    )
    response.raise_for_status()


def ask_chatbase(message_text: str, conversation_id: str, contact_id: str) -> str:
    payload = {
        "chatbotId": CHATBASE_CHATBOT_ID,
        "messages": [
            {
                "role": "user",
                "content": message_text,
            }
        ],
        "conversationId": conversation_id,
        "contactId": contact_id,
        "stream": False,
    }

    response = requests.post(
        CHATBASE_API,
        headers={
            "Authorization": f"Bearer {CHATBASE_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()

    # Chatbase usually returns the final answer in "text".
    answer = data.get("text")
    if answer:
        return answer

    # Fallbacks for slightly different response shapes.
    if isinstance(data.get("message"), str):
        return data["message"]

    return "Извините, я не смог сформировать ответ."


@app.get("/")
def root():
    return {"ok": True, "message": "Telegram + Chatbase bot is running"}


@app.post("/webhook")
async def webhook(request: Request):
    update = await request.json()

    message = update.get("message")
    if not message:
        return {"ok": True}

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text")

    if not chat_id or not text:
        return {"ok": True}

    # Один и тот же пользователь Telegram продолжает один и тот же диалог в Chatbase.
    conversation_id = f"tg_{chat_id}"
    contact_id = f"tg_{chat_id}"

    if text == "/start":
        send_telegram_message(
            chat_id,
            "Привет! Я подключён к Chatbase. Пиши вопрос — и я отвечу с сохранением контекста."
        )
        return {"ok": True}

    if text == "/help":
        send_telegram_message(
            chat_id,
            "Команды:\n/start — запуск\n/help — помощь\n\n"
            "Просто пиши сообщения, контекст сохраняется по твоему Telegram ID."
        )
        return {"ok": True}

    try:
        answer = ask_chatbase(text, conversation_id, contact_id)
        send_telegram_message(chat_id, answer)
    except Exception:
        send_telegram_message(
            chat_id,
            "Произошла ошибка при обращении к AI. Проверь переменные окружения "
            "и настройки Chatbase."
        )

    return {"ok": True}
