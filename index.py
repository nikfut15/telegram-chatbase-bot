import json
import os
import re
from typing import Dict, List

import requests
from fastapi import FastAPI, Request

app = FastAPI()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHATBASE_API_KEY = os.getenv("CHATBASE_API_KEY")
CHATBASE_CHATBOT_ID = os.getenv("CHATBASE_CHATBOT_ID")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

REQUIRED_VARS = [
    "TELEGRAM_TOKEN",
    "CHATBASE_API_KEY",
    "CHATBASE_CHATBOT_ID",
    "UPSTASH_REDIS_REST_URL",
    "UPSTASH_REDIS_REST_TOKEN",
]
missing = [name for name in REQUIRED_VARS if not os.getenv(name)]
if missing:
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
CHATBASE_API = "https://www.chatbase.co/api/v1/chat"

# ===== Настройки памяти =====

# Сколько последних сообщений храним как "живое окно"
RECENT_KEEP_MESSAGES = 8

# Максимум символов, которые отправляем в Chatbase за один запрос
# Для твоих длинных ответов это безопаснее, чем просто увеличивать число сообщений.
MAX_CONTEXT_CHARS = 24000

# Максимальный размер сохраняемой summary
MAX_SUMMARY_CHARS = 4000

# Сколько summary реально отправляем в модель
SUMMARY_RENDER_CHARS = 1800

# Максимум символов на одну строку при сжатии старых сообщений
SUMMARY_LINE_CLIP = 220

START_MESSAGE = (
    "Здравствуйте! Я «Электроник» — виртуальный помощник депутата "
    "Ржаненкова Александра Николаевича. Опишите ваш вопрос, и я постараюсь помочь."
)

HELP_MESSAGE = (
    "Команды:\n"
    "/start — приветствие\n"
    "/help — помощь\n"
    "/new — начать диалог заново\n\n"
    "Просто напишите ваш вопрос. Контекст текущей переписки сохраняется."
)


# ===== Telegram =====

def tg_send(chat_id: int, text: str) -> None:
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]
    for chunk in chunks:
        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": chunk},
            timeout=30,
        )
        response.raise_for_status()


# ===== Upstash Redis =====

def redis_command(*args):
    response = requests.post(
        UPSTASH_REDIS_REST_URL,
        headers={
            "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
            "Content-Type": "application/json",
        },
        json=list(args),
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(str(body["error"]))
    return body.get("result")


def history_key(user_id: str) -> str:
    return f"history:{user_id}"


def summary_key(user_id: str) -> str:
    return f"summary:{user_id}"


def facts_key(user_id: str) -> str:
    return f"facts:{user_id}"


def load_history(user_id: str) -> List[Dict[str, str]]:
    raw = redis_command("GET", history_key(user_id))
    if not raw:
        return []

    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            return []

        cleaned = []
        for item in data:
            if (
                isinstance(item, dict)
                and item.get("role") in {"user", "assistant"}
                and isinstance(item.get("content"), str)
            ):
                cleaned.append({
                    "role": item["role"],
                    "content": item["content"],
                })
        return cleaned
    except Exception:
        return []


def save_history(user_id: str, messages: List[Dict[str, str]]) -> None:
    redis_command("SET", history_key(user_id), json.dumps(messages, ensure_ascii=False))


def load_summary(user_id: str) -> str:
    raw = redis_command("GET", summary_key(user_id))
    return raw if isinstance(raw, str) else ""


def save_summary(user_id: str, summary: str) -> None:
    summary = summary.strip()
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[-MAX_SUMMARY_CHARS:]
    redis_command("SET", summary_key(user_id), summary)


def load_facts(user_id: str) -> Dict[str, str]:
    raw = redis_command("GET", facts_key(user_id))
    if not raw:
        return {}

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            cleaned = {}
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str):
                    cleaned[k] = v
            return cleaned
    except Exception:
        pass

    return {}


def save_facts(user_id: str, facts: Dict[str, str]) -> None:
    redis_command("SET", facts_key(user_id), json.dumps(facts, ensure_ascii=False))


def clear_memory(user_id: str) -> None:
    redis_command("DEL", history_key(user_id))
    redis_command("DEL", summary_key(user_id))
    redis_command("DEL", facts_key(user_id))


# ===== Вспомогательные функции =====

def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clip_text(text: str, limit: int) -> str:
    text = normalize_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def estimate_chars(messages: List[Dict[str, str]]) -> int:
    return sum(len(m.get("content", "")) for m in messages)


# ===== Извлечение фактов пользователя =====

def update_facts_from_user_text(text: str, facts: Dict[str, str]) -> Dict[str, str]:
    updated = dict(facts)
    clean = normalize_text(text)

    patterns = [
        ("name", r"(?:меня зовут|можно называть|обращайтесь ко мне|зовут)\s+([А-ЯЁA-Z][а-яёa-z\-]{1,30})"),
        ("city", r"(?:я из|живу в|нахожусь в)\s+(Санкт-Петербурге|СПб|Петербурге|Москве|Москве|Ленинградской области|Московской области)"),
        ("district", r"(?:мой район|живу в)\s+([А-ЯЁA-Z][а-яёa-z\-]+(?:ский|цкий|ный)?\s+район)"),
    ]

    for key, pattern in patterns:
        match = re.search(pattern, clean, flags=re.IGNORECASE)
        if match:
            value = match.group(1).strip()
            updated[key] = value

    return updated


def render_facts_block(facts: Dict[str, str]) -> str:
    if not facts:
        return ""

    ordered_keys = ["name", "city", "district"]
    labels = {
        "name": "Имя",
        "city": "Город/регион",
        "district": "Район",
    }

    lines = []
    for key in ordered_keys:
        if key in facts:
            lines.append(f"- {labels[key]}: {facts[key]}")

    for key, value in facts.items():
        if key not in ordered_keys:
            lines.append(f"- {key}: {value}")

    if not lines:
        return ""

    return "Служебная память о пользователе:\n" + "\n".join(lines)


# ===== Сжатие старой истории =====

def compress_messages_to_lines(messages: List[Dict[str, str]]) -> List[str]:
    lines = []

    for msg in messages:
        role = msg.get("role")
        content = clip_text(msg.get("content", ""), SUMMARY_LINE_CLIP)
        if not content:
            continue

        if role == "user":
            prefix = "[Пользователь]"
        else:
            prefix = "[Ассистент]"

        lines.append(f"{prefix} {content}")

    return lines


def merge_summary(existing_summary: str, old_messages: List[Dict[str, str]]) -> str:
    new_lines = compress_messages_to_lines(old_messages)
    if not new_lines:
        return existing_summary.strip()

    parts = []
    if existing_summary.strip():
        parts.append(existing_summary.strip())

    parts.append("\n".join(new_lines))
    merged = "\n".join(parts).strip()

    if len(merged) > MAX_SUMMARY_CHARS:
        merged = merged[-MAX_SUMMARY_CHARS:]

    return merged


def compact_history(history: List[Dict[str, str]], summary: str) -> tuple[List[Dict[str, str]], str]:
    if len(history) <= RECENT_KEEP_MESSAGES:
        return history, summary

    old_part = history[:-RECENT_KEEP_MESSAGES]
    recent_part = history[-RECENT_KEEP_MESSAGES:]
    new_summary = merge_summary(summary, old_part)

    return recent_part, new_summary


# ===== Сборка контекста для Chatbase =====

def build_context_window(
    facts: Dict[str, str],
    summary: str,
    recent_history: List[Dict[str, str]],
    new_user_text: str,
) -> List[Dict[str, str]]:
    memory_messages: List[Dict[str, str]] = []

    facts_block = render_facts_block(facts)
    if facts_block:
        memory_messages.append({
            "role": "assistant",
            "content": facts_block,
        })

    if summary.strip():
        memory_messages.append({
            "role": "assistant",
            "content": "Краткая сжатая сводка предыдущей переписки:\n" + summary[-SUMMARY_RENDER_CHARS:],
        })

    tail = recent_history[:]
    current_user_message = {"role": "user", "content": new_user_text}

    result = memory_messages + tail + [current_user_message]

    while estimate_chars(result) > MAX_CONTEXT_CHARS and tail:
        tail = tail[1:]
        result = memory_messages + tail + [current_user_message]

    # Если даже после урезания хвоста слишком много — урезаем summary
    while estimate_chars(result) > MAX_CONTEXT_CHARS and len(memory_messages) > 1:
        summary_msg = memory_messages[-1]["content"]
        if len(summary_msg) <= 500:
            break
        memory_messages[-1]["content"] = summary_msg[-max(500, len(summary_msg) - 400):]
        result = memory_messages + tail + [current_user_message]

    # Крайний случай: если всё ещё не влезает, урезаем текущее пользовательское сообщение
    if estimate_chars(result) > MAX_CONTEXT_CHARS:
        allowed = max(1000, MAX_CONTEXT_CHARS - estimate_chars(memory_messages + tail) - 100)
        current_user_message["content"] = clip_text(new_user_text, allowed)
        result = memory_messages + tail + [current_user_message]

    return result


# ===== Chatbase =====

def ask_chatbase(messages: List[Dict[str, str]], contact_id: str) -> str:
    payload = {
        "chatbotId": CHATBASE_CHATBOT_ID,
        "messages": messages,
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

    if isinstance(data, dict):
        if isinstance(data.get("text"), str) and data["text"].strip():
            return data["text"].strip()
        if isinstance(data.get("message"), str) and data["message"].strip():
            return data["message"].strip()

    return "Извините, я не смог сформировать ответ."


@app.get("/")
def root():
    return {
        "ok": True,
        "message": "Telegram + Chatbase bot is running with recent window + summary + facts"
    }


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
    text = text.strip()

    if text == "/start":
        tg_send(chat_id, START_MESSAGE)
        return {"ok": True}

    if text == "/help":
        tg_send(chat_id, HELP_MESSAGE)
        return {"ok": True}

    if text == "/new":
        clear_memory(user_id)
        tg_send(chat_id, "Контекст диалога очищен. Можете начать новую тему.")
        return {"ok": True}

    try:
        history = load_history(user_id)
        summary = load_summary(user_id)
        facts = load_facts(user_id)

        # Обновляем факты из нового сообщения пользователя
        facts = update_facts_from_user_text(text, facts)

        # Собираем контекст
        context_messages = build_context_window(
            facts=facts,
            summary=summary,
            recent_history=history,
            new_user_text=text,
        )

        # Запрос в Chatbase
        answer = ask_chatbase(context_messages, contact_id=user_id)

        # Обновляем "живую" историю
        updated_history = history + [
            {"role": "user", "content": text},
            {"role": "assistant", "content": answer},
        ]

        # Сжимаем старый хвост
        compacted_history, new_summary = compact_history(updated_history, summary)

        # Сохраняем
        save_history(user_id, compacted_history)
        save_summary(user_id, new_summary)
        save_facts(user_id, facts)

        # Отправляем ответ
        tg_send(chat_id, answer)

    except Exception as e:
        tg_send(
            chat_id,
            "Произошла ошибка при обращении к AI. Проверьте переменные окружения, "
            f"Chatbase и Upstash. Техническая деталь: {str(e)}"
        )

    return {"ok": True}
