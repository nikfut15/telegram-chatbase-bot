import os
import requests
from fastapi import FastAPI, Request

app = FastAPI()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHATBASE_API_KEY = os.getenv("CHATBASE_API_KEY")
CHATBASE_CHATBOT_ID = os.getenv("CHATBASE_CHATBOT_ID")  # сюда вставляешь agentId

if not TELEGRAM_TOKEN or not CHATBASE_API_KEY or not CHATBASE_CHATBOT_ID:
    raise RuntimeError(
        "Missing required environment variables: "
        "TELEGRAM_TOKEN, CHATBASE_API_KEY, CHATBASE_CHATBOT_ID"
    )

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
CHATBASE_API = f"https://www.chatbase.co/api/v2/agents/{CHATBASE_CHATBOT_ID}/chat"


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


def ask_chatbase(message_text: str, conversation_id: str, user_id: str) -> str:
    response = requests.post(
        CHATBASE_API,
        headers={
            "Authorization": f"Bearer {CHATBASE_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "message": message_text,
            "conversationId": conversation_id,
            "userId": user_id,
            "stream": False
        },
        timeout=60,
    )
    response.raise_for_status()

    data = response.json()

    # На случай если ответ пришёл строкой
    if isinstance(data, str):
        return data

    # На случай если ответ пришёл объектом
    if isinstance(data, dict):
        if isinstance(data.get("text"), str):
            return data["text"]
        if isinstance(data.get("message"), str):
            return data["message"]
        if isinstance(data.get("answer"), str):
            return data["answer"]

    return "Извините, я не смог сформировать ответ."


@app.get("/")
def root():
    return {"ok": True, "message": "Telegram + Chatbase agent bot is running"}


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

    # Стабильный ID диалога для одного и того же пользователя
    conversation_id = f"tg_{chat_id}"
    user_id = f"tg_{chat_id}"

    if text == "/start":
        send_telegram_message(
            chat_id,
            "Здравствуйте! Меня зовут Электроник. Буду рад Вам помочь! Просто напишите Ваш вопрос в чат."
        )
        return {"ok": True}

    if text == "/help":
        send_telegram_message(
            chat_id,
            "Команды:\n/start — запуск\n/help — помощь\n\n"
            "Просто пишите сообщения. Я стараюсь сохранять контекст диалога."
        )
        return {"ok": True}

    try:
        answer = ask_chatbase(text, conversation_id, user_id)
        send_telegram_message(chat_id, answer)
    except Exception as e:
        send_telegram_message(
            chat_id,
            f"Ошибка при обращении к Chatbase: {str(e)}"
        )

    return {"ok": True}
