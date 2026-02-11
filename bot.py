"""
Телеграм-бот для координации присутствия в офисе.
Гарантирует, что каждый будний день в офисе >= 2 человек.

Команды:
  /start          — приветствие и справка
  /week           — показать расписание на текущую неделю
  /next           — показать расписание на следующую неделю
  /set Пн Ср Пт   — отметить свои дни на этой неделе
  /setnext Вт Чт  — отметить свои дни на следующей неделе
  /clear          — убрать все свои дни на этой неделе
  /clearnext      — убрать все свои дни на следующей неделе
  /status         — проблемные дни (где < 2 человек)
  /remind         — вкл/выкл ежедневное утреннее напоминание (09:00 МСК)

Хранение: JSON-файл (data.json) рядом с ботом.
"""

import json
import os
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ─── Настройки ────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATA_FILE = Path(__file__).parent / "data.json"
TZ = ZoneInfo("Europe/Moscow")
MIN_PEOPLE = 2  # минимум человек в офисе

DAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт"]
DAYS_MAP = {
    "пн": 0, "понедельник": 0,
    "вт": 1, "вторник": 1,
    "ср": 2, "среда": 2,
    "чт": 3, "четверг": 3,
    "пт": 4, "пятница": 4,
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Хранилище ────────────────────────────────────────────────────────────────

def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"weeks": {}, "remind_chats": [], "names": {}}


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ─── Утилиты ──────────────────────────────────────────────────────────────────

def week_key(dt: datetime) -> str:
    """Ключ недели = дата понедельника, напр. '2026-02-09'."""
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def current_week_key() -> str:
    return week_key(datetime.now(TZ))


def next_week_key() -> str:
    return week_key(datetime.now(TZ) + timedelta(weeks=1))


def monday_of(wk: str) -> datetime:
    return datetime.strptime(wk, "%Y-%m-%d").replace(tzinfo=TZ)


def parse_days(args: list[str]) -> list[int] | None:
    """Парсит список дней из аргументов команды. Возвращает None при ошибке."""
    days = []
    for a in args:
        key = a.lower().strip(",. ")
        if key in DAYS_MAP:
            days.append(DAYS_MAP[key])
        else:
            return None
    return sorted(set(days))


def get_display_name(user) -> str:
    """Человекочитаемое имя пользователя."""
    if user.first_name and user.last_name:
        return f"{user.first_name} {user.last_name}"
    return user.first_name or user.username or str(user.id)


def format_week(data: dict, wk: str) -> str:
    """Красивое расписание недели."""
    week_data = data["weeks"].get(wk, {})
    mon = monday_of(wk)
    lines = []
    header_date = mon.strftime("%d.%m") + " — " + (mon + timedelta(days=4)).strftime("%d.%m.%Y")
    lines.append(f"📅 Неделя {header_date}\n")

    any_problem = False
    for i, day_name in enumerate(DAYS_RU):
        date_str = (mon + timedelta(days=i)).strftime("%d.%m")
        # Собираем людей на этот день
        people = []
        for uid, days_list in week_data.items():
            if i in days_list:
                name = data["names"].get(uid, f"id:{uid}")
                people.append(name)

        count = len(people)
        if count < MIN_PEOPLE:
            marker = "🔴"
            any_problem = True
        else:
            marker = "🟢"

        people_str = ", ".join(people) if people else "—"
        lines.append(f"{marker} {day_name} ({date_str}):  [{count}]  {people_str}")

    if any_problem:
        lines.append(f"\n⚠️ Нужно минимум {MIN_PEOPLE} чел. на каждый день!")

    return "\n".join(lines)


def problem_days_text(data: dict, wk: str) -> str:
    """Текст про проблемные дни."""
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
    return "Проблемные дни на неделе:\n" + "\n".join(problems)


# ─── Обработчики команд ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привет! Я помогу организовать присутствие в офисе.\n\n"
        "Команды:\n"
        "  /set Пн Ср Пт — отметить дни на этой неделе\n"
        "  /setnext Вт Чт — отметить дни на следующей неделе\n"
        "  /clear — убрать свои дни (этой недели)\n"
        "  /clearnext — убрать свои дни (след. недели)\n"
        "  /week — расписание этой недели\n"
        "  /next — расписание следующей недели\n"
        "  /status — показать проблемные дни\n"
        "  /remind — вкл/выкл утреннее напоминание (09:00 МСК)\n\n"
        f"Цель: минимум {MIN_PEOPLE} человека в офисе каждый будний день 💪"
    )
    await update.message.reply_text(text)


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_set(update, context, current_week_key(), "эту неделю")


async def cmd_setnext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_set(update, context, next_week_key(), "следующую неделю")


async def _do_set(update: Update, context: ContextTypes.DEFAULT_TYPE, wk: str, label: str):
    if not context.args:
        await update.message.reply_text(
            "Укажи дни через пробел, например:\n/set Пн Ср Пт"
        )
        return

    days = parse_days(context.args)
    if days is None:
        await update.message.reply_text(
            "Не понял дни. Используй: Пн, Вт, Ср, Чт, Пт\n"
            "Пример: /set Пн Ср Пт"
        )
        return

    data = load_data()
    uid = str(update.effective_user.id)
    data["names"][uid] = get_display_name(update.effective_user)

    if wk not in data["weeks"]:
        data["weeks"][wk] = {}
    data["weeks"][wk][uid] = days
    save_data(data)

    day_names = ", ".join(DAYS_RU[d] for d in days)
    name = data["names"][uid]
    await update.message.reply_text(
        f"✅ {name} будет в офисе на {label}: {day_names}"
    )

    # Автоматически показываем проблемные дни
    problems = problem_days_text(data, wk)
    if "🔴" in problems:
        await update.message.reply_text(problems)


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_clear(update, current_week_key(), "эту неделю")


async def cmd_clearnext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_clear(update, next_week_key(), "следующую неделю")


async def _do_clear(update: Update, wk: str, label: str):
    data = load_data()
    uid = str(update.effective_user.id)
    if wk in data["weeks"] and uid in data["weeks"][wk]:
        del data["weeks"][wk][uid]
        save_data(data)
        await update.message.reply_text(f"🗑 Записи на {label} убраны.")
    else:
        await update.message.reply_text(f"У тебя и так нет записей на {label}.")


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    await update.message.reply_text(format_week(data, current_week_key()))


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    await update.message.reply_text(format_week(data, next_week_key()))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    wk = current_week_key()
    nwk = next_week_key()
    text = (
        "📊 Эта неделя:\n" + problem_days_text(data, wk) + "\n\n"
        "📊 Следующая неделя:\n" + problem_days_text(data, nwk)
    )
    await update.message.reply_text(text)


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    chat_id = update.effective_chat.id
    if chat_id in data["remind_chats"]:
        data["remind_chats"].remove(chat_id)
        save_data(data)
        await update.message.reply_text("🔕 Утреннее напоминание выключено.")
    else:
        data["remind_chats"].append(chat_id)
        save_data(data)
        await update.message.reply_text(
            "🔔 Утреннее напоминание включено!\n"
            "Каждый будний день в 09:00 МСК буду напоминать, "
            "если на день не хватает людей."
        )


# ─── Утреннее напоминание (job) ──────────────────────────────────────────────

async def morning_reminder(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ)
    # Только будни (0=Пн ... 4=Пт)
    if now.weekday() > 4:
        return

    data = load_data()
    wk = current_week_key()
    week_data = data["weeks"].get(wk, {})

    today_idx = now.weekday()
    count = sum(1 for days_list in week_data.values() if today_idx in days_list)

    if count < MIN_PEOPLE:
        need = MIN_PEOPLE - count
        day_name = DAYS_RU[today_idx]
        text = (
            f"🚨 Сегодня {day_name} — в офисе отмечено только {count} чел.\n"
            f"Нужно ещё {need}! Кто готов прийти?\n\n"
            f"Отметься командой: /set {day_name}"
        )
        for chat_id in data.get("remind_chats", []):
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                logger.warning(f"Не удалось отправить напоминание в {chat_id}: {e}")


# ─── Очистка старых данных (раз в неделю) ────────────────────────────────────

async def cleanup_old_weeks(context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    cutoff = (datetime.now(TZ) - timedelta(weeks=4)).strftime("%Y-%m-%d")
    old_keys = [k for k in data["weeks"] if k < cutoff]
    for k in old_keys:
        del data["weeks"][k]
    if old_keys:
        save_data(data)
        logger.info(f"Очищено старых недель: {len(old_keys)}")


# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Установи токен бота!")
        print("   export BOT_TOKEN='123456:ABC-DEF...'")
        print("   python bot.py")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("setnext", cmd_setnext))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("clearnext", cmd_clearnext))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("remind", cmd_remind))

    # Утреннее напоминание — каждый день в 09:00 МСК
    job_queue = app.job_queue
    reminder_time = datetime.now(TZ).replace(hour=9, minute=0, second=0)
    job_queue.run_daily(
        morning_reminder,
        time=reminder_time.timetz(),
    )

    # Очистка старых недель — раз в неделю (понедельник 03:00)
    cleanup_time = datetime.now(TZ).replace(hour=3, minute=0, second=0)
    job_queue.run_daily(
        cleanup_old_weeks,
        time=cleanup_time.timetz(),
        days=(0,),  # только понедельник
    )

    logger.info("🤖 Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
