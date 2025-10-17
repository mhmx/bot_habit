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
import traceback
import logging
from logging.handlers import RotatingFileHandler
from typing import Dict, Any
from config import TOKEN, DB_CONFIG

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Вывод в консоль
        RotatingFileHandler('bot.log', maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')  # Ротация логов
    ]
)
logger = logging.getLogger(__name__)

# Подавляем подробные логи от telebot для ошибки 409
telebot_logger = logging.getLogger('telebot')
telebot_logger.setLevel(logging.WARNING)

# Фильтр для подавления повторяющихся ошибок 409
class Error409Filter(logging.Filter):
    def filter(self, record):
        if hasattr(record, 'msg') and isinstance(record.msg, str):
            if "Error code: 409" in record.msg and "Conflict: terminated by other getUpdates request" in record.msg:
                return False
        return True

# Применяем фильтр к логгеру telebot
telebot_logger.addFilter(Error409Filter())

bot = telebot.TeleBot(TOKEN)

# Кастомный обработчик исключений для telebot
class CustomExceptionHandler:
    """Обработчик исключений telebot для краткого вывода ошибок"""
    def __init__(self):
        self.error_409_count = 0
        self.last_error_409_time = 0
    
    def handle(self, exception):
        """Обработка исключения"""
        error_msg = str(exception)
        current_time = time.time()
        
        if "Error code: 409" in error_msg and "Conflict: terminated by other getUpdates request" in error_msg:
            self.error_409_count += 1
            # Выводим сообщение только раз в минуту
            if current_time - self.last_error_409_time > 60:
                logger.error(f"Ошибка 409: Запущено несколько экземпляров бота одновременно (всего ошибок: {self.error_409_count})")
                self.last_error_409_time = current_time
        else:
            logger.error(f"Ошибка telebot: {error_msg}")
        
        return True  # Возвращаем True, чтобы telebot знал, что исключение обработано

# Устанавливаем кастомный обработчик
bot.exception_handler = CustomExceptionHandler()

def error_handler(func):
    """Декоратор для обработки ошибок в функциях бота"""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_msg = str(e)
            # Краткое сообщение для ошибки 409 (конфликт экземпляров бота)
            if "Error code: 409" in error_msg and "Conflict: terminated by other getUpdates request" in error_msg:
                logger.error("Ошибка 409: Запущено несколько экземпляров бота одновременно")
            else:
                logger.error(f"Ошибка в {func.__name__}: {error_msg}")
                logger.error(f"Трассировка: {traceback.format_exc()}")
            return None
    return wrapper

# Нижняя граница дат, которые учитываются в интерфейсе и при подсчёте серий
START_DATE = datetime.date(2025, 9, 27)
# Количество дней для формирования устойчивой привычки (используется в отображении прогресса)
SUCCESS_DAY_COUNT = 21

# Кэш в памяти
class DataCache:
    """Потокобезопасный кэш состояния бота.

    Назначение:
    - сократить обращения к БД для операций UI
    - хранить словари привычек и дневных статусов
    - периодически синхронизировать всё в БД (см. _background_sync)
    """
    def __init__(self):
        self.habits: Dict[str, Dict[str, str]] = {}  # {habit_id: {"name": str, "start_date": str}}
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
            try:
                time.sleep(60)  # Проверяем каждую минуту
                if time.time() - self.last_sync >= self.sync_interval:
                    self._sync_to_db()
            except Exception as e:
                logger.error(f"Ошибка в фоновой синхронизации: {str(e)}")
                logger.error(f"Трассировка: {traceback.format_exc()}")
                time.sleep(60)  # Продолжаем работу даже при ошибке
    
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
                for habit_id, habit_data in self.habits.items():
                    cursor.execute(
                        "INSERT INTO habits (id, name, start_date) VALUES (%s, %s, %s)",
                        (habit_id, habit_data["name"], habit_data["start_date"])
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
                logger.info(f"Синхронизация с БД завершена: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                
        except Exception as e:
            logger.error(f"Ошибка синхронизации с БД: {str(e)}")
            logger.error(f"Трассировка: {traceback.format_exc()}")
    
    def load_from_db(self):
        """Полная загрузка данных из БД в кэш (перетирает текущее состояние)."""
        try:
            with psycopg2.connect(**DB_CONFIG) as conn:
                cursor = conn.cursor()
                
                # Загружаем привычки
                cursor.execute("SELECT id, name, start_date FROM habits")
                self.habits = {}
                for row in cursor.fetchall():
                    habit_id, name, start_date = row
                    self.habits[str(habit_id)] = {"name": name, "start_date": start_date}
                
                # Загружаем статистику
                cursor.execute("SELECT date, habit_id, status FROM stats")
                self.stats = {}
                for row in cursor.fetchall():
                    date, habit_id, status = row
                    if date not in self.stats:
                        self.stats[date] = {}
                    self.stats[date][str(habit_id)] = bool(status)
                
                logger.info(f"Данные загружены из БД: {len(self.habits)} привычек, {len(self.stats)} дней")
                
        except Exception as e:
            logger.error(f"Ошибка загрузки из БД: {str(e)}")
            logger.error(f"Трассировка: {traceback.format_exc()}")
    
    def get_habits(self) -> Dict[str, str]:
        """Получить привычки из кэша (только имена для обратной совместимости)"""
        with self.lock:
            return {habit_id: habit_data["name"] for habit_id, habit_data in self.habits.items()}
    
    def get_habits_full(self) -> Dict[str, Dict[str, str]]:
        """Получить полную информацию о привычках из кэша"""
        with self.lock:
            return {habit_id: habit_data.copy() for habit_id, habit_data in self.habits.items()}
    
    def get_stats(self) -> Dict[str, Dict[str, bool]]:
        """Получить статистику из кэша"""
        with self.lock:
            return {date: habits.copy() for date, habits in self.stats.items()}
    
    def add_habit(self, habit_id: str, name: str, start_date: str):
        """Добавить привычку в кэш"""
        with self.lock:
            self.habits[habit_id] = {"name": name, "start_date": start_date}
    
    def update_stat(self, date: str, habit_id: str, status: bool):
        """Обновить статус привычки в кэше"""
        with self.lock:
            if date not in self.stats:
                self.stats[date] = {}
            self.stats[date][habit_id] = status

# Глобальный кэш данных
data_cache = DataCache()

# Инициализация БД
def migrate_database():
    """Миграция существующих данных для добавления поля start_date."""
    try:
        with psycopg2.connect(**DB_CONFIG) as conn:
            cursor = conn.cursor()
            
            # Проверяем, существует ли поле start_date
            cursor.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='habits' AND column_name='start_date'
            """)
            
            if not cursor.fetchone():
                # Добавляем поле start_date
                cursor.execute("ALTER TABLE habits ADD COLUMN start_date VARCHAR(8)")
                
                # Устанавливаем значение по умолчанию для существующих записей
                cursor.execute("UPDATE habits SET start_date = '20250927' WHERE start_date IS NULL")
                
                # Делаем поле обязательным
                cursor.execute("ALTER TABLE habits ALTER COLUMN start_date SET NOT NULL")
                
                conn.commit()
                logger.info("Миграция БД завершена: добавлено поле start_date")
            else:
                logger.info("Поле start_date уже существует в таблице habits")
                
    except Exception as e:
        logger.error(f"Ошибка миграции БД: {str(e)}")
        logger.error(f"Трассировка: {traceback.format_exc()}")

def init_database():
    """Создание таблиц в БД (PostgreSQL диалект).

    Схема:
    - habits(id, name, start_date, created_at)
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
                    start_date VARCHAR(8) NOT NULL,
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
            logger.info("Таблицы БД созданы/проверены")
            
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {str(e)}")
        logger.error(f"Трассировка: {traceback.format_exc()}")

def calc_streaks(data, habits_full):
    """Подсчитать лучшую/текущую серии по каждой привычке.

    data: {"YYYYMMDD": {habit_id: bool}}
    habits_full: {habit_id: {"name": str, "start_date": str}}
    Возвращает: {habit_name: {"current": int, "best": int}}
    """
    results = {}
    today = datetime.date.today()
    today_str = today.strftime("%Y%m%d")

    for h_id, habit_data in habits_full.items():
        habit_name = habit_data["name"]
        start_date_str = habit_data["start_date"]
        
        # Парсим дату начала привычки
        try:
            habit_start_date = datetime.datetime.strptime(start_date_str, "%Y%m%d").date()
        except ValueError:
            habit_start_date = START_DATE
        
        # Инициализируем streak для этой привычки
        streaks = {"current": 0, "best": 0}
        current_streak = 0
        max_streak = 0
        
        # Проходим по всем датам от начала привычки до вчера
        current_date = habit_start_date
        while current_date < today:
            date_str = current_date.strftime("%Y%m%d")
            
            # Проверяем статус привычки на эту дату
            if date_str in data and h_id in data[date_str] and data[date_str][h_id]:
                current_streak += 1
                if current_streak > max_streak:
                    max_streak = current_streak
            else:
                current_streak = 0
            
            current_date += datetime.timedelta(days=1)
        
        # Для текущего дня проверяем статус отдельно
        if today_str in data and h_id in data[today_str] and data[today_str][h_id]:
            current_streak += 1
            if current_streak > max_streak:
                max_streak = current_streak
        
        streaks["current"] = current_streak
        streaks["best"] = max_streak
        results[habit_name] = streaks

    return results

def is_week_gold(date, data, habits_full):
    """Проверяет «золотую неделю»: все привычки выполнены каждый день недели."""
    monday = date - datetime.timedelta(days=date.weekday())
    sunday = monday + datetime.timedelta(days=6)

    for d in (monday + datetime.timedelta(days=i) for i in range(7)):
        d_str = d.strftime("%Y%m%d")
        if d_str not in data:
            return False
        for h_id, habit_data in habits_full.items():
            try:
                habit_start_date = datetime.datetime.strptime(habit_data["start_date"], "%Y%m%d").date()
                if d >= habit_start_date and not data[d_str].get(h_id, False):
                    return False
            except ValueError:
                if d >= START_DATE and not data[d_str].get(h_id, False):
                    return False
    return True

def day_status_emoji(date_str, data, habits_full):
    """Возвращает эмодзи статуса для дня (⭐/🔴/🟡/🟢)."""
    today = datetime.date.today()
    date = datetime.datetime.strptime(date_str, "%Y%m%d").date()

    if date < START_DATE:
        return ""

    if is_week_gold(date, data, habits_full):
        return "⭐"

    if date_str not in data:
        if date < today:
            return "🔴"
        return ""
    
    habits_data = data[date_str]
    done = 0
    total_active = 0
    
    for h_id, habit_data in habits_full.items():
        try:
            habit_start_date = datetime.datetime.strptime(habit_data["start_date"], "%Y%m%d").date()
            if date >= habit_start_date:
                total_active += 1
                if habits_data.get(h_id, False):
                    done += 1
        except ValueError:
            if date >= START_DATE:
                total_active += 1
                if habits_data.get(h_id, False):
                    done += 1
    
    if total_active == 0:
        return ""
    elif done == 0:
        if date < today:
            return "🔴"
        else:
            return ""
    elif done == total_active:
        return "🟢"
    else:
        return "🟡"

def build_calendar(year, month, data, habits_full):
    """Построение инлайн-календаря с днями и навигацией по месяцам."""
    kb = InlineKeyboardMarkup(row_width=7)
    cal = calendar.Calendar(firstweekday=0)
    month_days = cal.monthdayscalendar(year, month)

    MONTHS_RU = [
        "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
    ]
    DAYS_RU = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]

    kb.add(InlineKeyboardButton(f"{MONTHS_RU[month]} {year}", callback_data="none"))

    days_row = [InlineKeyboardButton(d, callback_data="none") for d in DAYS_RU]
    kb.row(*days_row)

    for week in month_days:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="none"))
            else:
                date_str = f"{year}{month:02d}{day:02d}"
                emoji = day_status_emoji(date_str, data, habits_full)
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

def build_day_menu(date_str, data, habits_full):
    """Меню статусов привычек за день (переключение по клику)."""
    kb = InlineKeyboardMarkup(row_width=1)
    habits_data = data.get(date_str, {})
    date = datetime.datetime.strptime(date_str, "%Y%m%d").date()
    
    for h_id, habit_data in habits_full.items():
        habit_name = habit_data["name"]
        try:
            habit_start_date = datetime.datetime.strptime(habit_data["start_date"], "%Y%m%d").date()
            if date >= habit_start_date:
                status = "✅" if habits_data.get(h_id, False) else ""
                cb = f"toggle_{date_str}_{h_id}"
                kb.add(InlineKeyboardButton(f"{status} {habit_name}", callback_data=cb))
        except ValueError:
            if date >= START_DATE:
                status = "✅" if habits_data.get(h_id, False) else ""
                cb = f"toggle_{date_str}_{h_id}"
                kb.add(InlineKeyboardButton(f"{status} {habit_name}", callback_data=cb))
    
    kb.add(InlineKeyboardButton("⬅️ Назад к календарю", callback_data=f"back_{date_str[:6]}"))
    return kb

def build_main_text(data, habits_full):
    """Текст главного экрана с лучшими результатами по привычкам."""
    streaks = calc_streaks(data, habits_full)
    today = datetime.date.today()
    today_str = today.strftime("%Y%m%d")
    lines = [f"-----------Лучшие результаты:-----------\n"]
    
    for habit, streak_data in streaks.items():
        current = streak_data["current"]
        best = streak_data["best"]
        
        # Проверяем, есть ли статус за сегодня
        today_status_exists = today_str in data and any(h_id in data[today_str] for h_id, h_data in habits_full.items() if h_data["name"] == habit)
        today_status = False
        if today_status_exists:
            for h_id, h_data in habits_full.items():
                if h_data["name"] == habit and h_id in data[today_str]:
                    today_status = data[today_str][h_id]
                    break
        
        if current > 0 or (today_status_exists and today_status):
            # Есть текущая серия (включая случай, если сегодня отмечено)
            if current == best:
                if current < SUCCESS_DAY_COUNT:
                    lines.append(f"🔘 {habit} — {current}/{SUCCESS_DAY_COUNT} дн. ({round(current/SUCCESS_DAY_COUNT*100)}%)")
                else:
                    lines.append(f"🔥 {habit} — {current} дн.")
            else:
                if current < SUCCESS_DAY_COUNT:
                    lines.append(f"🔘 {habit} — {current}/{SUCCESS_DAY_COUNT} дн. ({round(current/SUCCESS_DAY_COUNT*100)}%) 🏆: {best} дн.")
                else:
                    lines.append(f"🔘 {habit} — ✅ {current} дней (🏆 {best})")
        else:
            # Нет текущей серии
            if today_status_exists and not today_status:
                # Сегодня отмечено как невыполненное
                if best > 0:
                    lines.append(f"❌ {habit} — серия прервана. 🏆: {best} дн.")
                else:
                    lines.append(f"🔘 {habit} — пока нет результатов")
            else:
                # Сегодня ещё не отмечено
                if best > 0:
                    lines.append(f"🔘 {habit} — сегодня ещё не отмечено. 🏆: {best} дн.")
                else:
                    lines.append(f"🔘 {habit} — сегодня ещё не отмечено")
    
    lines.append(" ")
    return "\n".join(lines)

user_states = {}  # хранение состояния пользователей

@bot.message_handler(commands=["start"])
@error_handler
def send_welcome(message):
    """Старт: показать календарь и лучшие результаты (без обращения к БД)."""
    logger.info(f"Пользователь {message.from_user.id} ({message.from_user.username}) запустил бота")
    now = datetime.date.today()
    data = data_cache.get_stats()
    habits_full = data_cache.get_habits_full()
    kb = build_calendar(now.year, now.month, data, habits_full)
    text = build_main_text(data, habits_full)
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.message_handler(commands=["upload"])
@error_handler
def force_upload(message):
    """Принудительно выгрузить кэш в БД и обновить экран."""
    logger.info(f"Пользователь {message.from_user.id} выполнил принудительную загрузку данных")
    try:
        data_cache._sync_to_db()
        bot.reply_to(message, "✅ Данные успешно загружены в базу данных!")
        
        # Отображаем главное меню
        now = datetime.date.today()
        data = data_cache.get_stats()
        habits_full = data_cache.get_habits_full()
        kb = build_calendar(now.year, now.month, data, habits_full)
        text = build_main_text(data, habits_full)
        bot.send_message(message.chat.id, text, reply_markup=kb)
        
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка загрузки: {e}")

@bot.message_handler(commands=["reload"])
@error_handler
def reload_cache(message):
    """Полностью перезагрузить кэш из БД и обновить экран."""
    logger.info(f"Пользователь {message.from_user.id} выполнил перезагрузку кэша")
    try:
        data_cache.load_from_db()
        bot.reply_to(message, "✅ Кэш перезагружен из базы данных!")
        
        # Отображаем главное меню
        now = datetime.date.today()
        data = data_cache.get_stats()
        habits_full = data_cache.get_habits_full()
        kb = build_calendar(now.year, now.month, data, habits_full)
        text = build_main_text(data, habits_full)
        bot.send_message(message.chat.id, text, reply_markup=kb)
        
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка перезагрузки: {e}")

@bot.callback_query_handler(func=lambda call: True)
@error_handler
def callback_handler(call):
    """Обработка всех callback-кнопок календаря и меню дня."""
    data = data_cache.get_stats()
    habits_full = data_cache.get_habits_full()

    if call.data == "add_habit":
        logger.info(f"Пользователь {call.from_user.id} начал добавление новой привычки")
        bot.send_message(call.message.chat.id, "Отправь название новой привычки в формате:\n• привычка/дата (например: Читать книги/21.09.2025)\n• или просто привычка (дата будет сегодняшняя)")
        user_states[call.from_user.id] = "waiting_habit"
        return

    if call.data.startswith("day_"):
        date_str = call.data.split("_")[1]
        text = f"Статус привычек за {date_str[6:]}.{date_str[4:6]}.{date_str[:4]}"
        kb = build_day_menu(date_str, data, habits_full)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif call.data.startswith("toggle_"):
        _, date_str, habit_id = call.data.split("_", 2)
        # Получаем текущий статус и инвертируем его
        current_status = data.get(date_str, {}).get(habit_id, False)
        new_status = not current_status
        
        # Логируем изменение статуса
        habit_name = habits_full.get(habit_id, {}).get("name", f"Привычка {habit_id}")
        status_text = "выполнено" if new_status else "не выполнено"
        logger.info(f"Пользователь {call.from_user.id} изменил статус '{habit_name}' на {date_str[6:]}.{date_str[4:6]}.{date_str[:4]} - {status_text}")
        
        # Обновляем в кэше
        data_cache.update_stat(date_str, habit_id, new_status)
        
        # Обновляем локальную копию для отображения
        data = data_cache.get_stats()
        
        # Обновляем текст и меню
        text = f"Статус привычек за {date_str[6:]}.{date_str[4:6]}.{date_str[:4]}"
        kb = build_day_menu(date_str, data, habits_full)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif call.data.startswith("back_"):
        yyyymm = call.data.split("_")[1]
        year, month = int(yyyymm[:4]), int(yyyymm[4:])
        kb = build_calendar(year, month, data, habits_full)
        text = build_main_text(data, habits_full)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif call.data.startswith("month_"):
        yyyymm = call.data.split("_")[1]
        year, month = int(yyyymm[:4]), int(yyyymm[4:])
        kb = build_calendar(year, month, data, habits_full)
        text = build_main_text(data, habits_full)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb)

@bot.message_handler(func=lambda message: True)
@error_handler
def handle_text(message):
    """Обработка текстовых сообщений.

    Если ожидается название новой привычки (состояние "waiting_habit"),
    добавляем её в кэш и показываем главный экран.
    """
    if user_states.get(message.from_user.id) == "waiting_habit":
        habits_full = data_cache.get_habits_full()
        new_id = str(len(habits_full) + 1)
        
        # Парсим сообщение в формате "привычка/дата" или просто "привычка"
        text_parts = message.text.strip().split("/")
        habit_name = text_parts[0].strip()
        
        if len(text_parts) == 2:
            # Пользователь указал дату
            date_str = text_parts[1].strip()
            try:
                parsed_date = datetime.datetime.strptime(date_str, "%d.%m.%Y").date()
                start_date = parsed_date.strftime("%Y%m%d")
            except ValueError:
                bot.send_message(message.chat.id, "❌ Неверный формат даты! Используйте формат: привычка/дд.мм.гггг\nНапример: Читать книги/21.09.2025")
                return
        else:
            start_date = datetime.date.today().strftime("%Y%m%d")
        
        # Логируем добавление новой привычки
        logger.info(f"Пользователь {message.from_user.id} добавил новую привычку: '{habit_name}' с датой начала {start_date} (ID: {new_id})")
        
        # Добавляем привычку в кэш
        data_cache.add_habit(new_id, habit_name, start_date)
        
        user_states.pop(message.from_user.id)

        data = data_cache.get_stats()
        habits_full = data_cache.get_habits_full()
        now = datetime.date.today()
        kb = build_calendar(now.year, now.month, data, habits_full)
        text = build_main_text(data, habits_full)
        bot.send_message(message.chat.id, f"Привычка «{habit_name}» добавлена!", reply_markup=kb)

# Инициализация при запуске
if __name__ == "__main__":
    try:
        logger.info("Инициализация БД...")
        init_database()
        
        logger.info("Выполнение миграции БД...")
        migrate_database()
        
        logger.info("Загрузка данных из БД...")
        data_cache.load_from_db()
        
        logger.info("Бот запущен!")
        
        # Обработка ошибок polling с повторными попытками
        while True:
            try:
                bot.infinity_polling()
            except Exception as e:
                error_msg = str(e)
                if "Error code: 409" in error_msg and "Conflict: terminated by other getUpdates request" in error_msg:
                    logger.error("Ошибка 409: Запущено несколько экземпляров бота одновременно")
                    logger.info("Ожидание 10 секунд перед повторной попыткой...")
                    time.sleep(10)
                else:
                    logger.error(f"Ошибка polling: {error_msg}")
                    logger.error(f"Трассировка: {traceback.format_exc()}")
                    logger.info("Ожидание 5 секунд перед повторной попыткой...")
                    time.sleep(5)
                    
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске бота: {str(e)}")
        logger.error(f"Трассировка: {traceback.format_exc()}")