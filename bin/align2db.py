#!/usr/bin/env python3
import json
import sqlite3
import sys
from datetime import datetime, timezone

BUFFER_SEC = 0.1

db_path = sys.argv[1]
id_format = sys.argv[2]
user_id = sys.argv[3]
conn = sqlite3.connect(db_path)

with conn:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        alignment = json.loads(line)
        start = None
        end = None

        words = []

        for word in alignment["words"]:
            if word["word"] in {"<eps>"}:
                continue

            words.append(word["word"])

            for phone in word["phones"]:
                if phone["phone"] in {"SIL"}:
                    continue

                phone_start = float(phone["start"])
                phone_end = phone_start + float(phone["duration"])

                start = phone_start if start is None else min(start, phone_start)
                end = phone_end if end is None else max(end, phone_end)

        assert (start is not None) and (end is not None), line

        text = " ".join(words)
        media_id = id_format.format(id=alignment["id"])

        start = max(0, start - BUFFER_SEC)

        real_end = max(
            phone["start"] + phone["duration"]
            for word in alignment["words"]
            for phone in word["phones"]
        )
        end = min(real_end, end + BUFFER_SEC)

        conn.execute(
            "DELETE FROM verify WHERE user_id = ? AND media_id = ?", (user_id, media_id)
        )

        conn.execute(
            "INSERT INTO verify (date_created, media_id, media_begin, media_end, media_text, user_id, is_skipped) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc), media_id, start, end, text, user_id, False),
        )

        print(media_id, start, end, text)
