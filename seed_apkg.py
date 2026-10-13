"""
Завантажує .apkg НАПРЯМУ (без проміжного CSV) у базу бота:
- дістає текст карток (поля notes.flds),
- дістає аудіо-файли вимови ([sound:xxx.mp3] в тексті поля) і копіює їх
  у MEDIA_ROOT/<slug>/, щоб бот міг надіслати їх через send_voice.

Запуск:
    python seed_apkg.py mydeck.apkg --slug oxford3000 --title "Oxford 3000"

Якщо front/back переплутані — підберіть індекси полів (з 0):
    python seed_apkg.py mydeck.apkg --slug oxford3000 --title "Oxford 3000" \
        --front-index 0 --back-index 2

Якщо колода новіша і файл всередині називається collection.anki21b (zstd):
    pip install zstandard
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

FIELD_SEP = "\x1f"
SOUND_RE = re.compile(r"\[sound:(.*?)\]")
HTML_TAG_RE = re.compile(r"<[^>]+>")


def find_collection_file(zf: zipfile.ZipFile, extract_dir: Path) -> Path:
    names = zf.namelist()
    for candidate in ("collection.anki21b", "collection.anki21", "collection.anki2"):
        if candidate in names:
            zf.extract(candidate, extract_dir)
            path = extract_dir / candidate
            if candidate == "collection.anki21b":
                path = decompress_zstd(path)
            return path
    raise FileNotFoundError("У архіві немає файлу колекції — це точно .apkg?")


def decompress_zstd(path: Path) -> Path:
    try:
        import zstandard
    except ImportError as e:
        raise SystemExit("pip install zstandard  # потрібно для новіших .apkg") from e
    out_path = path.with_suffix(".sqlite")
    with open(path, "rb") as src, open(out_path, "wb") as dst:
        zstandard.ZstdDecompressor().copy_stream(src, dst)
    return out_path


def load_media_map(zf: zipfile.ZipFile, extract_dir: Path) -> dict[str, str]:
    """Файл 'media' у корені apkg — це JSON {"0": "orig_name.mp3", "1": ...}."""
    if "media" not in zf.namelist():
        return {}
    zf.extract("media", extract_dir)
    with open(extract_dir / "media", encoding="utf-8") as f:
        return json.load(f)  # {"0": "hello.mp3", ...}


def clean_text(field: str) -> tuple[str, str | None]:
    """Повертає (текст_без_sound_і_html, ім'я_звукового_файлу_або_None)."""
    match = SOUND_RE.search(field)
    audio_name = match.group(1) if match else None
    text = SOUND_RE.sub("", field)
    text = HTML_TAG_RE.sub("", text).replace("&nbsp;", " ").strip()
    return text, audio_name


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("apkg_path")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--front-index", type=int, default=0)
    parser.add_argument("--back-index", type=int, default=1)
    parser.add_argument(
        "--front-audio-index", type=int, default=None,
        help="Індекс окремого поля зі звуком вимови (якщо аудіо не всередині front-поля)",
    )
    parser.add_argument(
        "--back-audio-index", type=int, default=None,
        help="Індекс окремого поля зі звуком для back (напр. аудіо прикладу речення)",
    )
    parser.add_argument("--db-path", default=os.environ.get("DB_PATH", "/data/cards.db"))
    parser.add_argument("--media-root", default=os.environ.get("MEDIA_ROOT", "/data/media"))
    args = parser.parse_args()

    deck_media_dir = Path(args.media_root) / args.slug
    deck_media_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(args.apkg_path) as zf:
            db_path = find_collection_file(zf, tmp_path)
            media_map = load_media_map(zf, tmp_path)  # numeric_name -> original_filename

            # копіюємо ВСІ медіафайли колоди одразу (простіше, ніж вибірково)
            for numeric_name, original_name in media_map.items():
                if numeric_name in zf.namelist():
                    zf.extract(numeric_name, tmp_path)
                    shutil.copy(tmp_path / numeric_name, deck_media_dir / original_name)

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT flds FROM notes").fetchall()
        conn.close()

    out_conn = sqlite3.connect(args.db_path)
    out_conn.execute("INSERT OR IGNORE INTO decks (slug, title) VALUES (?, ?)", (args.slug, args.title))
    out_conn.commit()
    deck_id = out_conn.execute("SELECT id FROM decks WHERE slug = ?", (args.slug,)).fetchone()[0]

    added = 0
    for (flds,) in rows:
        parts = flds.split(FIELD_SEP)
        needed_max = max(
            args.front_index, args.back_index,
            args.front_audio_index or 0, args.back_audio_index or 0,
        )
        if len(parts) <= needed_max:
            continue
        front_raw, back_raw = parts[args.front_index], parts[args.back_index]
        front, front_audio = clean_text(front_raw)
        back, back_audio = clean_text(back_raw)

        # якщо аудіо лежить в окремому полі (не всередині front/back) — беремо звідти
        if args.front_audio_index is not None:
            _, explicit_audio = clean_text(parts[args.front_audio_index])
            front_audio = explicit_audio or front_audio
        if args.back_audio_index is not None:
            _, explicit_audio = clean_text(parts[args.back_audio_index])
            back_audio = explicit_audio or back_audio

        if not front or not back:
            continue
        # аудіо зберігаємо як шлях відносно MEDIA_ROOT: "<slug>/<файл>"
        front_audio_rel = f"{args.slug}/{front_audio}" if front_audio else None
        back_audio_rel = f"{args.slug}/{back_audio}" if back_audio else None
        out_conn.execute(
            "INSERT INTO deck_cards (deck_id, front, back, front_audio, back_audio) VALUES (?, ?, ?, ?, ?)",
            (deck_id, front, back, front_audio_rel, back_audio_rel),
        )
        added += 1

    out_conn.commit()
    out_conn.close()
    print(f"Додано {added} карток у колоду '{args.slug}'. Аудіо скопійовано в {deck_media_dir}")


if __name__ == "__main__":
    main()
