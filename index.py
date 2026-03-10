import os
import requests
from fastapi import FastAPI, Request

app = FastAPI()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHATBASE_API_KEY = os.getenv("CHATBASE_API_KEY")
CHATBASE_CHATBOT_ID = os.getenv("CHATBASE_CHATBOT_ID")  # сюда подставлен Agent ID

if not TELEGRAM_TOKEN or not CHATBASE_API_KEY or not CHATBASE_CHATBOT_ID:
    raise RuntimeError(
        "Missing required environment variables: "
        "TELEGRAM_TOKEN, CHATBASE_API_KEY, CHATBASE_CHATBOT_ID"
    )

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
CHATBASE_BASE = "https://www.chatbase.co/api/v2"
CHAT_URL = f"{CHATBASE_BASE}/agents/{CHATBASE_CHATBOT_ID}/chat"


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


def get_latest_ongoing_conversation(user_id: str) -> str | None:
    """
    Ищем последний активный диалог пользователя в Chatbase.
    Если его нет — вернём None и создадим новый.
    """
    response = requests.get(
        f"{CHATBASE_BASE}/agents/{CHATBASE_CHATBOT_ID}/users/{user_id}/conversations",
        headers={
            "Authorization": f"Bearer {CHATBASE_API_KEY}",
        },
        params={"limit": 20},
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()

    for conv in body.get("data", []):
        if conv.get("status") == "ongoing":
            return conv.get("id")

    return None


def ask_chatbase(message_text: str, user_id: str) -> str:
    """
    Если у пользователя уже есть ongoing conversation — продолжаем её.
    Если нет — создаём новую, передав только userId.
    """
    conversation_id = get_latest_ongoing_conversation(user_id)

    payload = {
        "message": message_text,
        "stream": False,
    }

    if conversation_id:
        payload["conversationId"] = conversation_id
    else:
        payload["userId"] = user_id

    response = requests.post(
        CHAT_URL,
        headers={
            "Authorization": f"Bearer {CHATBASE_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )

    # Если разговор закончился/перехвачен, стартуем новый
    if response.status_code == 400:
        try:
            err = response.json().get("error", {})
            if err.get("code") == "CHAT_CONVERSATION_NOT_ONGOING":
                response = requests.post(
                    CHAT_URL,
                    headers={
                        "Authorization": f"Bearer {CHATBASE_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "message": message_text,
                        "stream": False,
                        "userId": user_id,
                    },
                    timeout=60,
                )
        except Exception:
            pass

    response.raise_for_status()
    body = response.json()

    # Ожидаемый формат non-streaming ответа:
    # body["data"]["parts"][0]["text"]
    data = body.get("data", {})
    parts = data.get("parts", [])

    text_chunks = []
    for part in parts:
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            text_chunks.append(part["text"])

    answer = "\n".join(text_chunks).strip()
    if answer:
        return answer

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

    user_id = f"tg_{chat_id}"

    if text == "/start":
        send_telegram_message(
            chat_id,
            "Здравствуйте! Я виртуальный помощник Александра Николаевича Ржаненкова. Опишите ваш вопрос, и я постараюсь подсказать решение."
        )
        return {"ok": True}

    if text == "/help":
        send_telegram_message(
            chat_id,
            "Команды:\n/start — начать\n/help — помощь\n\nПросто напишите ваш вопрос. Я постараюсь сохранять контекст текущего диалога."
        )
        return {"ok": True}

    try:
        answer = ask_chatbase(text, user_id)
        send_telegram_message(chat_id, answer)
    except Exception as e:
        send_telegram_message(
            chat_id,
            f"Ошибка при обращении к Chatbase: {str(e)}"
        )

    return {"ok": True}
