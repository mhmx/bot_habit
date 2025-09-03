import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import calendar
import datetime
import csv
import os

TOKEN = "8461591249:AAFNnI20VQaXjRhTw8S5lxdLxR21OWRWBbI"
bot = telebot.TeleBot(TOKEN)

HABITS_FILE = "habits.csv"
STATS_FILE = "stats.csv"

START_DATE = datetime.date(2025, 9, 3)

user_states = {}  # хранение состояния пользователей (например, ожидание названия привычки)


# -------------------- работа с CSV --------------------
def load_habits():
    habits = {}
    if not os.path.exists(HABITS_FILE):
        return habits
    with open(HABITS_FILE, newline='', encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            habits[row["id"]] = row["name"]
    return habits


def save_habits(habits):
    with open(HABITS_FILE, "w", newline='', encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "name"])
        writer.writeheader()
        for h_id, name in habits.items():
            writer.writerow({"id": h_id, "name": name})


def load_stats():
    data = {}
    if not os.path.exists(STATS_FILE):
        return data
    with open(STATS_FILE, newline='', encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date = row["date"]
            habit_id = row["habit_id"]
            status = row["status"] == "1"
            if date not in data:
                data[date] = {}
            data[date][habit_id] = status
    return data


def save_stats(data):
    with open(STATS_FILE, "w", newline='', encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "habit_id", "status"])
        writer.writeheader()
        for date, habits in data.items():
            for habit_id, status in habits.items():
                writer.writerow({
                    "date": date,
                    "habit_id": habit_id,
                    "status": "1" if status else "0"
                })


# -------------------- streaks --------------------
def calc_streaks(data, habits):
    results = {}
    today = datetime.date.today()

    for h_id, habit in habits.items():
        best = 0
        current = 0
        broken_best = 0
        streak = 0
        last_date = None

        for d in sorted(data.keys()):
            date = datetime.datetime.strptime(d, "%Y%m%d").date()
            if date < START_DATE or date > today:
                continue

            if h_id in data[d] and data[d][h_id]:
                if last_date is None or (date - last_date).days == 1:
                    streak += 1
                else:
                    if streak > best:
                        best = streak
                    streak = 1
                last_date = date
            else:
                if streak > best:
                    best = streak
                if streak > broken_best:
                    broken_best = streak
                streak = 0
                last_date = None

        if streak > best:
            best = streak

        results[habit] = (best, streak, broken_best)

    return results


# -------------------- статус для дня --------------------
def is_week_gold(date, data, habits):
    # проверяем всю неделю: если все привычки выполнены каждый день
    monday = date - datetime.timedelta(days=date.weekday())
    sunday = monday + datetime.timedelta(days=6)

    for d in (monday + datetime.timedelta(days=i) for i in range(7)):
        d_str = d.strftime("%Y%m%d")
        if d_str not in data:
            return False
        for h_id in habits.keys():
            if not data[d_str].get(h_id, False):
                return False
    return True


def day_status_emoji(date_str, data, habits):
    today = datetime.date.today()
    date = datetime.datetime.strptime(date_str, "%Y%m%d").date()

    if date < START_DATE:
        return ""

    if is_week_gold(date, data, habits):
        return " 🟡✨"

    if date_str not in data:
        if date < today:
            return " 🔴"
        return ""
    habits_data = data[date_str]
    done = sum(1 for h in habits.keys() if habits_data.get(h, False))
    if done == 0:
        if date < today:
            return " 🔴"
        else:
            return ""
    elif done == len(habits):
        return " 🟢"
    else:
        return " 🟡"


# -------------------- клавиатуры --------------------
def build_calendar(year, month, data, habits):
    kb = InlineKeyboardMarkup(row_width=7)
    cal = calendar.Calendar(firstweekday=0)
    month_days = cal.monthdayscalendar(year, month)

    kb.add(InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="none"))

    days_row = [InlineKeyboardButton(d, callback_data="none") for d in ["Mo","Tu","We","Th","Fr","Sa","Su"]]
    kb.row(*days_row)

    for week in month_days:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="none"))
            else:
                date_str = f"{year}{month:02d}{day:02d}"
                emoji = day_status_emoji(date_str, data, habits)
                cb = f"day_{date_str}"
                row.append(InlineKeyboardButton(f"{day}{emoji}", callback_data=cb))
        kb.row(*row)

    prev_month = (datetime.date(year, month, 1) - datetime.timedelta(days=1)).replace(day=1)
    next_month = (datetime.date(year, month, 28) + datetime.timedelta(days=4)).replace(day=1)
    kb.row(
        InlineKeyboardButton("⬅", callback_data=f"month_{prev_month.year}{prev_month.month:02d}"),
        InlineKeyboardButton("➕", callback_data="add_habit"),
        InlineKeyboardButton("➡", callback_data=f"month_{next_month.year}{next_month.month:02d}")
    )

    return kb


def build_day_menu(date_str, data, habits):
    kb = InlineKeyboardMarkup(row_width=1)
    habits_data = data.get(date_str, {})
    for h_id, name in habits.items():
        status = "✅" if habits_data.get(h_id, False) else "❌"
        cb = f"toggle_{date_str}_{h_id}"
        kb.add(InlineKeyboardButton(f"{status} {name}", callback_data=cb))
    kb.add(InlineKeyboardButton("⬅ Назад к календарю", callback_data=f"back_{date_str[:6]}"))
    return kb


# -------------------- тексты --------------------
def build_main_text(data, habits):
    streaks = calc_streaks(data, habits)
    lines = ["Лучшие результаты:\n"]
    for habit, (best, current, broken) in streaks.items():
        if broken and broken != best:
            lines.append(f"{habit} — {best} дней (был {broken})")
        else:
            lines.append(f"{habit} — {best} дней")
    lines.append("\nВыбери день:")
    return "\n".join(lines)


# -------------------- хендлеры --------------------
@bot.message_handler(commands=["start"])
def send_welcome(message):
    now = datetime.date.today()
    data = load_stats()
    habits = load_habits()
    kb = build_calendar(now.year, now.month, data, habits)
    text = build_main_text(data, habits)
    bot.send_message(message.chat.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    data = load_stats()
    habits = load_habits()

    if call.data == "add_habit":
        bot.send_message(call.message.chat.id, "Отправь название новой привычки:")
        user_states[call.from_user.id] = "waiting_habit"
        return

    if call.data.startswith("day_"):
        date_str = call.data.split("_")[1]
        text = f"Статус привычек за {date_str[6:]}.{date_str[4:6]}.{date_str[:4]}"
        kb = build_day_menu(date_str, data, habits)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif call.data.startswith("toggle_"):
        _, date_str, habit_id = call.data.split("_", 2)
        if date_str not in data:
            data[date_str] = {}
        data[date_str][habit_id] = not data[date_str].get(habit_id, False)
        save_stats(data)
        text = f"Статус привычек за {date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        kb = build_day_menu(date_str, data, habits)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif call.data.startswith("back_"):
        yyyymm = call.data.split("_")[1]
        year, month = int(yyyymm[:4]), int(yyyymm[4:])
        kb = build_calendar(year, month, data, habits)
        text = build_main_text(data, habits)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif call.data.startswith("month_"):
        yyyymm = call.data.split("_")[1]
        year, month = int(yyyymm[:4]), int(yyyymm[4:])
        kb = build_calendar(year, month, data, habits)
        text = build_main_text(data, habits)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)


@bot.message_handler(func=lambda message: True)
def handle_text(message):
    if user_states.get(message.from_user.id) == "waiting_habit":
        habits = load_habits()
        new_id = str(len(habits) + 1)
        habits[new_id] = message.text.strip()
        save_habits(habits)
        user_states.pop(message.from_user.id)

        data = load_stats()
        now = datetime.date.today()
        kb = build_calendar(now.year, now.month, data, habits)
        text = build_main_text(data, habits)
        bot.send_message(message.chat.id, f"Привычка «{message.text}» добавлена!", reply_markup=kb)


bot.polling(none_stop=True)
