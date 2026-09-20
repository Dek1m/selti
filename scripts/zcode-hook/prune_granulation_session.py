#!/usr/bin/env python3
"""Prune old hourly-granulation automation ticks from the ZCode CLI database.

The recurring ZCode automation "Ежечасная фоновая грануляция памяти selti"
runs inside one persistent session: every hourly tick appends messages and
the session grows forever. This script keeps only the last --keep ticks
(user messages that carry the automation prompt marker) and deletes every
older message/part/telemetry row of that session.

Deleted rows are dumped to a gzipped JSON archive before deletion, so a
manual restore is possible. The SQLite file itself is not vacuumed: freed
pages are reused by the client, which is enough to stop unbounded growth.

Usage:
    python prune_granulation_session.py            # keep 10 ticks
    python prune_granulation_session.py --dry-run  # show what would go
"""

import argparse
import gzip
import json
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path(r"C:/Users/User/.zcode/cli/db/db.sqlite")
DUMP_DIR = Path(r"C:/Users/User/.zcode/cli/prune-backups")
SESSION_TITLE = "Ежечасная фоновая грануляция памяти selti"
PROMPT_MARKER = "Фоновая ежечасная грануляция памяти selti"
DUMPS_TO_KEEP = 5

TELEMETRY_TABLES = ("turn_usage", "model_usage", "tool_usage")


def find_session(cur: sqlite3.Cursor, title: str) -> tuple[str, int] | None:
    cur.execute(
        "SELECT id FROM session WHERE title = ? ORDER BY time_updated DESC LIMIT 1",
        (title,),
    )
    row = cur.fetchone()
    if not row:
        return None
    session_id = row[0]
    cur.execute("SELECT COUNT(*) FROM message WHERE session_id = ?", (session_id,))
    return session_id, cur.fetchone()[0]


def find_ticks(cur: sqlite3.Cursor, session_id: str) -> list[tuple[int, int]]:
    """Return (sequence, time_created_ms) of user messages that are automation ticks."""
    cur.execute(
        "SELECT sequence, time_created FROM message "
        "WHERE session_id = ? AND json_extract(data, '$.role') = 'user' "
        "AND data LIKE ? ORDER BY sequence",
        (session_id, f"%{PROMPT_MARKER}%"),
    )
    return cur.fetchall()


def rotate_dumps() -> None:
    dumps = sorted(DUMP_DIR.glob("pruned-*.json.gz"))
    for old in dumps[:-DUMPS_TO_KEEP]:
        old.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=int, default=10, help="ticks to keep (default 10)")
    parser.add_argument("--dry-run", action="store_true", help="only report, change nothing")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--session-title", default=SESSION_TITLE)
    parser.add_argument("--no-dump", action="store_true", help="skip the deleted-rows archive")
    args = parser.parse_args()

    con = sqlite3.connect(args.db, timeout=15)
    con.execute("PRAGMA busy_timeout = 15000")
    cur = con.cursor()

    found = find_session(cur, args.session_title)
    if not found:
        print(f"Сессия с title={args.session_title!r} не найдена — чистить нечего.")
        con.close()
        return 0
    session_id, total_messages = found

    ticks = find_ticks(cur, session_id)
    if len(ticks) <= args.keep:
        print(
            f"Тиков {len(ticks)}, порог {args.keep} — чистка не нужна "
            f"(сообщений в сессии: {total_messages})."
        )
        con.close()
        return 0

    cutoff_seq = ticks[-args.keep][0]
    cutoff_time = ticks[-args.keep][1]

    cur.execute(
        "SELECT id, sequence, data FROM message WHERE session_id = ? AND sequence < ?",
        (session_id, cutoff_seq),
    )
    old_messages = cur.fetchall()
    msg_ids = [m[0] for m in old_messages]

    old_parts: list = []
    if msg_ids:
        placeholders = ",".join("?" * len(msg_ids))
        cur.execute(
            f"SELECT id, message_id, data FROM part WHERE session_id = ? "
            f"AND message_id IN ({placeholders})",
            (session_id, *msg_ids),
        )
        old_parts = cur.fetchall()

    old_telemetry = 0
    for table in TELEMETRY_TABLES:
        cur.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_id = ? AND started_at < ?",
            (session_id, cutoff_time),
        )
        old_telemetry += cur.fetchone()[0]

    kept = len(ticks) - args.keep
    print(
        f"Сессия {session_id}: сообщений {total_messages}, тиков {len(ticks)}. "
        f"К удалению: {kept} тиков ({len(old_messages)} сообщений, "
        f"{len(old_parts)} частей, {old_telemetry} записей телеметрии). "
        f"Останется {args.keep} тиков."
    )

    if args.dry_run:
        con.close()
        print("Dry-run — ничего не удалено.")
        return 0

    if not args.no_dump and (old_messages or old_parts):
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dump = DUMP_DIR / f"pruned-{stamp}.json.gz"
        payload = json.dumps(
            {
                "session_id": session_id,
                "cutoff_sequence": cutoff_seq,
                "messages": old_messages,
                "parts": old_parts,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        with gzip.open(dump, "wb") as f:
            f.write(payload)
        rotate_dumps()
        print(f"Архив удалённых строк: {dump}")

    if msg_ids:
        placeholders = ",".join("?" * len(msg_ids))
        cur.execute(
            f"DELETE FROM part WHERE session_id = ? AND message_id IN ({placeholders})",
            (session_id, *msg_ids),
        )
        cur.execute(
            "DELETE FROM message WHERE session_id = ? AND sequence < ?",
            (session_id, cutoff_seq),
        )
    for table in TELEMETRY_TABLES:
        cur.execute(
            f"DELETE FROM {table} WHERE session_id = ? AND started_at < ?",
            (session_id, cutoff_time),
        )
    con.commit()
    con.close()
    print("Чистка завершена.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
