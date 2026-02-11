"""
Hot Chair Bot 🔥
Телеграм-бот для координации присутствия в офисе.
Гарантирует минимум 2 человека каждый будний день.

Фичи:
- Классические команды (/set, /week, /status, ...)
- Общение через LLM (ChatGPT) — можно просто писать боту текстом
- Бот подтверждает действия перед выполнением (inline-кнопки)
- Проактивные напоминания в групповые чаты
- Работает и в личке, и в группах
"""

import json
import os
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ─── Настройки ────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
DATA_FILE = Path(__file__).parent / "data.json"
TZ = ZoneInfo("Europe/Moscow")
MIN_PEOPLE = 2

DAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт"]
DAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница"]
DAYS_MAP = {
    "пн": 0, "понедельник": 0, "понедельника": 0,
    "вт": 1, "вторник": 1, "вторника": 1,
    "ср": 2, "среда": 2, "среду": 2,
    "чт": 3, "четверг": 3, "четверга": 3,
    "пт": 4, "пятница": 4, "пятницу": 4,
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ─── Хранилище ────────────────────────────────────────────────────────────────

def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"weeks": {}, "group_chats": [], "names": {}}


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def register_group_chat(chat_id: int):
    """Запоминаем групповой чат для проактивных сообщений."""
    data = load_data()
    if chat_id not in data["group_chats"]:
        data["group_chats"].append(chat_id)
        save_data(data)


# ─── Утилиты ──────────────────────────────────────────────────────────────────

def week_key(dt: datetime) -> str:
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def current_week_key() -> str:
    return week_key(datetime.now(TZ))


def next_week_key() -> str:
    return week_key(datetime.now(TZ) + timedelta(weeks=1))


def monday_of(wk: str) -> datetime:
    return datetime.strptime(wk, "%Y-%m-%d").replace(tzinfo=TZ)


def parse_days(args: list[str]) -> list[int] | None:
    days = []
    for a in args:
        key = a.lower().strip(",.")
        if key in DAYS_MAP:
            days.append(DAYS_MAP[key])
    return sorted(set(days)) if days else None


def get_display_name(user) -> str:
    if user.first_name and user.last_name:
        return f"{user.first_name} {user.last_name}"
    return user.first_name or user.username or str(user.id)


def format_week(data: dict, wk: str, label: str = "") -> str:
    week_data = data["weeks"].get(wk, {})
    mon = monday_of(wk)
    lines = []
    header = mon.strftime("%d.%m") + " — " + (mon + timedelta(days=4)).strftime("%d.%m.%Y")
    lines.append(f"📅 {label}{header}\n")

    for i, day_name in enumerate(DAYS_RU):
        date_str = (mon + timedelta(days=i)).strftime("%d.%m")
        people = []
        for uid, days_list in week_data.items():
            if i in days_list:
                people.append(data["names"].get(uid, f"id:{uid}"))
        count = len(people)
        marker = "🔴" if count < MIN_PEOPLE else "🟢"
        people_str = ", ".join(people) if people else "—"
        lines.append(f"{marker} {day_name} ({date_str}):  [{count}]  {people_str}")

    return "\n".join(lines)


def get_schedule_summary(data: dict) -> str:
    """Текстовая сводка для LLM контекста."""
    lines = []
    for wk_label, wk_key in [("Текущая неделя", current_week_key()),
                               ("Следующая неделя", next_week_key())]:
        week_data = data["weeks"].get(wk_key, {})
        mon = monday_of(wk_key)
        lines.append(f"\n{wk_label} ({mon.strftime('%d.%m.%Y')}):")
        for i, day_name in enumerate(DAYS_RU):
            people = []
            for uid, days_list in week_data.items():
                if i in days_list:
                    people.append(data["names"].get(uid, uid))
            count = len(people)
            status = "⚠️ НЕХВАТКА" if count < MIN_PEOPLE else "ОК"
            ppl = ", ".join(people) if people else "никто"
            lines.append(f"  {day_name}: {ppl} ({count} чел.) — {status}")
    return "\n".join(lines)


def problem_days_text(data: dict, wk: str) -> str:
    week_data = data["weeks"].get(wk, {})
    mon = monday_of(wk)
    problems = []
    for i, day_name in enumerate(DAYS_RU):
        count = sum(1 for days_list in week_data.values() if i in days_list)
        if count < MIN_PEOPLE:
            need = MIN_PEOPLE - count
            date_str = (mon + timedelta(days=i)).strftime("%d.%m")
            problems.append(f"  🔴 {day_name} ({date_str}) — нужно ещё {need} чел.")
    if not problems:
        return "✅ Все дни закрыты, минимум по 2 человека!"
    return "\n".join(problems)


def set_days_for_user(uid: str, name: str, days: list[int], wk: str) -> str:
    data = load_data()
    data["names"][uid] = name
    if wk not in data["weeks"]:
        data["weeks"][wk] = {}
    data["weeks"][wk][uid] = days
    save_data(data)
    day_names = ", ".join(DAYS_RU[d] for d in days)
    which = "эту неделю" if wk == current_week_key() else "следующую неделю"
    return f"✅ {name} будет в офисе на {which}: {day_names}"


def clear_days_for_user(uid: str, wk: str) -> str:
    data = load_data()
    which = "эту неделю" if wk == current_week_key() else "следующую неделю"
    if wk in data["weeks"] and uid in data["weeks"][wk]:
        del data["weeks"][wk][uid]
        save_data(data)
        return f"🗑 Записи на {which} убраны."
    return f"У тебя и так нет записей на {which}."


# ─── LLM ──────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — Hot Chair Bot 🔥, бот для координации присутствия в офисе.
Правило: каждый будний день в офисе должно быть минимум 2 человека.

Твоя задача — помогать команде договариваться кто когда приходит.
Ты общаешься неформально, с юмором, но по делу. Ты слегка дерзкий и саркастичный.
Можешь подкалывать тех кто редко ходит в офис. Ты — стул, и ты хочешь чтобы на тебе сидели.

ВАЖНО: если человек хочет записаться на дни или изменить расписание, ты ОБЯЗАН предложить конкретное действие в СТРОГО определённом формате. НЕ ВЫПОЛНЯЙ действие сразу — предложи, человек подтвердит кнопкой.

Формат действия (ОБЯЗАТЕЛЬНО в конце сообщения, на ОТДЕЛЬНОЙ строке):
ACTION:SET:день1,день2:this  — записать на текущую неделю
ACTION:SET:день1,день2:next  — записать на следующую неделю
ACTION:CLEAR:this             — убрать записи на текущую неделю
ACTION:CLEAR:next             — убрать записи на следующую неделю

Дни указывай ЦИФРАМИ: 0=Пн, 1=Вт, 2=Ср, 3=Чт, 4=Пт

Примеры:
- "Запиши меня на понедельник и среду" → ACTION:SET:0,2:this
- "Буду на следующей неделе во вторник" → ACTION:SET:1:next
- "Убери меня с этой недели" → ACTION:CLEAR:this
- "Поменяй среду на четверг" → узнай неделю, потом SET с новыми днями

Если человек просто болтает или спрашивает расписание — отвечай БЕЗ ACTION.
Если непонятно на какую неделю — спроси.
Одно ACTION на сообщение максимум.
Отвечай КРАТКО, 1-3 предложения."""


async def ask_llm(user_message: str, user_name: str, schedule_context: str) -> str:
    if not openai_client:
        return "🤖 LLM не подключен — задай OPENAI_API_KEY. Пока используй команды: /set, /week, /status"

    now = datetime.now(TZ)
    today_name = DAYS_FULL[now.weekday()] if now.weekday() < 5 else "выходной"
    context = f"""Сейчас: {today_name}, {now.strftime('%d.%m.%Y %H:%M')} МСК
Пользователь: {user_name}

Текущее расписание офиса:
{schedule_context}"""

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "system", "content": context},
                {"role": "user", "content": user_message},
            ],
            max_tokens=400,
            temperature=0.8,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return f"😵 Мозги закоротило: {e}\nПопробуй команды: /set, /week"


def parse_action(text: str) -> dict | None:
    match = re.search(r"ACTION:(SET|CLEAR):?([0-4,]*):?(this|next)?", text)
    if not match:
        return None
    action_type = match.group(1)
    days_str = match.group(2)
    week_target = match.group(3) or "this"
    wk = current_week_key() if week_target == "this" else next_week_key()

    if action_type == "SET" and days_str:
        days = sorted(set(int(d) for d in days_str.split(",") if d.isdigit() and 0 <= int(d) <= 4))
        if days:
            return {"type": "SET", "days": days, "week": wk, "week_label": week_target}
    if action_type == "CLEAR":
        return {"type": "CLEAR", "week": wk, "week_label": week_target}
    return None


def strip_action_line(text: str) -> str:
    return re.sub(r"\n?ACTION:(SET|CLEAR)[^\n]*", "", text).strip()


# ─── Обработчики команд ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        register_group_chat(update.effective_chat.id)
    text = (
        "🔥 Hot Chair Bot — координация офиса!\n\n"
        "Команды:\n"
        "  /set Пн Ср Пт — отметить дни (эта неделя)\n"
        "  /setnext Вт Чт — дни на след. неделю\n"
        "  /clear /clearnext — убрать свои дни\n"
        "  /week /next — расписание\n"
        "  /status — проблемные дни\n\n"
        "Или просто пиши текстом:\n"
        "  «Запиши меня на понедельник и среду»\n"
        "  «Кто завтра в офисе?»\n"
        "  «Поменяй мне пятницу на четверг»\n\n"
        "В группе — тегни меня или ответь на моё сообщение.\n"
        f"Цель: минимум {MIN_PEOPLE} чел. каждый будний день 💪"
    )
    await update.message.reply_text(text)


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_set(update, context, current_week_key(), "эту неделю")

async def cmd_setnext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_set(update, context, next_week_key(), "следующую неделю")

async def _do_set(update: Update, context: ContextTypes.DEFAULT_TYPE, wk: str, label: str):
    if update.effective_chat.type != "private":
        register_group_chat(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("Укажи дни: /set Пн Ср Пт")
        return
    days = parse_days(context.args)
    if days is None:
        await update.message.reply_text("Не понял дни. Используй: Пн, Вт, Ср, Чт, Пт")
        return
    uid = str(update.effective_user.id)
    name = get_display_name(update.effective_user)
    result = set_days_for_user(uid, name, days, wk)
    await update.message.reply_text(result)
    data = load_data()
    problems = problem_days_text(data, wk)
    if "🔴" in problems:
        await update.message.reply_text(f"⚠️ На {label}:\n{problems}")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    await update.message.reply_text(clear_days_for_user(uid, current_week_key()))

async def cmd_clearnext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    await update.message.reply_text(clear_days_for_user(uid, next_week_key()))

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        register_group_chat(update.effective_chat.id)
    data = load_data()
    await update.message.reply_text(format_week(data, current_week_key(), "Эта неделя: "))

async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    await update.message.reply_text(format_week(data, next_week_key(), "След. неделя: "))

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    text = (
        "📊 Эта неделя:\n" + problem_days_text(data, current_week_key()) + "\n\n"
        "📊 Следующая неделя:\n" + problem_days_text(data, next_week_key())
    )
    await update.message.reply_text(text)


# ─── LLM обработка текстовых сообщений ───────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_type = update.effective_chat.type
    text = update.message.text

    # В группе реагируем только на @mention или reply
    if chat_type != "private":
        register_group_chat(update.effective_chat.id)
        bot_username = context.bot.username or ""
        is_mention = f"@{bot_username}" in text
        is_reply = (
            update.message.reply_to_message
            and update.message.reply_to_message.from_user
            and update.message.reply_to_message.from_user.id == context.bot.id
        )
        if not is_mention and not is_reply:
            return
        text = text.replace(f"@{bot_username}", "").strip()

    if not text:
        return

    data = load_data()
    schedule = get_schedule_summary(data)
    user_name = get_display_name(update.effective_user)

    llm_response = await ask_llm(text, user_name, schedule)
    action = parse_action(llm_response)
    clean_text = strip_action_line(llm_response)

    if action:
        uid = str(update.effective_user.id)
        wl = "эту неделю" if action["week_label"] == "this" else "следующую неделю"

        if action["type"] == "SET":
            day_names = ", ".join(DAYS_RU[d] for d in action["days"])
            confirm_text = f"\n\n📝 Записать тебя на {wl}: {day_names}?"
            cb = f"set:{uid}:{','.join(str(d) for d in action['days'])}:{action['week']}"
        else:
            confirm_text = f"\n\n🗑 Убрать все записи на {wl}?"
            cb = f"clear:{uid}:{action['week']}"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, давай", callback_data=cb),
            InlineKeyboardButton("❌ Не, отмена", callback_data="cancel"),
        ]])
        await update.message.reply_text(clean_text + confirm_text, reply_markup=keyboard)
    else:
        if clean_text:
            await update.message.reply_text(clean_text)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cb = query.data
    user_id = str(query.from_user.id)
    user_name = get_display_name(query.from_user)

    if cb == "cancel":
        await query.edit_message_text(query.message.text.rsplit("\n\n", 1)[0] + "\n\n❌ Отменено.")
        return

    parts = cb.split(":")

    if parts[0] == "set":
        target_uid, days_str, wk = parts[1], parts[2], parts[3]
        if user_id != target_uid:
            await query.answer("Эта кнопка не для тебя 😏", show_alert=True)
            return
        days = [int(d) for d in days_str.split(",")]
        result = set_days_for_user(target_uid, user_name, days, wk)
        base_text = query.message.text.rsplit("\n\n", 1)[0]
        await query.edit_message_text(f"{base_text}\n\n{result}")

        data = load_data()
        problems = problem_days_text(data, wk)
        if "🔴" in problems:
            await query.message.reply_text(f"⚠️ Остались проблемные дни:\n{problems}")

    elif parts[0] == "clear":
        target_uid, wk = parts[1], parts[2]
        if user_id != target_uid:
            await query.answer("Эта кнопка не для тебя 😏", show_alert=True)
            return
        result = clear_days_for_user(target_uid, wk)
        base_text = query.message.text.rsplit("\n\n", 1)[0]
        await query.edit_message_text(f"{base_text}\n\n{result}")


# ─── Проактивные напоминания ──────────────────────────────────────────────────

async def morning_reminder(context: ContextTypes.DEFAULT_TYPE):
    """09:00 МСК по будням — кто сегодня?"""
    now = datetime.now(TZ)
    if now.weekday() > 4:
        return

    data = load_data()
    wk = current_week_key()
    week_data = data["weeks"].get(wk, {})
    today_idx = now.weekday()
    people = [data["names"].get(uid, uid) for uid, days in week_data.items() if today_idx in days]
    count = len(people)

    if count < MIN_PEOPLE:
        need = MIN_PEOPLE - count
        day_name = DAYS_RU[today_idx]
        text = (
            f"🚨 Сегодня {day_name} — в офисе записано {count} чел.\n"
            f"Нужно ещё {need}! Кто спасёт ситуацию?\n\n"
            f"/set {day_name} или просто напиши мне «буду сегодня» 🪑🔥"
        )
        for chat_id in data.get("group_chats", []):
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                logger.warning(f"Send to {chat_id} failed: {e}")


async def midweek_lookahead(context: ContextTypes.DEFAULT_TYPE):
    """Среда 12:00 — обзор до конца недели + след. неделя."""
    now = datetime.now(TZ)
    if now.weekday() != 2:
        return

    data = load_data()
    wk = current_week_key()
    week_data = data["weeks"].get(wk, {})
    problems_this = [DAYS_RU[i] for i in [3, 4]
                     if sum(1 for d in week_data.values() if i in d) < MIN_PEOPLE]

    nwk = next_week_key()
    nweek_data = data["weeks"].get(nwk, {})
    problems_next = [DAYS_RU[i] for i in range(5)
                     if sum(1 for d in nweek_data.values() if i in d) < MIN_PEOPLE]

    if not problems_this and not problems_next:
        return

    lines = ["📋 Среда — сверяемся!\n"]
    if problems_this:
        lines.append(f"⚠️ До конца недели пусто: {', '.join(problems_this)}")
    if problems_next:
        lines.append(f"⚠️ След. неделя: {', '.join(problems_next)}")
        lines.append("\n/setnext или напишите мне кто когда сможет 💬")

    text = "\n".join(lines)
    for chat_id in data.get("group_chats", []):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning(f"Send to {chat_id} failed: {e}")


async def friday_nag(context: ContextTypes.DEFAULT_TYPE):
    """Пятница 15:00 — заполните следующую неделю!"""
    now = datetime.now(TZ)
    if now.weekday() != 4:
        return

    data = load_data()
    nwk = next_week_key()
    nweek_data = data["weeks"].get(nwk, {})
    empty = [DAYS_RU[i] for i in range(5)
             if sum(1 for d in nweek_data.values() if i in d) < MIN_PEOPLE]

    if not empty:
        return

    text = (
        f"🔥 Пятница! Не забудьте про следующую неделю.\n\n"
        f"Не закрыты: {', '.join(empty)}\n\n"
        f"/setnext или напишите «на след неделе буду в ...»"
    )
    for chat_id in data.get("group_chats", []):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning(f"Send to {chat_id} failed: {e}")


async def cleanup_old_weeks(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    cutoff = (datetime.now(TZ) - timedelta(weeks=4)).strftime("%Y-%m-%d")
    old = [k for k in data["weeks"] if k < cutoff]
    for k in old:
        del data["weeks"][k]
    if old:
        save_data(data)
        logger.info(f"Cleaned {len(old)} old weeks")


# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("❌ Установи BOT_TOKEN!")
        print("   export BOT_TOKEN='123456:ABC-DEF...'")
        return

    if not OPENAI_API_KEY:
        print("⚠️  OPENAI_API_KEY не задан — LLM-общение отключено")
        print("   Команды будут работать\n")

    app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("setnext", cmd_setnext))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("clearnext", cmd_clearnext))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("status", cmd_status))

    # Кнопки подтверждения
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Текст → LLM
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Расписание напоминаний
    jq = app.job_queue
    t = lambda h, m=0: datetime.now(TZ).replace(hour=h, minute=m, second=0).timetz()

    jq.run_daily(morning_reminder, time=t(9))           # Будни 09:00
    jq.run_daily(midweek_lookahead, time=t(12), days=(2,))  # Среда 12:00
    jq.run_daily(friday_nag, time=t(15), days=(4,))     # Пятница 15:00
    jq.run_daily(cleanup_old_weeks, time=t(3), days=(0,))   # Пн 03:00 очистка

    logger.info("🔥 Hot Chair Bot запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
