import os
import json
import requests

from fastapi import FastAPI, Request
from upstash_redis import Redis

app = FastAPI()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHATBASE_API_KEY = os.getenv("CHATBASE_API_KEY")
CHATBASE_CHATBOT_ID = os.getenv("CHATBASE_CHATBOT_ID")

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

if not TELEGRAM_TOKEN or not CHATBASE_API_KEY or not CHATBASE_CHATBOT_ID:
    raise RuntimeError(
        "Missing required environment variables: "
        "TELEGRAM_TOKEN, CHATBASE_API_KEY, CHATBASE_CHATBOT_ID"
    )

if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
    raise RuntimeError(
        "Missing required Upstash environment variables: "
        "UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN"
    )

redis = Redis(
    url=UPSTASH_REDIS_REST_URL,
    token=UPSTASH_REDIS_REST_TOKEN,
)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
CHATBASE_API = "https://www.chatbase.co/api/v1/chat"

MAX_CONTEXT_MESSAGES = 12
HISTORY_TTL_SECONDS = 30 * 24 * 60 * 60


def get_history_key(chat_id: int) -> str:
    return f"tg:history:{chat_id}"


def get_conversation_key(chat_id: int) -> str:
    return f"tg:conversation:{chat_id}"


def send_telegram_message(chat_id: int, text: str) -> None:
    response = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=30,
    )
    response.raise_for_status()


def send_telegram_chat_action(chat_id: int, action: str = "typing") -> None:
    requests.post(
        f"{TELEGRAM_API}/sendChatAction",
        json={"chat_id": chat_id, "action": action},
        timeout=15,
    )


def get_history(chat_id: int, limit: int = MAX_CONTEXT_MESSAGES) -> list:
    key = get_history_key(chat_id)
    items = redis.lrange(key, -limit, -1) or []

    print("DEBUG get_history raw items:", items)

    history = []
    for item in items:
        if isinstance(item, bytes):
            item = item.decode("utf-8")

        # Иногда клиент Upstash уже возвращает dict
        if isinstance(item, dict):
            parsed = item
        elif isinstance(item, str):
            try:
                parsed = json.loads(item)
            except Exception as e:
                print("DEBUG json.loads failed:", e, " item=", item)
                continue
        else:
            print("DEBUG unknown item type:", type(item), item)
            continue

        if (
            isinstance(parsed, dict)
            and parsed.get("role") in {"user", "assistant"}
            and parsed.get("content")
        ):
            history.append(
                {
                    "role": parsed["role"],
                    "content": parsed["content"],
                }
            )

    print("DEBUG parsed history:", history)
    return history


def append_message(chat_id: int, role: str, content: str) -> None:
    key = get_history_key(chat_id)

    value = json.dumps(
        {
            "role": role,
            "content": content,
        },
        ensure_ascii=False,
    )

    print("DEBUG append_message key:", key)
    print("DEBUG append_message value:", value)

    redis.rpush(key, value)
    redis.ltrim(key, -40, -1)
    redis.expire(key, HISTORY_TTL_SECONDS)

    print("DEBUG history after append:", redis.lrange(key, 0, -1))


def clear_history(chat_id: int) -> None:
    redis.delete(get_history_key(chat_id))
    redis.delete(get_conversation_key(chat_id))


def get_or_create_conversation_id(chat_id: int) -> str:
    key = get_conversation_key(chat_id)
    conversation_id = redis.get(key)

    if isinstance(conversation_id, bytes):
        conversation_id = conversation_id.decode("utf-8")

    if not conversation_id:
        conversation_id = f"tg_{chat_id}"
        redis.set(key, conversation_id, ex=HISTORY_TTL_SECONDS)

    print("DEBUG conversation_id:", conversation_id)
    return conversation_id


def ask_chatbase(chat_id: int, message_text: str) -> str:
    conversation_id = get_or_create_conversation_id(chat_id)
    contact_id = f"tg_{chat_id}"

    previous_messages = get_history(chat_id, limit=MAX_CONTEXT_MESSAGES)

    messages = previous_messages + [
        {
            "role": "user",
            "content": message_text,
        }
    ]

    print("DEBUG messages to Chatbase:", messages)

    payload = {
        "chatbotId": CHATBASE_CHATBOT_ID,
        "messages": messages,
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
        timeout=90,
    )

    print("DEBUG Chatbase status:", response.status_code)
    print("DEBUG Chatbase response text:", response.text)

    response.raise_for_status()
    data = response.json()

    answer = data.get("text")
    if isinstance(answer, str) and answer.strip():
        return answer

    if isinstance(data.get("message"), str) and data["message"].strip():
        return data["message"]

    return "Извините, я не смог сформировать ответ."


@app.get("/")
def root():
    return {"ok": True, "message": "Telegram + Chatbase bot is running", "debug_version": "v2-debug"}


@app.post("/webhook")
async def webhook(request: Request):
    update = await request.json()
    print("DEBUG incoming update:", update)

    message = update.get("message")
    if not message:
        return {"ok": True}

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text")

    print("DEBUG chat_id:", chat_id)
    print("DEBUG text:", text)

    if not chat_id or not text:
        return {"ok": True}

    text = text.strip()

    if text == "/start":
        send_telegram_message(
            chat_id,
            "Привет! Контекст должен сохраняться.\n/reset — очистить историю\nDEBUG VERSION: v2-debug"
        )
        return {"ok": True}

    if text == "/help":
        send_telegram_message(
            chat_id,
            "Команды:\n/start\n/help\n/reset\nDEBUG VERSION: v2-debug"
        )
        return {"ok": True}

    if text == "/reset":
        clear_history(chat_id)
        send_telegram_message(chat_id, "История диалога очищена.")
        return {"ok": True}

    try:
        send_telegram_chat_action(chat_id, "typing")

        answer = ask_chatbase(chat_id, text)

        append_message(chat_id, "user", text)
        append_message(chat_id, "assistant", answer)

        send_telegram_message(chat_id, answer)

    except Exception as e:
        print("DEBUG ERROR:", repr(e))
        send_telegram_message(
            chat_id,
            "Произошла ошибка при обращении к AI. Проверь Vercel logs."
        )

    return {"ok": True}
