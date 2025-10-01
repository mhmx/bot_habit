"""
Телеграм-бот для трекинга привычек с локальным кэшем и синхронизацией в PostgreSQL.

Идея:
- Данные (привычки и статусы по датам) держим в памяти для быстрого UI.
- Периодически синхронизируем кэш в БД (полная выгрузка снапшота).
- Есть команды для принудительной выгрузки / перезагрузки.

База данных: PostgreSQL
- Таблица habits: перечень привычек
- Таблица stats: ежедневные статусы; уникальность по (date, habit_id)
"""

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import calendar
import datetime
import psycopg2
import threading
import time
from typing import Dict, Any
from config import TOKEN, DB_CONFIG
bot = telebot.TeleBot(TOKEN)

# Нижняя граница дат, которые учитываются в интерфейсе и при подсчёте серий
START_DATE = datetime.date(2025, 9, 27)

# Кэш в памяти
class DataCache:
    """Потокобезопасный кэш состояния бота.

    Назначение:
    - сократить обращения к БД для операций UI
    - хранить словари привычек и дневных статусов
    - периодически синхронизировать всё в БД (см. _background_sync)
    """
    def __init__(self):
        self.habits: Dict[str, str] = {}
        self.stats: Dict[str, Dict[str, bool]] = {}
        self.last_sync = 0
        self.sync_interval = 3600  # интервал фоновой синхронизации, сек (1 час)
        self.lock = threading.Lock()
        
        # Запускаем фоновую синхронизацию
        self.sync_thread = threading.Thread(target=self._background_sync, daemon=True)
        self.sync_thread.start()
    
    def _background_sync(self):
        """Периодическая проверка: пришло ли время выгрузить кэш в БД.

        Цикл «просыпается» раз в минуту, чтобы не крутиться постоянно,
        и при достижении self.sync_interval вызывает _sync_to_db().
        """
        while True:
            time.sleep(60)  # Проверяем каждую минуту
            if time.time() - self.last_sync >= self.sync_interval:
                self._sync_to_db()
    
    def _sync_to_db(self):
        """Полная синхронизация кэша в БД (снимок текущего состояния).

        Текущая стратегия простая: TRUNCATE-подобная очистка (DELETE) и полная
        вставка из кэша. Это надёжно и прозрачно. Для больших объёмов данных
        можно перейти на UPSERT (ON CONFLICT DO UPDATE) и частичные изменения.
        """
        try:
            with psycopg2.connect(**DB_CONFIG) as conn:
                cursor = conn.cursor()
                
                # Синхронизация привычек
                cursor.execute("DELETE FROM habits")
                for habit_id, name in self.habits.items():
                    cursor.execute(
                        "INSERT INTO habits (id, name) VALUES (%s, %s)",
                        (habit_id, name)
                    )
                
                # Синхронизация статистики
                cursor.execute("DELETE FROM stats")
                for date, habits_data in self.stats.items():
                    for habit_id, status in habits_data.items():
                        cursor.execute(
                            "INSERT INTO stats (date, habit_id, status) VALUES (%s, %s, %s)",
                            (date, habit_id, status)
                        )
                
                conn.commit()
                self.last_sync = time.time()
                print(f"Синхронизация с БД завершена: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                
        except Exception as e:
            print(f"Ошибка синхронизации с БД: {e}")
    
    def load_from_db(self):
        """Полная загрузка данных из БД в кэш (перетирает текущее состояние)."""
        try:
            with psycopg2.connect(**DB_CONFIG) as conn:
                cursor = conn.cursor()
                
                # Загружаем привычки
                cursor.execute("SELECT id, name FROM habits")
                self.habits = {str(row[0]): row[1] for row in cursor.fetchall()}
                
                # Загружаем статистику
                cursor.execute("SELECT date, habit_id, status FROM stats")
                self.stats = {}
                for row in cursor.fetchall():
                    date, habit_id, status = row
                    if date not in self.stats:
                        self.stats[date] = {}
                    self.stats[date][str(habit_id)] = bool(status)
                
                print(f"Данные загружены из БД: {len(self.habits)} привычек, {len(self.stats)} дней")
                
        except Exception as e:
            print(f"Ошибка загрузки из БД: {e}")
    
    def get_habits(self) -> Dict[str, str]:
        """Получить привычки из кэша"""
        with self.lock:
            return self.habits.copy()
    
    def get_stats(self) -> Dict[str, Dict[str, bool]]:
        """Получить статистику из кэша"""
        with self.lock:
            return {date: habits.copy() for date, habits in self.stats.items()}
    
    def add_habit(self, habit_id: str, name: str):
        """Добавить привычку в кэш"""
        with self.lock:
            self.habits[habit_id] = name
    
    def update_stat(self, date: str, habit_id: str, status: bool):
        """Обновить статус привычки в кэше"""
        with self.lock:
            if date not in self.stats:
                self.stats[date] = {}
            self.stats[date][habit_id] = status

# Глобальный кэш данных
data_cache = DataCache()

# Инициализация БД
def init_database():
    """Создание таблиц в БД (PostgreSQL диалект).

    Схема:
    - habits(id, name, created_at)
    - stats(id SERIAL, date VARCHAR(8), habit_id, status BOOLEAN, created_at)
      + CONSTRAINT unique_date_habit UNIQUE (date, habit_id)
      + FOREIGN KEY (habit_id) REFERENCES habits(id) ON DELETE CASCADE
    """
    try:
        with psycopg2.connect(**DB_CONFIG) as conn:
            cursor = conn.cursor()
            
            # Таблица привычек
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS habits (
                    id VARCHAR(50) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Таблица статистики
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    id SERIAL PRIMARY KEY,
                    date VARCHAR(8) NOT NULL,
                    habit_id VARCHAR(50) NOT NULL,
                    status BOOLEAN NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT unique_date_habit UNIQUE (date, habit_id),
                    CONSTRAINT fk_stats_habit FOREIGN KEY (habit_id) REFERENCES habits(id) ON DELETE CASCADE
                )
            """)
            
            conn.commit()
            print("Таблицы БД созданы/проверены")
            
    except Exception as e:
        print(f"Ошибка инициализации БД: {e}")


def calc_streaks(data, habits):
    """Подсчитать лучшую/текущую/прерванную серии по каждой привычке.

    data: {"YYYYMMDD": {habit_id: bool}}
    habits: {habit_id: habit_name}
    Возвращает: {habit_name: (best, current, broken_best)}
    """
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


def is_week_gold(date, data, habits):
    """Проверяет «золотую неделю»: все привычки выполнены каждый день недели."""
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
    """Возвращает эмодзи статуса для дня (⭐/🔴/🟡/🟢)."""
    today = datetime.date.today()
    date = datetime.datetime.strptime(date_str, "%Y%m%d").date()

    if date < START_DATE:
        return ""

    if is_week_gold(date, data, habits):
        return " ⭐"

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


def build_calendar(year, month, data, habits):
    """Построение инлайн-календаря с днями и навигацией по месяцам."""
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
    """Меню статусов привычек за день (переключение по клику)."""
    kb = InlineKeyboardMarkup(row_width=1)
    habits_data = data.get(date_str, {})
    for h_id, name in habits.items():
        status = "✅" if habits_data.get(h_id, False) else ""
        cb = f"toggle_{date_str}_{h_id}"
        kb.add(InlineKeyboardButton(f"{status} {name}", callback_data=cb))
    kb.add(InlineKeyboardButton("⬅️ Назад к календарю", callback_data=f"back_{date_str[:6]}"))
    return kb

def build_main_text(data, habits):
    """Текст главного экрана с лучшими результатами по привычкам."""
    streaks = calc_streaks(data, habits)
    lines = ["Лучшие результаты:\n"]
    for habit, (best, current, broken) in streaks.items():
        if broken and broken != best:
            lines.append(f"{habit} — {best} дней (был {broken})")
        else:
            lines.append(f"{habit} — {best} дней")
    lines.append("\nВыбери день:")
    return "\n".join(lines)


user_states = {}  # хранение состояния пользователей

@bot.message_handler(commands=["start"])
def send_welcome(message):
    """Старт: показать календарь и лучшие результаты (без обращения к БД)."""
    now = datetime.date.today()
    data = data_cache.get_stats()
    habits = data_cache.get_habits()
    kb = build_calendar(now.year, now.month, data, habits)
    text = build_main_text(data, habits)
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["upload"])
def force_upload(message):
    """Принудительно выгрузить кэш в БД и обновить экран."""
    try:
        data_cache._sync_to_db()
        bot.reply_to(message, "✅ Данные успешно загружены в базу данных!")
        
        # Отображаем главное меню
        now = datetime.date.today()
        data = data_cache.get_stats()
        habits = data_cache.get_habits()
        kb = build_calendar(now.year, now.month, data, habits)
        text = build_main_text(data, habits)
        bot.send_message(message.chat.id, text, reply_markup=kb)
        
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка загрузки: {e}")


@bot.message_handler(commands=["reload"])
def reload_cache(message):
    """Полностью перезагрузить кэш из БД и обновить экран."""
    try:
        data_cache.load_from_db()
        bot.reply_to(message, "✅ Кэш перезагружен из базы данных!")
        
        # Отображаем главное меню
        now = datetime.date.today()
        data = data_cache.get_stats()
        habits = data_cache.get_habits()
        kb = build_calendar(now.year, now.month, data, habits)
        text = build_main_text(data, habits)
        bot.send_message(message.chat.id, text, reply_markup=kb)
        
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка перезагрузки: {e}")


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    """Обработка всех callback-кнопок календаря и меню дня."""
    data = data_cache.get_stats()
    habits = data_cache.get_habits()

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
        # Получаем текущий статус и инвертируем его
        current_status = data.get(date_str, {}).get(habit_id, False)
        new_status = not current_status
        
        # Обновляем в кэше
        data_cache.update_stat(date_str, habit_id, new_status)
        
        # Обновляем локальную копию для отображения
        data = data_cache.get_stats()
        # Сохраняем единый формат даты dd.mm.yyyy
        text = f"Статус привычек за {date_str[6:]}.{date_str[4:6]}.{date_str[:4]}"
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
    """Обработка текстовых сообщений.

    Если ожидается название новой привычки (состояние "waiting_habit"),
    добавляем её в кэш и показываем главный экран.
    """
    if user_states.get(message.from_user.id) == "waiting_habit":
        habits = data_cache.get_habits()
        new_id = str(len(habits) + 1)
        
        # Добавляем привычку в кэш
        data_cache.add_habit(new_id, message.text.strip())
        
        user_states.pop(message.from_user.id)

        data = data_cache.get_stats()
        now = datetime.date.today()
        kb = build_calendar(now.year, now.month, data, habits)
        text = build_main_text(data, habits)
        bot.send_message(message.chat.id, f"Привычка «{message.text}» добавлена!", reply_markup=kb)

# Инициализация при запуске
if __name__ == "__main__":
    print("Инициализация БД...")
    init_database()
    
    print("Загрузка данных из БД...")
    data_cache.load_from_db()
    
    print("Бот запущен!")
    bot.infinity_polling()
