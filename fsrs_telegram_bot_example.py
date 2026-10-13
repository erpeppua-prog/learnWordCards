"""
Мінімальний приклад: Telegram SRS-бот на py-fsrs + aiogram + SQLite.

Встановлення:
    pip install fsrs aiogram

Це СКЕЛЕТ для розуміння архітектури, не production-код:
- немає обробки помилок мережі, немає FSM-станів для введення карток,
- немає пагінації черги, немає авторизації.
"""

import json
import os
import sqlite3
from datetime import datetime, UTC

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from fsrs import Scheduler, Card, Rating

BOT_TOKEN = os.environ["BOT_TOKEN"]  # передається через .env / docker-compose
DB_PATH = os.environ.get("DB_PATH", "/data/cards.db")
MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "/data/media")  # аудіо з .apkg-колод

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
scheduler = Scheduler()  # дефолтні параметри FSRS-6; пізніше можна підставити навчені

# ---------- База даних ----------
#
# deck_cards  — спільна бібліотека контенту (одна копія на сервері,
#               наповнюється через seed_deck.py, користувачі її не редагують)
# user_cards  — особистий FSRS-прогрес користувача по конкретній картці
#               з deck_cards; тут кожен рядок унікальний для (user_id, deck_card_id)

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")  # дозволяє читати, поки триває запис — важливо при кількох користувачах
conn.execute("""
CREATE TABLE IF NOT EXISTS decks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,      -- напр. "oxford3000"
    title TEXT NOT NULL
)
""")
conn.execute("""
CREATE TABLE IF NOT EXISTS deck_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id INTEGER NOT NULL REFERENCES decks(id),
    front TEXT NOT NULL,
    back TEXT NOT NULL,
    front_audio TEXT,   -- шлях відносно MEDIA_ROOT, або NULL якщо аудіо немає
    back_audio TEXT
)
""")
conn.execute("""
CREATE TABLE IF NOT EXISTS user_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    deck_card_id INTEGER NOT NULL REFERENCES deck_cards(id),
    fsrs_state TEXT NOT NULL,       -- JSON від card.to_dict()
    due TEXT NOT NULL,              -- ISO datetime
    UNIQUE(user_id, deck_card_id)
)
""")
conn.commit()


def own_card_from_text(user_id: int, front: str, back: str) -> None:
    """Додати картку, яку користувач вписав сам командою /add (не зі спільної колоди)."""
    deck_row = conn.execute("SELECT id FROM decks WHERE slug = ?", ("_personal",)).fetchone()
    if deck_row is None:
        conn.execute("INSERT INTO decks (slug, title) VALUES (?, ?)", ("_personal", "Особисті картки"))
        conn.commit()
        deck_row = conn.execute("SELECT id FROM decks WHERE slug = ?", ("_personal",)).fetchone()
    deck_id = deck_row[0]
    cur = conn.execute("INSERT INTO deck_cards (deck_id, front, back) VALUES (?, ?, ?)", (deck_id, front, back))
    conn.commit()
    _attach_card_to_user(user_id, cur.lastrowid)


def _attach_card_to_user(user_id: int, deck_card_id: int) -> None:
    card = Card()  # due = зараз
    conn.execute(
        "INSERT OR IGNORE INTO user_cards (user_id, deck_card_id, fsrs_state, due) VALUES (?, ?, ?, ?)",
        (user_id, deck_card_id, json.dumps(card.to_dict()), card.due.isoformat()),
    )


def list_decks():
    return conn.execute("SELECT slug, title FROM decks WHERE slug != '_personal'").fetchall()


def add_deck_for_user(user_id: int, slug: str) -> int:
    """Копіює всі картки колоди в чергу користувача. Повертає кількість доданих карток."""
    deck_row = conn.execute("SELECT id FROM decks WHERE slug = ?", (slug,)).fetchone()
    if deck_row is None:
        return 0
    deck_id = deck_row[0]
    card_ids = [r[0] for r in conn.execute("SELECT id FROM deck_cards WHERE deck_id = ?", (deck_id,))]
    for deck_card_id in card_ids:
        _attach_card_to_user(user_id, deck_card_id)
    conn.commit()
    return len(card_ids)


def get_next_due_card(user_id: int):
    now = datetime.now(UTC).isoformat()
    row = conn.execute(
        "SELECT uc.id, dc.front, dc.back, dc.front_audio, dc.back_audio, uc.fsrs_state "
        "FROM user_cards uc JOIN deck_cards dc ON dc.id = uc.deck_card_id "
        "WHERE uc.user_id = ? AND uc.due <= ? ORDER BY uc.due LIMIT 1",
        (user_id, now),
    ).fetchone()
    return row  # (user_card_id, front, back, front_audio, back_audio, fsrs_state_json) або None


def apply_review(user_card_id: int, rating: Rating) -> None:
    row = conn.execute("SELECT fsrs_state FROM user_cards WHERE id = ?", (user_card_id,)).fetchone()
    card = Card.from_dict(json.loads(row[0]))
    card, review_log = scheduler.review_card(card, rating)
    conn.execute(
        "UPDATE user_cards SET fsrs_state = ?, due = ? WHERE id = ?",
        (json.dumps(card.to_dict()), card.due.isoformat(), user_card_id),
    )
    conn.commit()


# ---------- Хендлери бота ----------

show_kb = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="👁 Показати відповідь", callback_data="show"),
]])

rating_kb = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🔄 Again", callback_data="rate:1"),
    InlineKeyboardButton(text="😓 Hard", callback_data="rate:2"),
    InlineKeyboardButton(text="👍 Good", callback_data="rate:3"),
    InlineKeyboardButton(text="😎 Easy", callback_data="rate:4"),
]])

# тимчасове зберігання "яка картка зараз показана" (у проді — Redis/FSM)
current_card: dict[int, int] = {}
current_card_audio: dict[int, str | None] = {}


def count_due(user_id: int) -> int:
    now = datetime.now(UTC).isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM user_cards WHERE user_id = ? AND due <= ?", (user_id, now)
    ).fetchone()[0]


async def send_next_card(message: Message, user_id: int) -> None:
    row = get_next_due_card(user_id)
    if row is None:
        current_card.pop(user_id, None)
        await message.answer("На сьогодні карток немає 🎉")
        return
    card_id, front, _back, front_audio, back_audio, _state = row
    current_card[user_id] = card_id
    current_card_audio[user_id] = back_audio

    left = count_due(user_id)
    await message.answer(f"❓ {front}\n\n<i>Залишилось: {left}</i>", reply_markup=show_kb, parse_mode="HTML")
    if front_audio:
        path = os.path.join(MEDIA_ROOT, front_audio)
        if os.path.exists(path):
            await message.answer_voice(FSInputFile(path))


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привіт! Це бот для повторення карток за методом інтервального повторення.\n\n"
        "/decks — доступні готові колоди\n"
        "/add питання | відповідь — додати свою картку\n"
        "/review — почати повторення"
    )


@dp.message(Command("add"))
async def cmd_add(message: Message):
    # очікуваний формат: /add питання | відповідь
    try:
        _, payload = message.text.split(" ", 1)
        front, back = [p.strip() for p in payload.split("|", 1)]
    except ValueError:
        await message.answer("Формат: /add питання | відповідь")
        return
    own_card_from_text(message.from_user.id, front, back)
    await message.answer("Картку додано ✅")


@dp.message(Command("decks"))
async def cmd_decks(message: Message):
    decks = list_decks()
    if not decks:
        await message.answer("Поки немає жодної спільної колоди на сервері.")
        return
    lines = [f"• {title} — /adddeck_{slug}" for slug, title in decks]
    await message.answer("Доступні колоди:\n" + "\n".join(lines))


@dp.message(F.text.startswith("/adddeck_"))
async def cmd_adddeck(message: Message):
    slug = message.text.removeprefix("/adddeck_").strip()
    added = add_deck_for_user(message.from_user.id, slug)
    if added == 0:
        await message.answer("Такої колоди немає. Перевірте /decks")
    else:
        await message.answer(f"Додано {added} карток. Починайте: /review")


@dp.message(Command("review"))
async def cmd_review(message: Message):
    await send_next_card(message, message.from_user.id)


@dp.callback_query(F.data == "show")
async def on_show(callback):
    user_id = callback.from_user.id
    card_id = current_card.get(user_id)
    if card_id is None:
        await callback.answer("Спочатку /review")
        return
    row = conn.execute(
        "SELECT dc.back FROM user_cards uc JOIN deck_cards dc ON dc.id = uc.deck_card_id WHERE uc.id = ?",
        (card_id,),
    ).fetchone()

    back_audio = current_card_audio.get(user_id)
    if back_audio:
        path = os.path.join(MEDIA_ROOT, back_audio)
        if os.path.exists(path):
            await callback.message.answer_voice(FSInputFile(path))
    await callback.message.answer(f"✅ {row[0]}", reply_markup=rating_kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("rate:"))
async def on_rating(callback):
    user_id = callback.from_user.id
    card_id = current_card.get(user_id)
    if card_id is None:
        await callback.answer("Спочатку викличте /review")
        return

    rating_value = int(callback.data.split(":")[1])
    apply_review(card_id, Rating(rating_value))
    await callback.answer()
    await send_next_card(callback.message, user_id)  # одразу наступна картка, без ручного /review


if __name__ == "__main__":
    import asyncio
    asyncio.run(dp.start_polling(bot))
