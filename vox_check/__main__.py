#!/usr/bin/env python3
import argparse
import asyncio
import itertools
import json
import logging
import random
import signal
import typing
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite
import hypercorn
import quart_cors
from quart import (
    Quart,
    Response,
    exceptions,
    jsonify,
    make_response,
    render_template,
    request,
    send_file,
    send_from_directory,
    websocket,
)

from .utils import get_name

_LOGGER = logging.getLogger("vox_check")
_LOOP = asyncio.get_event_loop()

_ONE_HUNDRED_YEARS = 60 * 60 * 24 * 365 * 100

web_dir = Path(__file__).parent

# -----------------------------------------------------------------------------


def parse_args():
    """Parse command-line arguments"""
    parser = argparse.ArgumentParser("vox_check")
    parser.add_argument(
        "--host", type=str, help="Host for web server", default="0.0.0.0"
    )
    parser.add_argument("--port", type=int, help="Port for web server", default=8000)

    return parser.parse_args()


_ARGS = parse_args()

logging.basicConfig(level=logging.INFO)
_LOGGER.debug(_ARGS)

# -----------------------------------------------------------------------------


@dataclass
class Fragment:
    id: str
    begin: float
    end: float
    text: str


@dataclass
class MediaItem:
    id: str
    language: str
    audio_path: Path
    fragment: Fragment
    verify_count: int = 0


# -----------------------------------------------------------------------------
# Load Media
# -----------------------------------------------------------------------------

media_dir = web_dir / "media"
data_dir = web_dir / "data"

# Load media items
media_by_id = {}
media_by_lang = defaultdict(list)

prompts_by_lang = defaultdict(dict)

# Load pre-recorded media (audio books, etc.)
_LOGGER.debug("Loading media items from %s", media_dir)
for lang_dir in media_dir.iterdir():
    if not lang_dir.is_dir():
        continue

    language = lang_dir.name

    # Load media
    for audio_path in itertools.chain(
        lang_dir.rglob("**/*.mp3"), lang_dir.glob("**/*.webm")
    ):
        map_path = audio_path.with_suffix(".json")
        if not map_path.is_file():
            _LOGGER.warning("Skipping %s (no sync map)", audio_path)
            continue

        media_id = str(audio_path.relative_to(media_dir))
        with open(map_path, "r") as map_file:
            sync_map = json.load(map_file)

        fragment = Fragment(
            id=media_id,
            begin=sync_map["begin"],
            end=sync_map["end"],
            text=sync_map["raw_text"],
        )

        item = MediaItem(
            id=media_id, language=language, audio_path=audio_path, fragment=fragment
        )
        media_by_id[media_id] = item
        media_by_lang[language].append(item)

    # Load prompts
    prompts_path = lang_dir / "prompts.csv"
    if prompts_path.is_file():
        with open(prompts_path, "r") as prompts_file:
            for line in prompts_file:
                line = line.strip()
                if not line:
                    continue

                prompt_id, prompt_text = line.split("|", maxsplit=1)
                prompts_by_lang[language][prompt_id] = prompt_text

# -----------------------------------------------------------------------------
# Setup Database
# -----------------------------------------------------------------------------

_DB_CONN = None

user_prompts = defaultdict(set)


async def setup_database():
    global _DB_CONN

    data_dir.mkdir(parents=True, exist_ok=True)

    _DB_CONN = await aiosqlite.connect(data_dir / "vox_check.db")
    await _DB_CONN.execute(
        "CREATE TABLE IF NOT EXISTS verify "
        + "(id INTEGER PRIMARY KEY AUTOINCREMENT, date_created TEXT, media_id TEXT, media_begin FLOAT, media_end FLOAT, media_text TEXT, user_id TEXT);"
    )

    await _DB_CONN.execute(
        "CREATE TABLE IF NOT EXISTS record "
        + "(id INTEGER PRIMARY KEY AUTOINCREMENT, date_created TEXT, prompt_id TEXT, prompt_text TEXT, prompt_lang TEXT, user_id TEXT);"
    )

    # Update verification counts
    async with _DB_CONN.execute(
        "SELECT media_id, COUNT(*) FROM verify GROUP BY media_id"
    ) as cursor:
        async for row in cursor:
            item = media_by_id.get(row[0])
            if item:
                item.verify_count = row[1]

    # Update completed prompts
    async with _DB_CONN.execute("SELECT user_id, prompt_id FROM record") as cursor:
        async for row in cursor:
            user_prompts[row[0]].add(row[1])


_LOGGER.debug("Setting up databse")
_LOOP.run_until_complete(setup_database())

# -----------------------------------------------------------------------------
# Quart App
# -----------------------------------------------------------------------------

app = Quart("vox_check")
app.config["TEMPLATES_AUTO_RELOAD"] = True

app.secret_key = str(uuid4())
app = quart_cors.cors(app)

# -----------------------------------------------------------------------------
# Template Functions
# -----------------------------------------------------------------------------


@app.route("/")
async def api_index() -> Response:
    user_id = request.cookies.get("user_id")
    if not user_id:
        user_id = "_".join(get_name())

    return await render_template("index.html", user_id=user_id)


# -----------------------------------------------------------------------------

# Ensure verification counts are updated atomically
media_lock = asyncio.Lock()


@app.route("/verify", methods=["GET", "POST"])
async def api_verify() -> Response:
    if request.method == "POST":
        form = await request.form
        user_id = form["userId"]
        language = form["language"]
        fragment = Fragment(
            id=str(form["mediaId"]),
            begin=float(form["begin"]),
            end=float(form["end"]),
            text=str(form["text"]).strip(),
        )

        async with media_lock:
            item = media_by_id[fragment.id]
            if item.verify_count == 0:
                item.fragment = fragment

            item.verify_count += 1

            # Create new verification
            await _DB_CONN.execute(
                "INSERT INTO verify (date_created, media_id, media_begin, media_end, media_text) VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc),
                    fragment.id,
                    fragment.begin,
                    fragment.end,
                    fragment.text,
                ),
            )

            await _DB_CONN.commit()
    else:
        user_id = request.args["userId"]
        language = request.args.get("language", "en-us")

    # Choose the next item
    items = sorted(media_by_lang[language], key=lambda item: item.verify_count)
    assert items, f"No items for language {language}"
    item = items[0]

    num_verified = sum(1 for i in items if i.verify_count > 0)
    verify_percent = int((num_verified / len(items)) * 100)

    response = await make_response(
        await render_template(
            "verify.html",
            user_id=user_id,
            fragment=item.fragment,
            language=language,
            verify_percent=verify_percent,
            num_verified=num_verified,
            num_items=len(items),
        )
    )

    # Save user id
    response.set_cookie("user_id", user_id, max_age=_ONE_HUNDRED_YEARS)

    return response


# -----------------------------------------------------------------------------


@app.route("/record")
async def api_record() -> Response:
    language = request.args.get("language", "en-us")
    user_id = request.args.get("userId")

    assert user_id, "No user id"
    all_prompts = prompts_by_lang.get(language)
    assert all_prompts, f"No prompts for language {language}"

    incomplete_prompts = set(all_prompts.keys()) - user_prompts[user_id]
    assert incomplete_prompts, "All prompts complete!"

    num_complete = len(all_prompts) - len(incomplete_prompts)
    num_items = len(all_prompts)
    complete_percent = num_complete / num_items
    prompt_id = next(iter(incomplete_prompts))
    prompt_text = prompts_by_lang[language][prompt_id]

    response = await make_response(
        await render_template(
            "record.html",
            language=language,
            user_id=user_id,
            prompt_id=prompt_id,
            text=prompt_text,
            num_complete=num_complete,
            num_items=num_items,
            complete_percent=complete_percent,
        )
    )

    # Save user id
    response.set_cookie("user_id", user_id, max_age=_ONE_HUNDRED_YEARS)

    return response


@app.route("/submit", methods=["POST"])
async def api_submit() -> Response:
    form = await request.form
    language = form["language"]
    user_id = form["userId"]
    prompt_id = form["promptId"]
    prompt_text = form["text"]
    duration = float(form["duration"])

    files = await request.files
    assert "audio" in files, "No audio"

    # Save audio and transcription
    user_dir = media_dir / language / user_id
    user_dir.mkdir(parents=True, exist_ok=True)

    audio_path = (user_dir / prompt_id).with_suffix(".webm")
    with open(audio_path, "wb") as audio_file:
        files["audio"].save(audio_file)

    text_path = audio_path.with_suffix(".txt")
    text_path.write_text(prompt_text)

    sync_map = {"begin": 0, "end": duration, "raw_text": prompt_text}
    map_path = audio_path.with_suffix(".json")
    with open(map_path, "w") as map_file:
        json.dump(sync_map, map_file)

    # Create new recording
    await _DB_CONN.execute(
        "INSERT INTO record (date_created, prompt_id, prompt_text, prompt_lang, user_id) VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc), prompt_id, prompt_text, language, user_id),
    )

    await _DB_CONN.commit()
    user_prompts[user_id].add(prompt_id)

    # Add new media item to verify
    media_id = str(audio_path.relative_to(media_dir))
    fragment = Fragment(
        id=media_id,
        begin=sync_map["begin"],
        end=sync_map["end"],
        text=sync_map["raw_text"],
    )

    item = MediaItem(
        id=media_id, language=language, audio_path=audio_path, fragment=fragment
    )
    media_by_id[media_id] = item
    media_by_lang[language].append(item)

    # Get next prompt
    all_prompts = prompts_by_lang.get(language)
    assert all_prompts, f"No prompts for language {language}"

    incomplete_prompts = set(all_prompts.keys()) - user_prompts[user_id]
    assert incomplete_prompts, "All prompts complete!"

    num_complete = len(all_prompts) - len(incomplete_prompts)
    num_items = len(all_prompts)
    complete_percent = num_complete / num_items
    prompt_id = next(iter(incomplete_prompts))
    prompt_text = prompts_by_lang[language][prompt_id]

    return jsonify(
        {
            "promptId": prompt_id,
            "promptText": prompt_text,
            "numComplete": num_complete,
            "numItems": num_items,
            "completePercent": complete_percent,
        }
    )


# ---------------------------------------------------------------------
# Static Routes
# ---------------------------------------------------------------------

css_dir = web_dir / "css"
js_dir = web_dir / "js"
img_dir = web_dir / "img"
webfonts_dir = web_dir / "webfonts"


@app.route("/css/<path:filename>", methods=["GET"])
async def css(filename) -> Response:
    """CSS static endpoint."""
    return await send_from_directory(css_dir, filename)


@app.route("/js/<path:filename>", methods=["GET"])
async def js(filename) -> Response:
    """Javascript static endpoint."""
    return await send_from_directory(js_dir, filename)


@app.route("/img/<path:filename>", methods=["GET"])
async def img(filename) -> Response:
    """Image static endpoint."""
    return await send_from_directory(img_dir, filename)


@app.route("/webfonts/<path:filename>", methods=["GET"])
async def webfonts(filename) -> Response:
    """Webfonts static endpoint."""
    return await send_from_directory(webfonts_dir, filename)


@app.route("/media/<path:filename>", methods=["GET"])
async def media(filename) -> Response:
    """Media static endpoint."""
    return await send_from_directory(media_dir, filename)


@app.errorhandler(Exception)
async def handle_error(err) -> typing.Tuple[str, int]:
    """Return error as text."""
    _LOGGER.exception(err)
    return (f"{err.__class__.__name__}: {err}", 500)


# -----------------------------------------------------------------------------
# Run Web Server
# -----------------------------------------------------------------------------

hyp_config = hypercorn.config.Config()
hyp_config.bind = [f"{_ARGS.host}:{_ARGS.port}"]

# Create shutdown event for Hypercorn
shutdown_event = asyncio.Event()


def _signal_handler(*_: typing.Any) -> None:
    """Signal shutdown to Hypercorn"""
    shutdown_event.set()


_LOOP.add_signal_handler(signal.SIGTERM, _signal_handler)

try:
    # Need to type cast to satisfy mypy
    shutdown_trigger = typing.cast(
        typing.Callable[..., typing.Awaitable[None]], shutdown_event.wait
    )

    _LOOP.run_until_complete(
        hypercorn.asyncio.serve(app, hyp_config, shutdown_trigger=shutdown_trigger)
    )
except KeyboardInterrupt:
    _LOOP.call_soon(shutdown_event.set)
finally:
    _LOOP.run_until_complete(_DB_CONN.close())
