import os
import logging
import time
from datetime import datetime
from dotenv import load_dotenv
import mysql.connector
from mysql.connector import pooling

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# Загрузить .env
load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
DB_HOST = os.getenv('DB_HOST', '127.0.0.1')
DB_USER = os.getenv('DB_USER', 'todo_user')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')
DB_NAME = os.getenv('DB_NAME', 'todo_bot')

if not TELEGRAM_TOKEN:
    raise RuntimeError('TELEGRAM_TOKEN не задан. Скопируйте .env.template -> .env и заполните токен')

# Логирование
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Конфиг для подключения к MySQL
_db_config = {
    'host': DB_HOST,
    'user': DB_USER,
    'password': DB_PASSWORD,
    'database': DB_NAME,
    'charset': 'utf8mb4',
    'use_unicode': True,
}

# Попытки создать пул соединений — полезно, если БД стартует медленнее контейнера
def create_pool_with_retry(retries: int = 10, delay: int = 3):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            pool = pooling.MySQLConnectionPool(pool_name='todo_pool', pool_size=5, **_db_config)
            logger.info('Создан пул соединений к БД')
            return pool
        except Exception as e:
            last_exc = e
            logger.warning('Не удалось подключиться к БД (попытка %s/%s): %s', attempt, retries, e)
            time.sleep(delay)
    logger.error('Не удалось создать пул соединений после %s попыток', retries)
    raise last_exc

# Получить/создать пул
_pool = None
try:
    _pool = create_pool_with_retry()
except Exception as e:
    logger.error('Ошибка при создании пула: %s', e)

def get_conn():
    if _pool is None:
        return mysql.connector.connect(**_db_config)
    return _pool.get_connection()

# Инициализация схемы
def init_db():
    conn = None
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                user_id BIGINT NOT NULL,
                id INT NOT NULL AUTO_INCREMENT,
                description TEXT NOT NULL,
                done TINYINT(1) NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, id)
            ) CHARACTER SET = utf8mb4;
            """
        )
        conn.commit()
        cursor.close()
        logger.info('Таблица tasks готова')
    except Exception as e:
        logger.error('Ошибка инициализации БД: %s', e)
        raise
    finally:
        if conn:
            conn.close()

# CRUD-операции
def add_task(user_id: int, description: str) -> int:
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute(
        'SELECT COUNT(*)+1 FROM tasks WHERE user_id = %s',
        (user_id,)
    )
    next_id = cursor.fetchone()[0]

    cursor.execute(
        'INSERT INTO tasks (user_id, id, description) VALUES (%s, %s, %s)',
        (user_id, next_id, description)
    )
    conn.commit()
    cursor.close()
    conn.close()
    return next_id


def list_tasks(user_id: int, include_done: bool = False):
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    if include_done:
        cursor.execute('SELECT id, description, done, created_at FROM tasks WHERE user_id = %s ORDER BY created_at DESC', (user_id,))
    else:
        cursor.execute('SELECT id, description, done, created_at FROM tasks WHERE user_id = %s AND done = 0 ORDER BY created_at DESC', (user_id,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return rows

def mark_done(task_id: int, user_id: int) -> int:
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('UPDATE tasks SET done = 1 WHERE id = %s AND user_id = %s', (task_id, user_id))
    conn.commit()
    affected = cursor.rowcount
    cursor.close()
    conn.close()
    return affected

def delete_task(task_id: int, user_id: int) -> int:
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM tasks WHERE id = %s AND user_id = %s', (task_id, user_id))
    conn.commit()
    affected = cursor.rowcount
    cursor.close()
    conn.close()
    return affected

# === Telegram bot ===
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode='HTML')

# Главное меню
def main_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(
        KeyboardButton("➕ Новая задача"),
        KeyboardButton("📋 Список задач")
    )
    kb.row(
        KeyboardButton("ℹ️ Помощь")
    )
    return kb

@bot.message_handler(commands=['start', 'help'])
def handle_start(message):
    txt = (
        'Привет, мой достигатор!💪🏻🤓\nЧувствуешь, что не справляешься и тебе нужен помощник в планировании задач?😩'
        '\nЯ буду твоим любимым!😏😉😘\n'
        'Можешь пользоваться кнопками ниже или вводить команды вручную: 👇🏻\n'
        '/new — создать новую задачу\n'
        '/list — показать список текущих задач\n'
        '/done &lt;id&gt; — отметить задачу выполненной\n'
        '/delete &lt;id&gt; — удалить задачу\n'
        '/help — показать это сообщение'
    )
    bot.send_message(message.chat.id, txt, reply_markup=main_menu())

# Обработчики кнопок меню
@bot.message_handler(func=lambda m: m.text == "➕ Новая задача")
def menu_new_task(message):
    handle_new(message)

@bot.message_handler(func=lambda m: m.text == "📋 Список задач")
def menu_list_tasks(message):
    handle_list(message)

@bot.message_handler(func=lambda m: m.text == "ℹ️ Помощь")
def menu_help(message):
    handle_start(message)


@bot.message_handler(commands=['new'])
def handle_new(message):
    msg = bot.send_message(message.chat.id, 'Напиши текст новой задачи:', reply_markup=main_menu())
    bot.register_next_step_handler(msg, process_new_task)


def process_new_task(message):
    text = (message.text or '').strip()
    if not text:
        bot.send_message(message.chat.id, 'Пустой текст — задача не создана. Попробуйте ещё: /new',
                         reply_markup=main_menu())
        return
    try:
        tid = add_task(message.from_user.id, text)
        bot.send_message(message.chat.id, f'Задача сохранена с id={tid}', reply_markup=main_menu())
    except Exception as e:
        logger.exception('Ошибка при добавлении задачи: %s', e)
        bot.send_message(message.chat.id, 'Ошибка при сохранении задачи. Смотрите логи.', reply_markup=main_menu())


@bot.message_handler(commands=['list'])
def handle_list(message):
    try:
        rows = list_tasks(message.from_user.id)
    except Exception as e:
        logger.exception('Ошибка при выборке задач: %s', e)
        bot.send_message(message.chat.id, 'Ошибка при запросе задач. Смотрите логи.', reply_markup=main_menu())
        return

    if not rows:
        bot.send_message(message.chat.id, 'У вас нет активных задач. Создать: /new', reply_markup=main_menu())
        return

    for row in rows:
        text = f"<b>#{row['id']}</b> — {row['description']}\nсоздано: {row['created_at']}"
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton('✅ Выполнено', callback_data=f"done:{row['id']}"),
            InlineKeyboardButton('🗑 Удалить', callback_data=f"del:{row['id']}")
        )
        bot.send_message(message.chat.id, text, reply_markup=kb)


@bot.message_handler(commands=['done'])
def handle_done_cmd(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, 'Использование: /done <id>')
        return
    try:
        tid = int(parts[1])
    except ValueError:
        bot.send_message(message.chat.id, 'id должен быть числом')
        return
    try:
        affected = mark_done(tid, message.from_user.id)
        if affected:
            bot.send_message(message.chat.id, f'Задача #{tid} отмечена как выполненная')
        else:
            bot.send_message(message.chat.id, 'Задача не найдена или у вас нет к ней доступа')
    except Exception as e:
        logger.exception('Ошибка при отметке задачи: %s', e)
        bot.send_message(message.chat.id, 'Ошибка при выполнении операции. Смотрите логи.')

@bot.message_handler(commands=['delete'])
def handle_delete_cmd(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, 'Использование: /delete <id>')
        return
    try:
        tid = int(parts[1])
    except ValueError:
        bot.send_message(message.chat.id, 'id должен быть числом')
        return
    try:
        affected = delete_task(tid, message.from_user.id)
        if affected:
            bot.send_message(message.chat.id, f'Задача #{tid} удалена')
        else:
            bot.send_message(message.chat.id, 'Задача не найдена или у вас нет к ней доступа')
    except Exception as e:
        logger.exception('Ошибка при удалении задачи: %s', e)
        bot.send_message(message.chat.id, 'Ошибка при выполнении операции. Смотрите логи.')

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    data = call.data or ''
    try:
        if data.startswith('done:'):
            tid = int(data.split(':', 1)[1])
            affected = mark_done(tid, call.from_user.id)
            if affected:
                bot.answer_callback_query(call.id, 'Отмечено как выполненное')
                try:
                    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
                except Exception:
                    pass
                bot.send_message(call.message.chat.id, f'Задача #{tid} отмечена выполненной', reply_markup=main_menu())
            else:
                bot.answer_callback_query(call.id, 'Не найдено или нет прав')
        elif data.startswith('del:'):
            tid = int(data.split(':', 1)[1])
            affected = delete_task(tid, call.from_user.id)
            if affected:
                bot.answer_callback_query(call.id, 'Удалено')
                try:
                    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
                except Exception:
                    pass
                bot.send_message(call.message.chat.id, f'Задача #{tid} удалена', reply_markup=main_menu())
            else:
                bot.answer_callback_query(call.id, 'Не найдено или нет прав')
        else:
            bot.answer_callback_query(call.id, 'Неизвестная команда')
    except Exception as e:
        logger.exception('Ошибка в callback: %s', e)
        try:
            bot.answer_callback_query(call.id, 'Ошибка на сервере')
        except Exception:
            pass

if __name__ == '__main__':
    try:
        init_db()
    except Exception:
        logger.warning('Не удалось гарантировать наличие таблицы tasks. Возможны ошибки при работе.')

    logger.info('Запуск бота...')

    bot.infinity_polling(timeout=20, long_polling_timeout=5)

