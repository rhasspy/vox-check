#!/usr/bin/env python3
"""Voice recording and verification web server."""
import argparse
import asyncio
import csv
import itertools
import json
import logging
import math
import random
import signal
import subprocess
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
    jsonify,
    make_response,
    render_template,
    request,
    send_from_directory,
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
    parser.add_argument(
        "--debug", action="store_true", help="Print DEBUG messages to console"
    )

    return parser.parse_args()


_ARGS = parse_args()

if _ARGS.debug:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

_LOGGER.debug(_ARGS)

# -----------------------------------------------------------------------------


@dataclass
class Fragment:
    """Trimmed media fragment"""

    id: str
    begin: float = 0
    end: float = 0
    text: str = ""


@dataclass
class MediaItem:
    """Single audio item with text prompt"""

    id: str
    language: str
    audio_path: Path
    fragment: Fragment
    skipped: bool = False


# -----------------------------------------------------------------------------
# Load Media
# -----------------------------------------------------------------------------

media_dir = web_dir / "media"
data_dir = web_dir / "data"

# Load media items
media_by_id: typing.Dict[str, MediaItem] = {}

# language -> media id -> media item
media_by_lang: typing.Dict[str, typing.Dict[str, MediaItem]] = defaultdict(dict)

# language -> prompt id -> prompt text
prompts_by_lang: typing.Dict[str, typing.Dict[str, str]] = defaultdict(dict)

# user id -> { media id }
user_verified: typing.Dict[str, typing.Set[str]] = defaultdict(set)
user_skipped: typing.Dict[str, typing.Set[str]] = defaultdict(set)

# { media id }
all_verified: typing.Set[str] = set()


def load_items():
    """Load pre-recorded media (audio books, etc.)"""
    _LOGGER.debug("Loading media items from %s", media_dir)
    for lang_dir in media_dir.iterdir():
        if not lang_dir.is_dir():
            continue

        language = lang_dir.name

        # Load media
        for audio_path in itertools.chain(
            lang_dir.rglob("**/*.mp3"),
            lang_dir.glob("**/*.webm"),
            lang_dir.glob("**/*.wav"),
        ):
            map_path = audio_path.with_suffix(".json")
            if not map_path.is_file():
                _LOGGER.warning("Skipping %s (no sync map)", audio_path)
                continue

            media_id = str(audio_path.relative_to(media_dir))
            with open(map_path, "r") as map_file:
                sync_map = json.load(map_file)

            if not math.isfinite(sync_map["end"]):
                # Fix sync map
                _LOGGER.debug("Fixing sync map for %s", audio_path)
                sync_map["end"] = get_audio_duration(audio_path)
                with open(map_path, "w") as map_file:
                    json.dump(sync_map, map_file)

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
            media_by_lang[language][media_id] = item

        # Load prompts
        prompts_path = lang_dir / "prompts.csv"
        if prompts_path.is_file():
            with open(prompts_path, "r") as prompts_file:
                prompts_reader = csv.reader(prompts_file, delimiter="|")
                for row in prompts_reader:
                    prompt_id, prompt_text = row[0], row[1]
                    prompts_by_lang[language][prompt_id] = prompt_text


def get_audio_duration(audio_path: Path) -> float:
    """Get audio duration in seconds"""
    try:
        # Try mutagen
        import mutagen

        audio_file = mutagen.File(str(audio_path))
        assert audio_file
        return audio_file.info.length
    except Exception:
        try:
            # Try ffmpeg
            output_str = subprocess.check_output(
                [
                    "ffmpeg",
                    "-v",
                    "quiet",
                    "-stats",
                    "-i",
                    str(audio_path),
                    "-f",
                    "null",
                    "-",
                ],
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )

            # size=N/A time=00:00:02.63 bitrate=N/A speed= 454x -> 00:00:02.63
            time_str = output_str.strip().split()[1]
            time_str = time_str.split("=", maxsplit=1)[1]

            # 00:00:02.63 -> 00, 00, 02.63
            hours_str, mins_str, secs_str = time_str.split(":", maxsplit=2)

            # 00, 00, 02.63 -> 0 + 0 + 2.63
            seconds = (
                (60 * 60 * int(hours_str)) + (60 * int(mins_str)) + float(secs_str)
            )

            return seconds
        except Exception:
            _LOGGER.exception("get_audio_duration(%s)", audio_path)

    return 0.0


load_items()

# -----------------------------------------------------------------------------
# Setup Database
# -----------------------------------------------------------------------------

_DB_CONN = None

# user id -> { prompt id }
user_prompts = defaultdict(set)


async def setup_database():
    """Create sqlite3 database"""
    global _DB_CONN

    data_dir.mkdir(parents=True, exist_ok=True)

    _DB_CONN = await aiosqlite.connect(data_dir / "vox_check.db")
    await _DB_CONN.execute(
        "CREATE TABLE IF NOT EXISTS verify "
        + "(id INTEGER PRIMARY KEY AUTOINCREMENT, date_created TEXT, media_id TEXT, media_begin FLOAT, media_end FLOAT, media_text TEXT, user_id TEXT, is_skipped INTEGER);"
    )

    await _DB_CONN.execute(
        "CREATE TABLE IF NOT EXISTS record "
        + "(id INTEGER PRIMARY KEY AUTOINCREMENT, date_created TEXT, prompt_id TEXT, prompt_text TEXT, prompt_lang TEXT, user_id TEXT);"
    )

    # Update verification counts
    async with _DB_CONN.execute(
        "SELECT media_id, user_id, is_skipped FROM verify"
    ) as cursor:
        async for verify_row in cursor:
            media_id, user_id, is_skipped = verify_row[0], verify_row[1], verify_row[2]
            if is_skipped:
                user_skipped[user_id].add(media_id)
            else:
                user_verified[user_id].add(media_id)

            all_verified.add(media_id)

    # Update completed prompts
    async with _DB_CONN.execute("SELECT user_id, prompt_id FROM record") as cursor:
        async for record_row in cursor:
            user_id, prompt_id = record_row[0], record_row[1]
            user_prompts[user_id].add(prompt_id)


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
@app.route("/index.html")
async def api_index() -> str:
    """Main page"""
    user_id = request.cookies.get("user_id")
    if not user_id:
        user_id = "_".join(get_name())

    language = request.cookies.get("language", "en-us")

    return await render_template("index.html", user_id=user_id, language=language)


# -----------------------------------------------------------------------------

# Ensure verification counts are updated atomically
media_lock = asyncio.Lock()


@app.route("/verify", methods=["GET", "POST"])
async def api_verify() -> Response:
    """Verify a single media item"""
    if request.method == "POST":
        form = await request.form
        user_id = form["userId"]
        language = form["language"]
        skip = form["skip"].strip().lower() == "true"
        include_skipped = form.get("skipped", "false").strip().lower() == "true"
        shared_verifications = (
            request.args.get("shared", "true").strip().lower() == "true"
        )

        async with media_lock:
            fragment = Fragment(id=str(form["mediaId"]))
            assert fragment.id in media_by_id, f"Unknown media item {fragment.id}"
            if not skip:
                fragment.begin = float(form["begin"])
                fragment.end = float(form["end"])
                fragment.text = str(form["text"]).strip()

            # Create new verification
            assert _DB_CONN
            await _DB_CONN.execute(
                "INSERT INTO verify (date_created, media_id, media_begin, media_end, media_text, user_id, is_skipped) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc),
                    fragment.id,
                    fragment.begin,
                    fragment.end,
                    fragment.text,
                    user_id,
                    skip,
                ),
            )

            await _DB_CONN.commit()

            if skip:
                # Mark as a skipped item for user
                user_verified[user_id].add(fragment.id)
                user_skipped[user_id].add(fragment.id)
            else:
                # Mark as a verified item for user
                user_verified[user_id].add(fragment.id)
                user_skipped[user_id].discard(fragment.id)
    else:
        user_id = request.args["userId"]
        language = request.args.get("language", "en-us")
        include_skipped = request.args.get("skipped", "false").strip().lower() == "true"
        shared_verifications = (
            request.args.get("shared", "true").strip().lower() == "true"
        )

    # Choose the next item
    if shared_verifications:
        # Exclude items verified by anyone
        not_verified = media_by_lang[language].keys() - all_verified
    else:
        # Only exclude items verified by this user
        not_verified = media_by_lang[language].keys() - user_verified[user_id]

    if include_skipped:
        not_verified.update(
            set.intersection(set(media_by_lang[language].keys()), user_skipped[user_id])
        )

    item: typing.Optional[MediaItem] = None

    if not_verified:
        next_id = random.sample(not_verified, 1)[0]
        item = media_by_id[next_id]

        # Look for info from an already verified item
        assert _DB_CONN
        async with _DB_CONN.execute(
            "SELECT media_begin, media_end FROM verify WHERE media_id = ? LIMIT 1",
            (item.id,),
        ) as cursor:
            async for verify_row in cursor:
                item.fragment.begin, item.fragment.end = (
                    float(verify_row[0]),
                    float(verify_row[1]),
                )

    num_items = len(media_by_lang[language])
    num_verified = num_items - len(not_verified)
    verify_percent = int((num_verified / num_items) * 100) if num_items > 0 else 1

    response = await make_response(
        await render_template(
            "verify.html",
            user_id=user_id,
            fragment=item.fragment if item else None,
            language=language,
            verify_percent=verify_percent,
            num_verified=num_verified,
            num_items=num_items,
            skipped=include_skipped,
            shared=shared_verifications,
        )
    )

    # Save user id and language
    response.set_cookie(
        "user_id", user_id, max_age=_ONE_HUNDRED_YEARS, samesite="Strict"
    )

    response.set_cookie(
        "language", language, max_age=_ONE_HUNDRED_YEARS, samesite="Strict"
    )

    return response


# -----------------------------------------------------------------------------


@app.route("/record")
async def api_record() -> Response:
    """Record audio for a text prompt"""
    language = request.args.get("language", "en-us")
    user_id = request.args.get("userId")

    assert user_id, "No user id"
    all_prompts = prompts_by_lang.get(language)
    assert all_prompts, f"No prompts for language {language}"

    incomplete_prompts = set(all_prompts.keys()) - user_prompts[user_id]
    assert incomplete_prompts, "All prompts complete!"

    num_complete = len(all_prompts) - len(incomplete_prompts)
    num_items = len(all_prompts)
    complete_percent = num_complete / num_items if num_items > 0 else 1
    prompt_id = random.sample(incomplete_prompts, 1)[0]
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

    # Save user id and language
    response.set_cookie(
        "user_id", user_id, max_age=_ONE_HUNDRED_YEARS, samesite="Strict"
    )

    response.set_cookie(
        "language", language, max_age=_ONE_HUNDRED_YEARS, samesite="Strict"
    )

    return response


@app.route("/submit", methods=["POST"])
async def api_submit() -> Response:
    """Submit audio for a text prompt"""
    form = await request.form
    language = form["language"]
    user_id = form["userId"]
    prompt_id = form["promptId"]
    prompt_text = form["text"]
    duration = float(form["duration"])
    audio_format = form["format"]

    files = await request.files
    assert "audio" in files, "No audio"

    suffix = ".webm"
    if "wav" in audio_format:
        suffix = ".wav"

    # Save audio and transcription
    user_dir = media_dir / language / user_id
    user_dir.mkdir(parents=True, exist_ok=True)

    audio_path = (user_dir / prompt_id).with_suffix(suffix)
    with open(audio_path, "wb") as audio_file:
        files["audio"].save(audio_file)

    text_path = audio_path.with_suffix(".txt")
    text_path.write_text(prompt_text)

    if not math.isfinite(duration):
        # Fix duration
        _LOGGER.debug("Fixing sync map for %s", audio_path)
        duration = get_audio_duration(audio_path)

    sync_map = {"begin": 0, "end": duration, "raw_text": prompt_text}
    map_path = audio_path.with_suffix(".json")
    with open(map_path, "w") as map_file:
        json.dump(sync_map, map_file)

    # Create new recording
    assert _DB_CONN
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
    media_by_lang[language][media_id] = item

    # Get next prompt
    all_prompts = prompts_by_lang.get(language)
    assert all_prompts, f"No prompts for language {language}"

    incomplete_prompts = set(all_prompts.keys()) - user_prompts[user_id]
    assert incomplete_prompts, "All prompts complete!"

    num_complete = len(all_prompts) - len(incomplete_prompts)
    num_items = len(all_prompts)
    complete_percent = num_complete / num_items if num_items > 0 else 1
    prompt_id = random.sample(incomplete_prompts, 1)[0]
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


@app.route("/skip", methods=["POST"])
async def api_skip() -> Response:
    """Skip recording a media item"""
    form = await request.form
    language = form["language"]
    user_id = form["userId"]
    prompt_id = form["promptId"]

    user_prompts[user_id].add(prompt_id)

    # Get next prompt
    all_prompts = prompts_by_lang.get(language)
    assert all_prompts, f"No prompts for language {language}"

    incomplete_prompts = set(all_prompts.keys()) - user_prompts[user_id]
    assert incomplete_prompts, "All prompts complete!"

    num_complete = len(all_prompts) - len(incomplete_prompts)
    num_items = len(all_prompts)
    complete_percent = num_complete / num_items if num_items > 0 else 1
    prompt_id = random.sample(incomplete_prompts, 1)[0]
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


# -----------------------------------------------------------------------------


@app.route("/download")
async def api_download() -> Response:
    """Permalink for single media items"""
    user_id = request.args["userId"]
    language = request.args.get("language", "en-us")

    items = []
    user_dir = media_dir / language / user_id

    for file_path in user_dir.iterdir():
        if file_path.is_file() and (file_path.suffix in (".webm", ".wav")):
            text_path = file_path.with_suffix(".txt")
            if not text_path.is_file():
                continue

            mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
            text = text_path.read_text().strip()
            items.append(
                {
                    "id": file_path.stem,
                    "name": file_path.name,
                    "text": text,
                    "date": mtime,
                }
            )

    items = sorted(items, key=lambda i: i["date"], reverse=True)

    response = await make_response(
        await render_template(
            "download.html", user_id=user_id, language=language, items=items
        )
    )

    # Save user id and language
    response.set_cookie(
        "user_id", user_id, max_age=_ONE_HUNDRED_YEARS, samesite="Strict"
    )

    response.set_cookie(
        "language", language, max_age=_ONE_HUNDRED_YEARS, samesite="Strict"
    )

    return response


# -----------------------------------------------------------------------------


@app.route("/download-all")
async def api_download_all() -> Response:
    """Download all media items for a user in a .tar.gz"""
    user_id = request.args["userId"]
    language = request.args.get("language", "en-us")

    user_dir = media_dir / language / user_id
    assert user_dir.is_dir(), f"Invalid directory"

    async def generate():
        proc = await asyncio.create_subprocess_exec(
            "tar",
            "-cz",
            "--to-stdout",
            str(Path(language) / user_id),
            cwd=media_dir,
            stdout=asyncio.subprocess.PIPE,
        )

        stdout, _ = await proc.communicate()
        yield stdout

    response = app.response_class(generate(), mimetype="application/gzip")
    response.headers.set(
        "Content-Disposition", "attachment", filename=f"{language}_{user_id}.tar.gz"
    )

    return response


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
    if _DB_CONN:
        _LOOP.run_until_complete(_DB_CONN.close())
