#!/usr/bin/env python3
import csv
import sys
from collections import Counter
from pathlib import Path

media_dir = Path(sys.argv[1])
media_dir.mkdir(parents=True, exist_ok=True)

utt_counts = Counter()

reader = csv.reader(sys.stdin, delimiter="|")
for row in reader:
    utt_id, text = row[0], row[-1]
    utt_counts[utt_id] += 1
    utt_count = utt_counts[utt_id]

    text_path = media_dir / f"{utt_id}_{utt_count+1}.txt"

    text_path.write_text(text)
    print(text_path)
