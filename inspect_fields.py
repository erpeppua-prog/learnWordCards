"""
Показує поля перших кількох нотаток .apkg з їх індексами — щоб точно підібрати
--front-index / --back-index для seed_apkg.py.

Запуск:
    python inspect_fields.py mydeck.apkg
"""

import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path


def main() -> None:
    apkg_path = sys.argv[1]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(apkg_path) as zf:
            names = zf.namelist()
            db_path = None
            for candidate in ("collection.anki21b", "collection.anki21", "collection.anki2"):
                if candidate in names:
                    zf.extract(candidate, tmp_path)
                    db_path = tmp_path / candidate
                    break
            if db_path is None:
                raise SystemExit("У архіві немає файлу колекції")

        if db_path.name == "collection.anki21b":
            import zstandard
            out = db_path.with_suffix(".sqlite")
            with open(db_path, "rb") as s, open(out, "wb") as d:
                zstandard.ZstdDecompressor().copy_stream(s, d)
            db_path = out

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT flds FROM notes LIMIT 3").fetchall()
        for (flds,) in rows:
            parts = flds.split("\x1f")
            print("---")
            for i, p in enumerate(parts):
                print(f"  [{i}] {p[:80]!r}")


if __name__ == "__main__":
    main()
