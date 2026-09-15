import asyncio
import base64
import binascii
import logging
import os
import re
import struct
import secrets
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from difflib import SequenceMatcher
from html import escape
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI
from pymongo import AsyncMongoClient, ASCENDING, ReturnDocument
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    TypeHandler,
    filters,
)
from telegram.helpers import create_deep_linked_url
try:
    from ingest import InternetArchiveIngestor
except Exception:
    InternetArchiveIngestor = None

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except Exception:
    TelegramClient = None
    StringSession = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("autofilter")


# ---------------------------
# Configuration
# ---------------------------

def env_first(*names, default=None):
    for name in names:
        value = os.getenv(name)
        if value is not None and value != "":
            return value
    return default


def as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def csv_list(value):
    if not value:
        return []
    return [x.strip() for x in str(value).split(",") if x.strip()]


def csv_ints(value):
    out = set()
    for x in csv_list(value):
        try:
            out.add(int(x))
        except ValueError:
            log.warning("Ignoring invalid integer in list: %s", x)
    return out


PLACEHOLDER_VALUES = {"blank", "none", "null", "n/a", "na", "-"}

def is_placeholder(value):
    return str(value).strip().lower() in PLACEHOLDER_VALUES

def require_env(*names):
    value = env_first(*names)
    if value is None or is_placeholder(value):
        raise RuntimeError(f"Missing/invalid required environment variable: {names[0]}")
    return value

def optional_int(value, default=None, name="value"):
    if value is None or str(value).strip() == "" or is_placeholder(value):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a number or left blank; got: {value!r}") from exc


def int_env(name, default, minimum=None, maximum=None):
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "" or is_placeholder(raw):
        value = default
    else:
        try:
            value = int(str(raw).strip())
        except ValueError as exc:
            raise RuntimeError(f"{name} must be an integer or left blank; got: {raw!r}") from exc
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


class Config:
    def __init__(self):
        self.bot_token = require_env("BOT_TOKEN")
        self.api_id = optional_int(require_env("API_ID"), name="API_ID")
        self.api_hash = require_env("API_HASH")
        self.session_string = env_first("SESSION_STRING", default="")

        self.bot_username = (env_first("BOT_USERNAME", default="") or "").lstrip("@")
        self.admin_ids = csv_ints(env_first("ADMIN_IDS", "ADMINS", default=""))

        self.mongo_uri = require_env("MONGO_URI", "DATABASE_URI")
        self.db_name = env_first("DB_NAME", "DATABASE_NAME", default="autofilter")

        self.database_channel = require_env("DATABASE_CHANNEL_ID", "BIN_CHANNEL")
        self.request_group = env_first("REQUEST_GROUP_ID", "REQUEST_GROUP_USERNAME")
        if not self.request_group:
            raise RuntimeError("Missing required environment variable: REQUEST_GROUP_ID or REQUEST_GROUP_USERNAME")
        self.request_group_username = (
            env_first("REQUEST_GROUP_USERNAME", default="") or ""
        ).lstrip("@")

        self.fsub_channels = csv_list(env_first("FSUB_CHANNELS", default=""))
        self.fsub_links = csv_list(env_first("FSUB_INVITE_LINKS", default=""))

        # Linkpays shortener.
        # Keep the token only in the deployment environment; never commit it.
        self.linkpays_api = env_first("LINKPAYS_API", default="")
        self.linkpays_base_url = env_first(
            "LINKPAYS_BASE_URL",
            default="https://linkpays.in/api",
        )

        # Both new and legacy switches are understood.
        legacy_verify = env_first("IS_VERIFY", default=None)
        self.require_fsub = as_bool(
            env_first("REQUIRE_FSUB", default="true"), True
        )
        self.require_shortlink = as_bool(
            env_first(
                "REQUIRE_SHORTLINK",
                default=legacy_verify if legacy_verify is not None else "true",
            ),
            True,
        )

        self.shortlink_ttl = int_env("SHORTLINK_TTL_SECONDS", 1800, 60, 604800)
        self.delete_after = int_env("DELETE_AFTER_SECONDS", 300, 30, 86400)
        self.index_on_start = as_bool(
            env_first("INDEX_ON_START", default="false"), False
        )
        self.auto_index = as_bool(
            env_first("AUTO_INDEX_NEW_POSTS", default="true"), True
        )
        self.page_size = int_env("SEARCH_PAGE_SIZE", 8, 1, 10)
        self.max_results = int_env("MAX_SEARCH_RESULTS", 48, 1, 100)
        self.send_all_limit = int_env("SEND_ALL_LIMIT", 100, 1, 100)

        # Authorized/public-domain automatic ingestion. Disabled by default
        # until the operator explicitly enables it in Render Environment.
        self.ia_ingest_enabled = as_bool(env_first("IA_INGEST_ENABLED", default="false"), False)
        self.ia_initial_backfill = as_bool(env_first("IA_INITIAL_BACKFILL", default="false"), False)
        self.ia_retry_skipped = as_bool(env_first("IA_RETRY_SKIPPED", default="false"), False)
        self.ia_scan_interval = int_env("IA_SCAN_INTERVAL_SECONDS", 1800, 300, 86400)
        self.ia_batch_size = int_env("IA_BATCH_SIZE", 5, 1, 100)
        self.ia_initial_pages = int_env("IA_INITIAL_PAGES", 5, 1, 1000)
        self.ia_scan_pages = int_env("IA_SCAN_PAGES", 10, 1, 1000)
        self.ia_initial_limit = int_env("IA_INITIAL_LIMIT", 100, 1, 5000)
        self.ia_page_size = int_env("IA_PAGE_SIZE", 50, 1, 100)
        self.ia_max_file_mb = int_env("IA_MAX_FILE_MB", 1800, 20, 1900)
        self.ia_http_timeout = int_env("IA_HTTP_TIMEOUT", 120, 30, 600)
        self.ia_query = env_first(
            "IA_QUERY",
            default="mediatype:movies",
        )
        self.developer_username = (env_first("DEVELOPER_USERNAME", default="thevisionaryoffc") or "").lstrip("@")
        self.developer_name = env_first("DEVELOPER_NAME", default="Cinema HUB OG Developer") or "Cinema HUB OG Developer"
        self.greeting_timezone = env_first("GREETING_TIMEZONE", default="Asia/Kolkata") or "Asia/Kolkata"
        self.start_image_urls = csv_list(env_first("START_IMAGE_URLS", default=""))
        if not self.start_image_urls:
            self.start_image_urls = [
                "https://images.unsplash.com/photo-1501785888041-af3ef285b470?auto=format&fit=crop&w=1200&q=85",
                "https://images.unsplash.com/photo-1470770841072-f978cf4d019e?auto=format&fit=crop&w=1200&q=85",
                "https://images.unsplash.com/photo-1441974231531-c6227db76b6e?auto=format&fit=crop&w=1200&q=85",
                "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&w=1200&q=85",
            ]
        self.telegram_read_timeout = int_env("TELEGRAM_READ_TIMEOUT", 45, 10, 180)
        self.telegram_connect_timeout = int_env("TELEGRAM_CONNECT_TIMEOUT", 30, 5, 120)
        self.telegram_write_timeout = int_env("TELEGRAM_WRITE_TIMEOUT", 45, 10, 180)
        self.telegram_pool_timeout = int_env("TELEGRAM_POOL_TIMEOUT", 30, 5, 120)
        self.startup_retry_delay = int_env("STARTUP_RETRY_DELAY", 5, 2, 60)
        # LOG_CHAT_ID is optional. Treat common dashboard placeholders such as
        # "Blank" as empty instead of crashing the whole bot at startup.
        self.log_chat_id = optional_int(
            env_first("LOG_CHAT_ID"), default=None, name="LOG_CHAT_ID"
        )

        self.expiry_notice = env_first(
            "EXPIRY_NOTICE",
            default=(
                "⚠️ ᴛʜɪꜱ ᴍᴏᴠɪᴇ ꜰɪʟᴇ/ᴠɪᴅᴇᴏ ᴡɪʟʟ ʙᴇ ᴅᴇʟᴇᴛᴇᴅ ɪɴ 5 ᴍɪɴᴜᴛᴇꜱ\n\n"
                "ᴘʟᴇᴀꜱᴇ ꜰᴏʀᴡᴀʀᴅ ᴛʜɪꜱ ꜰɪʟᴇ ᴛᴏ ꜱᴏᴍᴇᴡʜᴇʀᴇ ᴇʟꜱᴇ & "
                "ꜱᴛᴀʀᴛ ᴅᴏᴡɴʟᴏᴀᴅɪɴɢ ᴛʜᴇʀᴇ"
            ),
        ).replace("\\n", "\n")

        self._resolve_static_fsub()
        self._resolve_chat_ids()

    def _resolve_static_fsub(self):
        # F-Sub is optional. Do not make a malformed/old F-Sub setting prevent
        # the entire bot from starting. Actual chat accessibility and public
        # usernames are resolved against Telegram during Runtime validation.
        if self.fsub_links and not self.fsub_channels:
            log.warning(
                "FSUB_INVITE_LINKS is set but FSUB_CHANNELS is empty; "
                "disabling F-Sub until the configuration is corrected."
            )
            self.fsub_links = []
            self.require_fsub = False
            return
        if len(self.fsub_links) > len(self.fsub_channels):
            log.warning(
                "Ignoring %s extra FSUB_INVITE_LINKS entry/entries.",
                len(self.fsub_links) - len(self.fsub_channels),
            )
            self.fsub_links = self.fsub_links[: len(self.fsub_channels)]
        if not self.fsub_channels:
            self.require_fsub = False

    def _resolve_chat_ids(self):
        def normalize_chat(value):
            value = str(value).strip()
            if value.lstrip("-").isdigit():
                return int(value)
            return value
        self.database_channel = normalize_chat(self.database_channel)
        self.request_group = normalize_chat(self.request_group)
        self.fsub_channels = [normalize_chat(x) for x in self.fsub_channels]


# ---------------------------
# Text / metadata
# ---------------------------

QUALITY_RE = re.compile(
    r"(?i)\b(4k|2160p|1440p|2k|1080p|720p|480p|360p)\b"
)
SEASON_RE = re.compile(r"(?i)\b(?:season|s)\s*(\d{1,2})\b")
LANG_RE = re.compile(
    r"(?i)\b(hindi|english|tamil|telugu|malayalam|kannada|punjabi|bengali|dual audio|multi audio)\b"
)
SIZE_RE = re.compile(r"(?i)\b(\d+(?:\.\d+)?)\s*(gb|mb)\b")
QUALITY_RANK = {
    "4K": 0,
    "2160P": 0,
    "1440P": 1,
    "2K": 1,
    "1080P": 2,
    "720P": 3,
    "480P": 4,
    "360P": 5,
}


def normalize(text):
    text = (text or "").lower()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def parse_metadata(text):
    text = text or ""
    q = QUALITY_RE.search(text)
    s = SEASON_RE.search(text)
    l = LANG_RE.search(text)
    z = SIZE_RE.search(text)
    quality = q.group(1).upper() if q else None
    return {
        "quality": quality,
        "quality_rank": QUALITY_RANK.get(quality, 99),
        "season": f"Season {s.group(1)}" if s else None,
        "language": l.group(1).title() if l else None,
        "size": z.group(0).upper() if z else None,
    }


def media_filename(msg):
    for attr in ("video", "document", "audio"):
        item = getattr(msg, attr, None)
        if item and getattr(item, "file_name", None):
            return item.file_name
    file_obj = getattr(msg, "file", None)
    return getattr(file_obj, "name", None) if file_obj else None


def media_file_size(msg):
    """Return the Telegram media size in bytes when Telegram exposes it."""
    for attr in ("video", "document", "audio"):
        item = getattr(msg, attr, None)
        if item:
            size = getattr(item, "file_size", None)
            if size:
                try:
                    return int(size)
                except (TypeError, ValueError):
                    pass
    file_obj = getattr(msg, "file", None)
    size = getattr(file_obj, "size", None) if file_obj else None
    if size:
        try:
            return int(size)
        except (TypeError, ValueError):
            pass
    return None


def format_file_size(size_bytes):
    """Format a Telegram byte size exactly like the reference result list."""
    if size_bytes is None:
        return None
    try:
        size_bytes = float(size_bytes)
    except (TypeError, ValueError):
        return None
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024 ** 3):.2f} GB"
    return f"{size_bytes / (1024 ** 2):.2f} MB"


def clean_result_name(value):
    """Make filenames readable without changing their actual identity."""
    text = str(value or "").strip()
    # The reference UI uses spaces instead of filename underscores.
    text = text.replace("_", " ")
    # Remove a leading source-tag such as [@TheEmpireBay] / [@FridayUniverse].
    text = re.sub(r"^\s*\[\s*@[^\]]+\]\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def new_token(nbytes=24):
    return secrets.token_urlsafe(nbytes).replace("-", "_").replace("=", "")


# ---------------------------
# MongoDB
# ---------------------------

class Database:
    def __init__(self, uri, name):
        self.client = AsyncMongoClient(
            uri, serverSelectionTimeoutMS=15000
        )
        self.db = self.client[name]
        self.movies = self.db.movies
        self.tokens = self.db.tokens
        self.users = self.db.users
        self.searches = self.db.searches
        self.requests = self.db.requests
        self.fsub_gates = self.db.fsub_gates
        self.batches = self.db.batches
        self.search_counts = self.db.search_counts
        self.ingest_jobs = self.db.ingest_jobs

    async def init(self):
        last = None
        for attempt in range(1, 7):
            try:
                await self.client.admin.command("ping")
                await self._migrate_users()
                await self.movies.create_index(
                    [("chat_id", ASCENDING), ("message_id", ASCENDING)],
                    unique=True,
                )
                await self.movies.create_index([("normalized", ASCENDING)])
                await self.movies.create_index([("quality", ASCENDING)])
                await self.movies.create_index([("language", ASCENDING)])
                await self.movies.create_index([("season", ASCENDING)])
                await self.tokens.create_index("token", unique=True)
                await self.tokens.create_index("expires_at", expireAfterSeconds=0)
                await self.users.create_index("user_id", unique=True, sparse=True)
                await self.searches.create_index("expires_at", expireAfterSeconds=0)
                await self.requests.create_index("expires_at", expireAfterSeconds=0)
                await self.fsub_gates.create_index("gate_id", unique=True)
                await self.fsub_gates.create_index("expires_at", expireAfterSeconds=0)
                await self.batches.create_index("batch_id", unique=True)
                await self.batches.create_index("expires_at", expireAfterSeconds=0)
                await self.search_counts.create_index("normalized", unique=True)
                await self.search_counts.create_index([("count", ASCENDING)])
                await self.ingest_jobs.create_index([("source", ASCENDING), ("identifier", ASCENDING)], unique=True)
                await self.ingest_jobs.create_index([("status", ASCENDING), ("updated_at", ASCENDING)])
                return
            except Exception as exc:
                last = exc
                log.exception("MongoDB initialization attempt %s/6 failed", attempt)
                if attempt < 6:
                    await asyncio.sleep(min(2 ** (attempt - 1), 20))
        raise RuntimeError(f"MongoDB connection failed: {last}")

    async def _migrate_users(self):
        # Older AutoFilter databases commonly used `id` instead of `user_id`,
        # or stored user_id as a string. Normalize that schema before creating
        # the unique index so deployment never dies on duplicate/null user_id.
        # index_information() is a direct async method returning a mapping;
        # using it here avoids version-specific AsyncCommandCursor/to_list
        # handling during startup.
        index_info = await self.users.index_information()
        for index_name, info in list(index_info.items()):
            if index_name == "_id_":
                continue
            try:
                key = list(info.get("key", []))
            except Exception:
                key = []
            if key == [("user_id", 1)]:
                try:
                    await self.users.drop_index(index_name)
                except Exception:
                    log.exception("Could not drop legacy user_id index %s", index_name)

        docs = await self.users.find(
            {}, {"_id": 1, "id": 1, "user_id": 1}
        ).to_list(length=None)
        seen = {}
        for doc in docs:
            raw = doc.get("user_id")
            if raw is None or str(raw).strip() == "" or is_placeholder(raw):
                raw = doc.get("id")
            uid = None
            if raw is not None:
                try:
                    uid = int(raw)
                except (TypeError, ValueError):
                    uid = None

            # If a malformed user_id exists but the legacy numeric `id` is
            # usable, recover from the legacy field instead of discarding the
            # user record.
            if uid is None and raw != doc.get("id"):
                legacy_id = doc.get("id")
                try:
                    uid = int(legacy_id) if legacy_id is not None else None
                except (TypeError, ValueError):
                    uid = None

            if uid is None:
                await self.users.update_one(
                    {"_id": doc["_id"]}, {"$unset": {"user_id": ""}}
                )
                continue

            if uid in seen:
                # Preserve one canonical record per Telegram user. If an old
                # migration created duplicates, keep the first document.
                await self.users.delete_one({"_id": doc["_id"]})
                continue

            seen[uid] = doc["_id"]
            await self.users.update_one(
                {"_id": doc["_id"]}, {"$set": {"user_id": uid}}
            )
    async def close(self):
        await self.client.close()

    async def upsert_movie(self, doc):
        await self.movies.update_one(
            {"chat_id": doc["chat_id"], "message_id": doc["message_id"]},
            {"$set": doc},
            upsert=True,
        )

    async def find_movies(self, query, limit, filters=None):
        filters = filters or {}
        words = [w for w in normalize(query).split() if len(w) >= 2]
        if not words:
            return []
        pattern = ".*".join(re.escape(w) for w in words)
        criteria = {"normalized": {"$regex": pattern, "$options": "i"}}
        for key in ("quality", "language", "season"):
            if filters.get(key):
                criteria[key] = filters[key]
        cursor = (
            self.movies.find(criteria)
            .sort([("quality_rank", ASCENDING), ("title", ASCENDING)])
            .limit(limit)
        )
        return await cursor.to_list(length=limit)

    async def suggestions(self, query, limit=5):
        q = normalize(query)
        if not q:
            return []
        docs = await self.movies.find(
            {}, {"title": 1, "normalized": 1}
        ).limit(5000).to_list(5000)
        scored = []
        for d in docs:
            title = d.get("title", "")
            norm = d.get("normalized", "")
            score = max(
                SequenceMatcher(None, q, normalize(title)).ratio(),
                SequenceMatcher(None, q, norm).ratio(),
            )
            if score >= 0.55:
                scored.append((score, title))
        scored.sort(reverse=True)
        seen, out = set(), []
        for _, title in scored:
            key = title.lower()
            if key not in seen:
                seen.add(key)
                out.append(title)
                if len(out) >= limit:
                    break
        return out

    async def record_user(self, user_id, **extra):
        user_id = int(user_id)
        now = datetime.now(timezone.utc)
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {"last_seen": now, **extra},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    async def record_request(self, user_id, query):
        user_id = int(user_id) if user_id is not None else 0
        now = datetime.now(timezone.utc)
        await self.requests.insert_one({
            "user_id": user_id,
            "query": query,
            "created_at": now,
            "expires_at": now + timedelta(days=30),
        })
        normalized_query = normalize(query)
        if normalized_query:
            await self.search_counts.update_one(
                {"normalized": normalized_query},
                {"$set": {"query": query.strip()[:200], "updated_at": now}, "$inc": {"count": 1}, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )

    async def top_searches(self, limit=10):
        cursor = self.search_counts.find({}, {"_id": 0, "query": 1, "count": 1}).sort([("count", -1), ("query", 1)]).limit(limit)
        return await cursor.to_list(length=limit)

    async def create_search(self, user_id, query, filters, expires_at):
        sid = new_token(10)
        await self.searches.insert_one(
            {
                "search_id": sid,
                "user_id": user_id,
                "query": query,
                "filters": filters or {},
                "created_at": datetime.now(timezone.utc),
                "expires_at": expires_at,
            }
        )
        return sid

    async def get_search(self, sid, user_id):
        return await self.searches.find_one(
            {"search_id": sid, "user_id": user_id}
        )

    async def update_search_filter(self, sid, user_id, filters):
        await self.searches.update_one(
            {"search_id": sid, "user_id": user_id},
            {"$set": {"filters": filters}},
        )

    async def create_fsub_gate(self, gate_id, user_id, movie_ids, expires_at, mode="standard", title=None):
        await self.fsub_gates.insert_one(
            {
                "gate_id": gate_id,
                "user_id": user_id,
                "movie_ids": movie_ids,
                "mode": mode,
                "title": title or "",
                "created_at": datetime.now(timezone.utc),
                "expires_at": expires_at,
                "used": False,
            }
        )

    async def consume_fsub_gate(self, gate_id, user_id):
        return await self.fsub_gates.find_one_and_update(
            {
                "gate_id": gate_id,
                "user_id": user_id,
                "used": False,
                "expires_at": {"$gt": datetime.now(timezone.utc)},
            },
            {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )

    async def create_token(self, token, user_id, movie_ids, expires_at, mode="standard", title=None):
        await self.tokens.insert_one(
            {
                "token": token,
                "user_id": user_id,
                "movie_ids": movie_ids,
                "mode": mode,
                "title": title or "",
                "created_at": datetime.now(timezone.utc),
                "expires_at": expires_at,
                "used": False,
            }
        )

    async def consume_token(self, token, user_id):
        return await self.tokens.find_one_and_update(
            {
                "token": token,
                "user_id": user_id,
                "used": False,
                "expires_at": {"$gt": datetime.now(timezone.utc)},
            },
            {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )

    async def create_batch(self, batch_id, user_id, movie_ids, expires_at):
        await self.batches.insert_one(
            {
                "batch_id": batch_id,
                "user_id": user_id,
                "movie_ids": movie_ids,
                "created_at": datetime.now(timezone.utc),
                "expires_at": expires_at,
                "used": False,
            }
        )

    async def consume_batch(self, batch_id, user_id):
        return await self.batches.find_one_and_update(
            {
                "batch_id": batch_id,
                "user_id": user_id,
                "used": False,
                "expires_at": {"$gt": datetime.now(timezone.utc)},
            },
            {"$set": {"used": True, "used_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )

    async def stats(self):
        return {
            "movies": await self.movies.count_documents({}),
            "users": await self.users.count_documents({}),
            "tokens": await self.tokens.count_documents({}),
            "requests": await self.requests.count_documents({}),
            "ingest_jobs": await self.ingest_jobs.count_documents({}),
            "ingest_uploaded": await self.ingest_jobs.count_documents({"status": "uploaded"}),
        }


# ---------------------------
# Linkpays
# ---------------------------


async def shorten_linkpays(cfg, destination):
    if not cfg.linkpays_api:
        raise RuntimeError("LINKPAYS_API is missing. Add your Linkpays API token.")
    timeout = httpx.Timeout(30.0, connect=15.0)
    last = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.get(
                    cfg.linkpays_base_url,
                    params={
                        "api": cfg.linkpays_api,
                        "url": destination,
                    },
                )
                response.raise_for_status()
                data = response.json()

            if data.get("status") != "success":
                raise RuntimeError(data.get("message") or str(data))

            result = data.get("shortenedUrl")
            if not result:
                raise RuntimeError("Linkpays did not return shortenedUrl")
            return result
        except Exception as exc:
            last = exc
            if attempt < 3:
                await asyncio.sleep(attempt * 2)

    raise RuntimeError(f"Linkpays request failed after retries: {last}")


# Force Subscribe
# ---------------------------

async def missing_channels(bot, user_id, channels):
    missing = []
    for channel in channels:
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in {"left", "kicked"}:
                missing.append(channel)
        except TelegramError as exc:
            log.warning("FSub check failed for %s: %s", channel, exc)
            missing.append(channel)
    return missing


def fsub_keyboard(cfg, gate_id):
    rows = []
    for i, link in enumerate(cfg.fsub_links, 1):
        rows.append(
            [InlineKeyboardButton(f"📢 Join Channel {i}", url=link)]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "✅ Verify Subscription", callback_data=f"fsub:{gate_id}"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


# ---------------------------
# Presentation
# ---------------------------

def result_text(movie):
    title = escape(str(movie.get("title") or "Movie"))
    meta = [
        movie.get("quality"),
        movie.get("language"),
        movie.get("season"),
        movie.get("size"),
    ]
    meta = [escape(str(x)) for x in meta if x]
    if meta:
        return f"🎬 <b>{title}</b>\n\n" + " • ".join(meta)
    return f"🎬 <b>{title}</b>"


def result_keyboard(movie, bot_username):
    url = create_deep_linked_url(bot_username, f"file_{movie['_id']}")
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎬 Get Movie", url=url)]]
    )


def filter_keyboard(results, search_id):
    groups = [("Quality", "quality"), ("Language", "language"), ("Season", "season")]
    rows = []
    for label, key in groups:
        values = sorted({str(m.get(key)) for m in results if m.get(key)})
        if values:
            row = []
            for idx, value in enumerate(values[:6]):
                row.append(
                    InlineKeyboardButton(
                        value[:20],
                        callback_data=f"filter:{key}:{search_id}:{idx}",
                    )
                )
            rows.append(row)
    return InlineKeyboardMarkup(rows) if rows else None


# ---------------------------
# Premium presentation
# ---------------------------

WELCOME_HELP = (
    "🔎 <b>HOW TO SEARCH</b>\n\n"
    "Just type the <b>movie or series name</b> in the Request Group.\n\n"
    "✅ Use the correct spelling.\n"
    "✅ Type only the title.\n"
    "❌ Don't add emojis or long descriptions.\n"
    "❌ Don't write full season/episode details in the title.\n\n"
    "The bot will find the available matches automatically."
)
ABOUT_TEXT = (
    "📖 <b>ABOUT CINEMA HUB OG</b>\n\n"
    "Cinema HUB OG was created with one simple goal: to make movie and series discovery faster, cleaner and easier for everyone who needs it.\n\n"
    "This project is built with passion around automation, search and a smooth user experience, with constant improvements to verification and result delivery.\n\n"
    "👑 <b>Developer:</b> {developer_name}\n"
    "💙 <b>Contact:</b> @{developer_username}\n\n"
    "Thank you for supporting the project and helping it grow."
)
UPGRADE_TEXT = (
    "💎 <b>SUPPORT THE PROJECT</b>\n\n"
    "You can use the basic bot without paying anything.\n\n"
    "If you enjoy the experience and want to help me keep building more powerful, creative and premium Telegram automation, you can support the developer.\n\n"
    "Your support helps with development, hosting, maintenance and new features.\n\n"
    "✨ <b>Want to support the project?</b>\n"
    "👉 @thevisionaryoffc\n\n"
    "Thank you for believing in the work. ❤️"
)

def time_greeting(tz_name="Asia/Kolkata"):
    try:
        hour=datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).hour
    except Exception:
        hour=datetime.now().astimezone().hour
    if 5 <= hour < 12: return "GOOD MORNING 🌅"
    if 12 <= hour < 17: return "GOOD AFTERNOON ☀️"
    if 17 <= hour < 21: return "GOOD EVENING 🌆"
    return "GOOD NIGHT 🌙"

def main_menu_keyboard(cfg):
    rows=[]
    if cfg.bot_username:
        rows.append([InlineKeyboardButton("🔰 ADD ME TO YOUR GROUP 🔰", url=f"https://t.me/{cfg.bot_username}?startgroup=true")])
    rows += [
        [InlineKeyboardButton("HELP 📢", callback_data="menu:help"), InlineKeyboardButton("ABOUT 📖", callback_data="menu:about")],
        [InlineKeyboardButton("TOP SEARCHING ⭐", callback_data="menu:top"), InlineKeyboardButton("UPGRADE 💎", callback_data="menu:upgrade")],
    ]
    return InlineKeyboardMarkup(rows)

def request_group_url(cfg):
    return f"https://t.me/{cfg.request_group_username}" if cfg.request_group_username else None

def request_group_prompt(user):
    name=escape(user.first_name or "Friend")
    return (
        f"👑 <b>HEY {name} ✨</b>\n\n"
        "<i>You can search for movies only on our Movie Group. You are not allowed to search for movies on Direct Bot. Please join our movie group by clicking the <b>REQUEST HERE</b> button given below and search your favorite movie there 👇</i>\n\n"
        "<blockquote>आप केवल हमारे <b>Movie Group</b> पर ही Movie Search कर सकते हो। आपको Direct Bot पर Movie Search करने की Permission नहीं है। कृपया नीचे दिए गए <b>REQUEST HERE</b> वाले Button पर Click करके हमारे Movie Group को Join करें और वहाँ पर अपनी मनपसंद Movie Search करें।</blockquote>"
    )

def premium_fsub_text():
    return (
        "⚠️ <b>ACCESS DENIED ⚠️</b>\n\n"
        "<b>YOU NEED TO JOIN OUR UPDATE CHANNEL TO ACCESS THIS BOT.</b>\n\n"
        "👇 <b>STEPS TO VERIFY</b>\n\n"
        "1️⃣ Click the <b>JOIN UPDATE CHANNEL</b> button.\n"
        "2️⃣ Join the channel.\n"
        "3️⃣ Click <b>TRY AGAIN</b>.\n\n"
        "✨ <b>THANK YOU FOR YOUR SUPPORT!</b>"
    )

def premium_fsub_keyboard(cfg, gate_id):
    rows=[[InlineKeyboardButton(f"📢 JOIN UPDATE CHANNEL {i}",url=link)] for i,link in enumerate(cfg.fsub_links,1)]
    rows.append([InlineKeyboardButton("🔄 TRY AGAIN 🔄",callback_data=f"fsub:{gate_id}")])
    return InlineKeyboardMarkup(rows)

def search_button(movie,cfg,number):
    # Kept for compatibility with the existing codebase. Search results are
    # now rendered as full clickable text links instead of inline buttons.
    title=str(movie.get("title") or "Untitled")
    quality=str(movie.get("quality") or "Quality ?")
    language=str(movie.get("language") or "Language ?")
    size=str(movie.get("size") or "Size ?")
    label=re.sub(r"\s+"," ",f"{number}. {size} • {quality} • {language} • {title}").strip()
    if len(label)>58: label=label[:55].rstrip()+"…"
    return InlineKeyboardButton(label,url=create_deep_linked_url(cfg.bot_username,f"file_{movie['_id']}"))

def search_result_link(movie,cfg,number):
    """Return one clean clickable result line matching the reference UI."""
    size = str(movie.get("size") or "").strip()
    if not size:
        size = format_file_size(movie.get("file_size")) or ""

    filename = clean_result_name(movie.get("filename") or movie.get("title") or "Untitled")
    label = f"{number}. {size} | {filename}" if size else f"{number}. {filename}"
    url = create_deep_linked_url(cfg.bot_username, f"file_{movie['_id']}")
    return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'

def search_header(query,results,page,pages,filters_,page_size=8):
    active=[f"{k.title()}: {escape(str(v))}" for k,v in filters_.items() if k in {"quality","language","season"} and v]
    shown_start=page*page_size+1 if results else 0
    shown_end=min((page+1)*page_size,len(results))
    return ("👑 <b>CINEMA HUB OG</b>\n<i>PREMIUM MOVIE SEARCH</i>\n\n"
            f"🔎 <b>RESULTS FOR:</b> <u>{escape(query)}</u>\n"
            f"✅ <b>{len(results)} result(s)</b> • <b>{shown_start}-{shown_end}</b> shown\n"
            + ("🎛 <b>"+" • ".join(active)+"</b>\n" if active else "")
            + "\n👇 <b>SELECT A FILE OR USE FILTERS</b>")

def search_result_text(cfg,results,page,page_size):
    lines=[]
    start=page*page_size
    current=results[start:start+page_size]
    for number,movie in enumerate(current,start=start+1):
        lines.append(search_result_link(movie,cfg,number))
    # A blank line between every result keeps long filenames visually separated.
    return "\n\n".join(lines)

def search_markup(cfg,results,search_id,page,pages,batch_url):
    rows=[]
    rows.append([InlineKeyboardButton("📦 SEND ALL FILES 📦",url=batch_url)])
    rows.append([
        InlineKeyboardButton("🎞 QUALITY",callback_data=f"filter_menu:quality:{search_id}"),
        InlineKeyboardButton("🌐 LANGUAGE",callback_data=f"filter_menu:language:{search_id}"),
        InlineKeyboardButton("📺 SEASON",callback_data=f"filter_menu:season:{search_id}"),
    ])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton("‹ PREV",callback_data=f"page:{page-1}:{search_id}"))
    nav.append(InlineKeyboardButton(f"{page+1} / {pages}",callback_data="noop"))
    if page<pages-1: nav.append(InlineKeyboardButton("NEXT ›",callback_data=f"page:{page+1}:{search_id}"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)

async def create_batch_for_page(context,user_id,results,limit):
    db=context.application.bot_data["db"]
    batch_id=new_token(8)
    await db.create_batch(batch_id,user_id,[str(m["_id"]) for m in results[:limit]],datetime.now(timezone.utc)+timedelta(minutes=30))
    return batch_id

async def render_search_message(message,context,results,search_id,page,query,user_id):
    cfg=context.application.bot_data["cfg"]
    db=context.application.bot_data["db"]
    doc=await db.get_search(search_id,user_id)
    filters_=(doc or {}).get("filters") or {}
    size=cfg.page_size
    pages=max(1,(len(results)+size-1)//size)
    page=max(0,min(page,pages-1))
    batch_id=await create_batch_for_page(context,user_id,results,cfg.send_all_limit)
    markup=search_markup(cfg,results,search_id,page,pages,create_deep_linked_url(cfg.bot_username,f"all_{batch_id}"))
    text=search_header(query,results,page,pages,filters_,cfg.page_size)
    text += "\n\n" + search_result_text(cfg,results,page,cfg.page_size)
    try:
        await message.edit_text(text,reply_markup=markup,parse_mode="HTML")
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            await message.reply_text(text,reply_markup=markup,parse_mode="HTML")

def menu_text(kind, cfg=None):
    if kind=="help": return WELCOME_HELP
    if kind=="about":
        cfg = cfg or type("Cfg", (), {"developer_name": "Cinema HUB OG Developer", "developer_username": "thevisionaryoffc"})()
        return ABOUT_TEXT.format(developer_name=escape(cfg.developer_name), developer_username=escape(cfg.developer_username))
    if kind=="upgrade": return UPGRADE_TEXT
    return None

async def _edit_menu_message(query, text, markup):
    message = query.message
    try:
        if getattr(message, "photo", None):
            await message.edit_caption(caption=text, reply_markup=markup, parse_mode="HTML")
        else:
            await message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def menu_callback(update,context):
    q=update.callback_query
    await q.answer()
    action=q.data.split(":",1)[1]
    if action=="main":
        cfg=context.application.bot_data["cfg"]
        bot_username=cfg.bot_username or (getattr(context.bot,"username","") or "")
        link=f"https://t.me/{bot_username}" if bot_username else "#"
        text=("🚩 <b>JAI SHRI RAM 🚩</b>\n\n"
              f"👋 <b>HEY {escape(q.from_user.first_name or 'Friend')}</b>, {time_greeting(cfg.greeting_timezone)}\n\n"
              f"🤖 I AM <a href=\"{link}\">Cinema HUB OG</a>,\n"
              "<b>THE MOST POWERFUL AUTO FILTER BOT WITH PREMIUM FEATURES.</b>\n\n"
              "Here you get a clean, fast and premium movie-search experience with smart filters, smooth verification and direct result access.")
        await _edit_menu_message(q,text,main_menu_keyboard(cfg)); return
    if action=="top":
        rows=await context.application.bot_data["db"].top_searches(10)
        text="⭐ <b>TOP SEARCHING</b>\n\n"+("\n".join(f"<b>{i}.</b> {escape(str(r.get('query') or 'Unknown'))} — <code>{r.get('count',0)}</code> searches" for i,r in enumerate(rows,1)) if rows else "No searches have been recorded yet.")
    else:
        text=menu_text(action, context.application.bot_data["cfg"]) or "Unavailable."
    await _edit_menu_message(q,text,InlineKeyboardMarkup([[InlineKeyboardButton("‹ BACK TO MAIN MENU",callback_data="menu:main")]]))


# ---------------------------

async def deliver(update, context, movie_ids):
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    user_id = update.effective_user.id

    sent_ids = []
    for movie_id in movie_ids:
        try:
            from bson import ObjectId
            doc = await db.movies.find_one({"_id": ObjectId(movie_id)})
        except Exception:
            doc = None
        if not doc:
            continue
        try:
            sent = await context.bot.copy_message(
                chat_id=user_id,
                from_chat_id=cfg.database_channel,
                message_id=doc["message_id"],
            )
            sent_ids.append(sent.message_id)
        except Exception:
            log.exception("Failed to copy database message %s", doc.get("message_id"))

    if not sent_ids:
        await update.effective_message.reply_text(
            "⚠️ The requested file is unavailable right now. Please try again."
        )
        return

    notice = await update.effective_message.reply_text(
        cfg.expiry_notice
    )
    sent_ids.append(notice.message_id)

    context.job_queue.run_once(
        delete_delivered,
        when=cfg.delete_after,
        data={"chat_id": user_id, "message_ids": sent_ids},
    )


async def delete_delivered(context):
    data = context.job.data
    for mid in data["message_ids"]:
        try:
            await context.bot.delete_message(data["chat_id"], mid)
        except Exception:
            pass


# ---------------------------
# Start / verification
# ---------------------------

async def send_main_menu(update, context):
    cfg=context.application.bot_data["cfg"]
    user=update.effective_user
    bot_username = cfg.bot_username or (getattr(context.bot, "username", "") or "")
    bot_link = f"https://t.me/{bot_username}" if bot_username else "#"
    text=("🚩 <b>JAI SHRI RAM 🚩</b>\n\n"
          f"👋 <b>HEY {escape(user.first_name or 'Friend')}</b>, {time_greeting(cfg.greeting_timezone)}\n\n"
          f"🤖 I AM <a href=\"{bot_link}\">Cinema HUB OG</a>,\n"
          "<b>THE MOST POWERFUL AUTO FILTER BOT WITH PREMIUM FEATURES.</b>\n\n"
          "Here you get a clean, fast and premium movie-search experience with smart filters, smooth verification and direct result access.")
    image=random.choice(cfg.start_image_urls) if cfg.start_image_urls else None
    if image:
        try:
            return await update.effective_message.reply_photo(photo=image,caption=text,reply_markup=main_menu_keyboard(cfg),parse_mode="HTML")
        except Exception:
            log.warning("Welcome image could not be sent; falling back to text.")
    return await update.effective_message.reply_text(text,reply_markup=main_menu_keyboard(cfg),parse_mode="HTML")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_message: return
    cfg=context.application.bot_data["cfg"]; db=context.application.bot_data["db"]
    await db.record_user(update.effective_user.id,username=update.effective_user.username or "",first_name=update.effective_user.first_name or "")
    payload=context.args[0] if context.args else ""
    if payload.startswith("file_"):
        await begin_gate(update,context,[payload[5:]]); return
    if payload.startswith("all_"):
        batch=await db.consume_batch(payload[4:],update.effective_user.id)
        if not batch:
            await update.effective_message.reply_text("❌ This batch link is expired, invalid, or already used."); return
        await begin_gate(update,context,batch["movie_ids"]); return
    if payload.startswith("web_"):
        movie_id = payload[4:]
        try:
            from bson import ObjectId
            doc = await db.movies.find_one({"_id": ObjectId(movie_id)})
        except Exception:
            doc = None
        if not doc:
            await update.effective_message.reply_text("⚠️ This title is no longer available.")
            return
        title = str(doc.get("title") or doc.get("filename") or "Untitled")
        await begin_gate(update, context, [movie_id], mode="web_direct", title=title)
        return
    if payload.startswith("sv_"):
        await verification_return(update,context,payload[3:]); return
    await send_main_menu(update,context)

async def begin_gate(update, context, movie_ids, mode="standard", title=None):
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    user_id = update.effective_user.id

    if cfg.require_fsub and cfg.fsub_channels:
        missing = await missing_channels(
            context.bot, user_id, cfg.fsub_channels
        )
        if missing:
            clean_ids = [str(x) for x in movie_ids]
            gate_id = new_token(10)
            expires = datetime.now(timezone.utc) + timedelta(minutes=30)
            await db.create_fsub_gate(
                gate_id, user_id, clean_ids, expires, mode=mode, title=title
            )
            await update.effective_message.reply_text(
                premium_fsub_text(),
                reply_markup=premium_fsub_keyboard(cfg, gate_id),
                parse_mode="HTML",
            )
            return

    await send_linkpays(update, context, movie_ids, mode=mode, title=title)


async def fsub_check(update, context):
    query=update.callback_query; cfg=context.application.bot_data["cfg"]; db=context.application.bot_data["db"]
    gate_id=query.data.split(":",1)[1]
    missing=await missing_channels(context.bot,query.from_user.id,cfg.fsub_channels)
    if missing:
        await query.answer("Please join all required channels, then try again.",show_alert=True); return
    gate=await db.consume_fsub_gate(gate_id,query.from_user.id)
    if not gate:
        await query.answer("Verification session expired. Open the flow again.",show_alert=True); return
    await query.answer("Verified ✅")
    if not gate.get("movie_ids"):
        markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 REQUEST HERE",url=request_group_url(cfg) or "https://t.me/")]])
        text=request_group_prompt(query.from_user)
        try: await query.message.edit_text(text,reply_markup=markup,parse_mode="HTML")
        except Exception: await query.message.reply_text(text,reply_markup=markup,parse_mode="HTML")
        return
    await query.message.reply_text("✅ Subscription verified. Creating your verification link…")
    await send_linkpays(query,context,gate["movie_ids"],mode=gate.get("mode","standard"),title=gate.get("title") or None)

async def send_linkpays(update, context, movie_ids, mode="standard", title=None):
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    user_id = update.effective_user.id

    if not cfg.require_shortlink:
        await deliver(update, context, movie_ids)
        return

    token = new_token(24)
    expires = datetime.now(timezone.utc) + timedelta(
        seconds=cfg.shortlink_ttl
    )
    clean_ids = [str(x) for x in movie_ids]
    await db.create_token(
        token, user_id, clean_ids, expires, mode=mode, title=title
    )

    destination = create_deep_linked_url(
        cfg.bot_username, f"sv_{token}"
    )

    try:
        short = await shorten_linkpays(cfg, destination)
    except Exception as exc:
        log.exception("Linkpays failed")
        await update.effective_message.reply_text(
            "⚠️ Verification service is temporarily unavailable. "
            "Please try again later."
        )
        return

    await update.effective_message.reply_text(
        "🔗 <b>Verification Required</b>\n\n"
        "Complete the verification using the button below. "
        "After completion, you will be returned to the bot automatically.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔗 Verify & Continue", url=short)]]
        ),
        parse_mode="HTML",
    )


async def verification_return(update, context, token):
    cfg = context.application.bot_data["cfg"]
    db = context.application.bot_data["db"]
    user_id = update.effective_user.id

    if cfg.require_fsub and cfg.fsub_channels:
        missing = await missing_channels(
            context.bot, user_id, cfg.fsub_channels
        )
        if missing:
            await update.effective_message.reply_text(
                "❌ Subscription verification failed. "
                "Join all required channels and try again."
            )
            return

    doc = await db.consume_token(token, user_id)
    if not doc:
        await update.effective_message.reply_text(
            "❌ This verification link is invalid, expired, already used, "
            "or belongs to another user."
        )
        return

    if doc.get("mode") == "web_direct":
        await deliver(update, context, doc.get("movie_ids") or [])
        return

    await deliver(
        update, context, doc["movie_ids"]
    )


# ---------------------------
# Request/Search group
# ---------------------------

async def react_to_search_message(message):
    try:
        await message.set_reaction(reaction=random.choice(["🔥","❤️","😍","👍","🤩","⚡"]),is_big=True)
    except Exception:
        pass

async def group_search(update, context):
    message=update.message
    if not message or not message.text or (message.from_user and message.from_user.is_bot): return
    cfg=context.application.bot_data["cfg"]
    if str(update.effective_chat.id)!=str(cfg.request_group): return
    query=message.text.strip()
    if not query or query.startswith("/") or not update.effective_user: return
    db=context.application.bot_data["db"]
    await react_to_search_message(message)
    try: await db.record_request(update.effective_user.id,query)
    except Exception: log.exception("Could not record search query")
    searching=await message.reply_text(
        f"👑 <b>CINEMA HUB OG</b>\n\n"
        f"👤 <b>{escape(update.effective_user.first_name or 'Friend')}</b> requested:\n"
        f"<blockquote>🎬 {escape(query)}</blockquote>\n"
        f"🔎 <b>SEARCHING FOR:</b> <u>{escape(query.lower())}</u>\n\n"
        "✨ Please wait a moment…",
        parse_mode="HTML",reply_to_message_id=message.message_id)
    await asyncio.sleep(0.25)
    try:
        results=await db.find_movies(query,cfg.max_results)
    except Exception:
        log.exception("Group search failed for query=%r",query)
        await searching.edit_text("⚠️ <b>Search is temporarily unavailable.</b>\n\nPlease try again in a moment.",parse_mode="HTML"); return
    if not results:
        suggestions=await db.suggestions(query)
        if suggestions:
            sid=await db.create_search(update.effective_user.id,query,{"suggestions":suggestions},datetime.now(timezone.utc)+timedelta(minutes=30))
            buttons=[[InlineKeyboardButton(title[:44],callback_data=f"suggest:{sid}:{i}")] for i,title in enumerate(suggestions)]
            await searching.edit_text(f"👑 <b>CINEMA HUB OG</b>\n\n❌ <b>NO EXACT MATCH FOUND</b>\n\n💡 <b>DID YOU MEAN:</b>",reply_markup=InlineKeyboardMarkup(buttons),parse_mode="HTML")
        else:
            await searching.edit_text(f"👑 <b>CINEMA HUB OG</b>\n\n❌ <b>NO MATCHING FILE FOUND</b>\n\n<blockquote>{escape(query)}</blockquote>\nPlease request this title in the group.",parse_mode="HTML")
        return
    await db.record_user(update.effective_user.id)
    search_id=await db.create_search(update.effective_user.id,query,{},datetime.now(timezone.utc)+timedelta(minutes=30))
    await render_search_message(searching,context,results,search_id,0,query,update.effective_user.id)

async def page_callback(update, context):
    q=update.callback_query
    try: _,page_raw,search_id=q.data.split(":",2); page=int(page_raw)
    except ValueError: await q.answer("Invalid page.",show_alert=True); return
    db=context.application.bot_data["db"]; doc=await db.get_search(search_id,q.from_user.id)
    if not doc: await q.answer("Search expired. Search again.",show_alert=True); return
    cfg=context.application.bot_data["cfg"]; results=await db.find_movies(doc["query"],cfg.max_results,doc.get("filters") or {})
    if not results: await q.answer("No results.",show_alert=True); return
    await q.answer(); await render_search_message(q.message,context,results,search_id,page,doc["query"],q.from_user.id)

async def filter_menu_callback(update, context):
    q=update.callback_query
    try: _,kind,search_id=q.data.split(":",2)
    except ValueError: await q.answer("Invalid filter.",show_alert=True); return
    if kind not in {"quality","language","season"}: await q.answer("Invalid filter.",show_alert=True); return
    db=context.application.bot_data["db"]; doc=await db.get_search(search_id,q.from_user.id)
    if not doc: await q.answer("Search expired.",show_alert=True); return
    cfg=context.application.bot_data["cfg"]; all_results=await db.find_movies(doc["query"],cfg.max_results,{})
    values=sorted({str(m.get(kind)) for m in all_results if m.get(kind)})
    rows=[[InlineKeyboardButton(v[:38],callback_data=f"filter:{kind}:{search_id}:{i}")] for i,v in enumerate(values[:12])]
    rows.append([InlineKeyboardButton("‹ BACK TO RESULTS",callback_data=f"page:0:{search_id}")])
    await q.answer(); await q.message.edit_reply_markup(InlineKeyboardMarkup(rows))

async def filter_callback(update, context):
    q=update.callback_query
    try: _,kind,search_id,idx_raw=q.data.split(":",3); idx=int(idx_raw)
    except (ValueError,IndexError): await q.answer("Invalid filter.",show_alert=True); return
    db=context.application.bot_data["db"]; doc=await db.get_search(search_id,q.from_user.id)
    if not doc: await q.answer("Search expired.",show_alert=True); return
    cfg=context.application.bot_data["cfg"]; all_results=await db.find_movies(doc["query"],cfg.max_results,{})
    values=sorted({str(m.get(kind)) for m in all_results if m.get(kind)})
    if idx<0 or idx>=len(values): await q.answer("Filter expired.",show_alert=True); return
    filters_=dict(doc.get("filters") or {}); filters_[kind]=values[idx]
    await db.update_search_filter(search_id,q.from_user.id,filters_)
    results=await db.find_movies(doc["query"],cfg.max_results,filters_)
    if not results: await q.answer("No results for this filter.",show_alert=True); return
    await q.answer("Filter applied ✅"); await render_search_message(q.message,context,results,search_id,0,doc["query"],q.from_user.id)

async def suggest_callback(update, context):
    q=update.callback_query
    try: _,sid,idx_raw=q.data.split(":",2); idx=int(idx_raw)
    except (ValueError,IndexError): await q.answer("Invalid suggestion.",show_alert=True); return
    db=context.application.bot_data["db"]; doc=await db.get_search(sid,q.from_user.id); suggestions=(doc or {}).get("filters",{}).get("suggestions",[])
    if idx<0 or idx>=len(suggestions): await q.answer("Suggestion expired.",show_alert=True); return
    text=suggestions[idx]; cfg=context.application.bot_data["cfg"]; results=await db.find_movies(text,cfg.max_results)
    if not results: await q.answer("No matching file found.",show_alert=True); return
    await q.answer("Searching…"); await render_search_message(q.message,context,results,sid,0,text,q.from_user.id)

# ---------------------------
# Channel indexing
# ---------------------------

async def index_channel_post(update, context):
    msg = update.channel_post or update.edited_channel_post
    if not msg:
        return
    cfg = context.application.bot_data["cfg"]
    if not cfg.auto_index:
        return
    if str(msg.chat.id) != str(cfg.database_channel):
        return
    if not (
        msg.video or msg.document or msg.audio or msg.photo
    ):
        return

    caption = msg.caption or msg.text or ""
    filename = media_filename(msg)
    title = (
        caption.splitlines()[0].strip()
        if caption.strip()
        else (filename or f"File {msg.message_id}")
    )
    meta = parse_metadata(
        f"{title}\n{caption}\n{filename or ''}"
    )
    file_size = media_file_size(msg)
    try:
        await context.application.bot_data["db"].upsert_movie(
            {
                "chat_id": msg.chat.id,
                "message_id": msg.message_id,
                "title": title[:500],
                "caption": caption[:4000],
                "filename": filename,
                "normalized": normalize(
                    f"{title} {caption} {filename or ''}"
                ),
                "quality": meta["quality"],
                "quality_rank": meta["quality_rank"],
                "language": meta["language"],
                "season": meta["season"],
                "size": meta["size"],
                "file_size": file_size,
                "indexed_at": datetime.now(timezone.utc),
            }
        )
        log.info(
            "Indexed new database-channel media: chat=%s message=%s title=%r",
            msg.chat.id, msg.message_id, title[:120],
        )
    except Exception:
        log.exception(
            "Failed to index database-channel media: chat=%s message=%s",
            msg.chat.id, msg.message_id,
        )


def _prepare_session_string(raw):
    """Normalize common Render/env formatting without inventing session data."""
    if not raw:
        return ""
    value = str(raw).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1].strip()
    value = "".join(value.split())
    # Telethon stores a one-character version prefix outside the base64 data.
    # Add omitted padding only to the base64 payload, never to the prefix.
    if value.startswith("1"):
        payload = value[1:]
        payload += "=" * (-len(payload) % 4)
        return "1" + payload
    return value


def _validate_session_string(raw):
    """Return a normalized StringSession or None if it is structurally invalid."""
    value = _prepare_session_string(raw)
    if not value:
        return None
    try:
        # Decode only for an early, deterministic format check. Telethon still
        # performs the authoritative StringSession parsing below. The first
        # character is Telethon's version prefix and is not part of base64.
        if not value.startswith("1"):
            raise ValueError("unsupported StringSession version")
        decoded = base64.urlsafe_b64decode(value[1:].encode("ascii"))
        if len(decoded) not in {263, 275}:
            raise ValueError(
                f"decoded session has unexpected length {len(decoded)}"
            )
        return value
    except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
        log.warning("SESSION_STRING is invalid; historical indexing is disabled: %s", exc)
        return None


async def historical_index(cfg, db, force=False):
    """Backfill existing media from the database channel.

    INDEX_ON_START controls automatic startup backfill only.  A manual /reindex
    command must still work when INDEX_ON_START=false.
    """
    if not force and not cfg.index_on_start:
        return 0
    if not cfg.session_string:
        log.info(
            "INDEX_ON_START=true but SESSION_STRING is empty; skipping historical index."
        )
        return 0
    if TelegramClient is None or StringSession is None:
        log.warning("Telethon is unavailable; historical index skipped.")
        return 0

    session_string = _validate_session_string(cfg.session_string)
    if not session_string:
        # Do not repeatedly generate scary tracebacks for a configuration value
        # that cannot possibly work until the operator replaces it.
        cfg.index_on_start = False
        return 0

    count = 0
    try:
        async with TelegramClient(
            StringSession(session_string),
            cfg.api_id,
            cfg.api_hash,
        ) as client:
            if not await client.is_user_authorized():
                log.warning(
                    "SESSION_STRING is not authorized for a Telegram account; "
                    "historical indexing is disabled."
                )
                cfg.index_on_start = False
                return 0

            async for msg in client.iter_messages(cfg.database_channel):
                if not (msg.video or msg.document or msg.audio or msg.photo):
                    continue

                caption = msg.message or ""
                filename = media_filename(msg)
                title = (
                    caption.splitlines()[0].strip()
                    if caption.strip()
                    else (filename or f"File {msg.id}")
                )
                meta = parse_metadata(f"{title}\n{caption}\n{filename or ''}")
                file_size = media_file_size(msg)
                await db.upsert_movie(
                    {
                        "chat_id": cfg.database_channel,
                        "message_id": msg.id,
                        "title": title[:500],
                        "caption": caption[:4000],
                        "filename": filename,
                        "normalized": normalize(
                            f"{title} {caption} {filename or ''}"
                        ),
                        "quality": meta["quality"],
                        "quality_rank": meta["quality_rank"],
                        "language": meta["language"],
                        "season": meta["season"],
                        "size": meta["size"],
                        "file_size": file_size,
                        "indexed_at": datetime.now(timezone.utc),
                    }
                )
                count += 1

        log.info("Historical index complete: %s media messages.", count)
    except (binascii.Error, UnicodeError, ValueError, TypeError, struct.error) as exc:
        # A malformed session must never make the Render service look unhealthy.
        cfg.index_on_start = False
        log.warning("Historical indexing skipped because SESSION_STRING is invalid: %s", exc)
    except Exception as exc:
        # Historical backfill is an optional/background feature. The bot can
        # continue serving searches and new channel posts even if backfill fails.
        log.warning("Historical indexing skipped after a non-fatal error: %s", exc)
    return count


# ---------------------------
# Admin
# ---------------------------

def is_admin(update, cfg):
    return bool(
        update.effective_user
        and update.effective_user.id in cfg.admin_ids
    )


async def deny_admin(update, cfg):
    """Never silently swallow admin commands; show the exact ID to configure."""
    if not update.effective_message or not update.effective_user:
        return
    await update.effective_message.reply_text(
        "⛔ Admin access required.\n\n"
        f"Your Telegram ID: <code>{update.effective_user.id}</code>\n"
        "Add this numeric ID to Render → Environment → ADMIN_IDS, then redeploy.",
        parse_mode="HTML",
    )


async def id_cmd(update, context):
    if not update.effective_user or not update.effective_message:
        return
    await update.effective_message.reply_text(
        f"🆔 Your Telegram ID: <code>{update.effective_user.id}</code>",
        parse_mode="HTML",
    )


async def admin_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    await update.message.reply_text(
        "🛠 Admin:\n"
        "/stats\n"
        "/reindex (or /index)\n"
        "/iasync\n"
        "/iastats\n"
        "/getsettings\n"
        "/broadcast <text>"
    )


async def stats_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    stats = await context.application.bot_data["db"].stats()
    await update.message.reply_text(
        f"📊 Movies: {stats['movies']}\n"
        f"Users: {stats['users']}\n"
        f"Tokens: {stats['tokens']}\n"
        f"Requests: {stats['requests']}\n"
        f"IA jobs: {stats['ingest_jobs']} | Uploaded: {stats['ingest_uploaded']}\n\n"
        f"Auto-index new posts: {cfg.auto_index}\n"
        f"Index on start: {cfg.index_on_start}\n"
        f"Session configured: {bool(cfg.session_string)}"
    )


async def reindex_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return

    if not cfg.session_string:
        await update.message.reply_text(
            "⚠️ Reindex needs SESSION_STRING.\n\n"
            "INDEX_ON_START can stay false; manual /reindex works independently. "
            "Generate a valid Telethon StringSession and put it in Render Environment."
        )
        return

    if _validate_session_string(cfg.session_string) is None:
        await update.message.reply_text(
            "⚠️ SESSION_STRING is malformed/invalid, so old channel history cannot be indexed.\n\n"
            "Replace SESSION_STRING with a fresh valid Telethon StringSession. "
            "New channel posts can still be auto-indexed when AUTO_INDEX_NEW_POSTS=true."
        )
        return

    await update.message.reply_text(
        "🔄 Historical indexing started in the background.\n"
        "You can keep using the bot while it runs."
    )

    async def work():
        count = await historical_index(
            cfg, context.application.bot_data["db"], force=True
        )
        try:
            await update.message.reply_text(
                f"✅ Indexing finished. Indexed {count} media messages."
            )
        except Exception:
            pass

    asyncio.create_task(work())


async def iasync_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    ingestor = context.application.bot_data.get("ingestor")
    if not ingestor or not cfg.ia_ingest_enabled:
        await update.message.reply_text(
            "⚠️ Automatic ingestion is disabled. Set IA_INGEST_ENABLED=true in Render and redeploy."
        )
        return
    result = await ingestor.sync_once(initial=False, max_items=cfg.ia_batch_size)
    reasons = result.get("skip_reasons") or {}
    reason_text = ""
    if reasons:
        reason_text = "\n\n<b>Skip reasons:</b>\n" + "\n".join(
            f"• {reason}: {count}" for reason, count in sorted(reasons.items())
        )
    await update.message.reply_text(
        "📥 <b>AUTHORIZED INGESTION</b>\n\n"
        f"Discovered: {result['discovered']}\n"
        f"Uploaded: {result['uploaded']}\n"
        f"Skipped: {result['skipped']}\n"
        f"Failed: {result['failed']}" + reason_text,
        parse_mode="HTML",
    )


async def iastats_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    ingestor = context.application.bot_data.get("ingestor")
    stats = await context.application.bot_data["db"].ingest_jobs.count_documents({})
    uploaded = await context.application.bot_data["db"].ingest_jobs.count_documents({"status": "uploaded"})
    failed = await context.application.bot_data["db"].ingest_jobs.count_documents({"status": "failed"})
    skipped = await context.application.bot_data["db"].ingest_jobs.count_documents({"status": "skipped"})
    await update.message.reply_text(
        "📊 <b>INGESTION STATUS</b>\n\n"
        f"Enabled: {cfg.ia_ingest_enabled}\n"
        f"Jobs: {stats}\n"
        f"Uploaded: {uploaded}\n"
        f"Skipped: {skipped}\n"
        f"Failed: {failed}\n"
        f"Worker running: {bool(ingestor and ingestor.running)}",
        parse_mode="HTML",
    )


async def settings_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    await update.message.reply_text(
        "⚙️ Settings\n"
        f"Force Subscribe: {cfg.require_fsub}\n"
        f"Linkpays shortener: {cfg.require_shortlink}\n"
        f"Delete after: {cfg.delete_after}s\n"
        f"Search page size: {cfg.page_size}\n"
        f"Index on start: {cfg.index_on_start}\n"
        f"Auto ingestion: {cfg.ia_ingest_enabled}"
    )


async def broadcast_cmd(update, context):
    cfg = context.application.bot_data["cfg"]
    if not is_admin(update, cfg):
        await deny_admin(update, cfg)
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text(
            "Usage: /broadcast <message>"
        )
        return

    cursor = context.application.bot_data["db"].users.find(
        {}, {"user_id": 1}
    )
    sent = 0
    async for row in cursor:
        try:
            await context.bot.send_message(row["user_id"], text)
            sent += 1
        except Exception:
            continue
    await update.message.reply_text(
        f"✅ Broadcast sent to {sent} users."
    )


# ---------------------------
# General updates
# ---------------------------

async def private_text(update, context):
    if not update.message or not update.effective_user or update.effective_chat.type != "private": return
    cfg=context.application.bot_data["cfg"]
    if cfg.require_fsub and cfg.fsub_channels:
        missing=await missing_channels(context.bot,update.effective_user.id,cfg.fsub_channels)
        if missing:
            gate_id=new_token(10)
            await context.application.bot_data["db"].create_fsub_gate(gate_id,update.effective_user.id,[],datetime.now(timezone.utc)+timedelta(minutes=30))
            await update.message.reply_text(premium_fsub_text(),reply_markup=premium_fsub_keyboard(cfg,gate_id),parse_mode="HTML")
            return
    await update.message.reply_text(request_group_prompt(update.effective_user),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📝 REQUEST HERE",url=request_group_url(cfg) or "https://t.me/")]]),parse_mode="HTML")

async def channel_type_dispatch(update, context):
    if update.channel_post or update.edited_channel_post:
        await index_channel_post(update, context)


async def noop_callback(update, context):
    query = update.callback_query
    if query:
        await query.answer()


async def menu_command(update, context, kind):
    if not update.effective_message: return
    if kind=="top":
        rows=await context.application.bot_data["db"].top_searches(10)
        text="⭐ <b>TOP SEARCHING</b>\n\n"+("\n".join(f"<b>{i}.</b> {escape(str(r.get('query') or 'Unknown'))} — <code>{r.get('count',0)}</code> searches" for i,r in enumerate(rows,1)) if rows else "No searches have been recorded yet.")
    else:
        text=menu_text(kind, context.application.bot_data["cfg"]) or "Unavailable."
    await update.effective_message.reply_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("‹ BACK TO MAIN MENU",callback_data="menu:main")]]),parse_mode="HTML")


async def error_handler(update, context):
    log.error(
        "Unhandled update exception: %s",
        context.error,
        exc_info=context.error,
    )
    cfg = context.application.bot_data.get("cfg")
    if cfg and cfg.log_chat_id:
        try:
            await context.bot.send_message(
                cfg.log_chat_id,
                f"⚠️ Bot error:\n{escape(str(context.error))[:3500]}",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ---------------------------
# Runtime / Render health
# ---------------------------

async def retry_telegram_call(label, fn, attempts=6):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except (BadRequest, Forbidden) as exc:
            # These are normally permanent configuration/access errors (for
            # example an invalid chat ID or a bot that cannot access a private
            # chat). Retrying the same request immediately only delays recovery.
            log.error("Telegram %s rejected permanently: %s", label, exc)
            raise
        except Exception as exc:
            last = exc
            log.warning(
                "Telegram %s failed (attempt %s/%s): %s",
                label, attempt, attempts, exc,
            )
            if attempt < attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 15))
    raise last

class Runtime:
    def __init__(self):
        self.cfg = Config()
        self.db = Database(self.cfg.mongo_uri, self.cfg.db_name)
        self.app = None
        self.telegram_task = None
        self.db_task = None
        self.db_ready = False
        self.telegram_ready = False
        self.index_started = False
        self._stopping = False
        self.ingestor = InternetArchiveIngestor(self.cfg, self.db, log) if InternetArchiveIngestor else None
        self.cfg._runtime_db_ready = False
        self.cfg._runtime_telegram_ready = False

    def _build_app(self):
        request = HTTPXRequest(
            connection_pool_size=64,
            read_timeout=self.cfg.telegram_read_timeout,
            write_timeout=self.cfg.telegram_write_timeout,
            connect_timeout=self.cfg.telegram_connect_timeout,
            pool_timeout=self.cfg.telegram_pool_timeout,
            http_version="1.1",
        )
        updates_request = HTTPXRequest(
            connection_pool_size=16,
            read_timeout=max(60, self.cfg.telegram_read_timeout),
            write_timeout=self.cfg.telegram_write_timeout,
            connect_timeout=self.cfg.telegram_connect_timeout,
            pool_timeout=self.cfg.telegram_pool_timeout,
            http_version="1.1",
        )
        app = (
            Application.builder()
            .token(self.cfg.bot_token)
            .request(request)
            .get_updates_request(updates_request)
            .concurrent_updates(True)
            .build()
        )
        self._register_handlers(app)
        return app

    def _register_handlers(self, app):
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("id", id_cmd))
        app.add_handler(CommandHandler("admin", admin_cmd))
        app.add_handler(CommandHandler("stats", stats_cmd))
        app.add_handler(CommandHandler("reindex", reindex_cmd))
        app.add_handler(CommandHandler("index", reindex_cmd))
        app.add_handler(CommandHandler("iasync", iasync_cmd))
        app.add_handler(CommandHandler("iastats", iastats_cmd))
        app.add_handler(CommandHandler("getsettings", settings_cmd))
        app.add_handler(CommandHandler("broadcast", broadcast_cmd))
        app.add_handler(CommandHandler("help", lambda update, context: menu_command(update, context, "help")))
        app.add_handler(CommandHandler("about", lambda update, context: menu_command(update, context, "about")))
        app.add_handler(CommandHandler("top", lambda update, context: menu_command(update, context, "top")))
        app.add_handler(CommandHandler("upgrade", lambda update, context: menu_command(update, context, "upgrade")))
        app.add_handler(CallbackQueryHandler(fsub_check, pattern=r"^fsub:"))
        app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
        app.add_handler(CallbackQueryHandler(page_callback, pattern=r"^page:"))
        app.add_handler(CallbackQueryHandler(filter_menu_callback, pattern=r"^filter_menu:"))
        app.add_handler(CallbackQueryHandler(filter_callback, pattern=r"^filter:"))
        app.add_handler(CallbackQueryHandler(suggest_callback, pattern=r"^suggest:"))
        app.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"))
        app.add_handler(TypeHandler(Update, channel_type_dispatch), group=1)
        app.add_handler(
            MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, group_search),
            group=0,
        )
        app.add_handler(
            MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, private_text),
            group=0,
        )
        app.add_error_handler(error_handler)

    async def _cleanup_app(self):
        app = self.app
        self.app = None
        self.telegram_ready = False
        if app is None:
            return
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
        except Exception:
            log.exception("Telegram updater cleanup failed")
        try:
            if app.running:
                await app.stop()
        except Exception:
            log.exception("Telegram application cleanup failed")
        try:
            await app.shutdown()
        except Exception:
            log.exception("Telegram application shutdown failed")

    async def _resolve_and_validate(self, app):
        me = await retry_telegram_call(
            "get_me",
            lambda: app.bot.get_me(read_timeout=self.cfg.telegram_read_timeout),
            attempts=8,
        )
        actual_username = (me.username or "").lstrip("@")
        configured_username = (self.cfg.bot_username or "").lstrip("@")
        if configured_username and actual_username and configured_username != actual_username:
            log.warning(
                "BOT_USERNAME=%s does not match Telegram account @%s; using Telegram's actual username.",
                configured_username, actual_username,
            )
        self.cfg.bot_username = actual_username or configured_username
        if not self.cfg.bot_username:
            raise RuntimeError("Telegram did not provide a bot username and BOT_USERNAME is empty.")

        db_chat = await retry_telegram_call(
            "database channel lookup",
            lambda: app.bot.get_chat(self.cfg.database_channel),
        )
        request_chat = await retry_telegram_call(
            "request group lookup",
            lambda: app.bot.get_chat(self.cfg.request_group),
        )
        self.cfg.database_channel = db_chat.id
        self.cfg.request_group = request_chat.id

        # F-Sub is deliberately non-fatal. A stale/private/deleted channel ID
        # must not take the whole Render service down. Permanent Telegram
        # errors (BadRequest/Forbidden) disable only that F-Sub entry; network
        # timeouts and other transient errors still abort this validation pass
        # so the worker retries them normally.
        if self.cfg.require_fsub and self.cfg.fsub_channels:
            valid_channels = []
            valid_links = []
            for idx, channel in enumerate(self.cfg.fsub_channels):
                try:
                    chat = await app.bot.get_chat(channel)
                except (BadRequest, Forbidden) as exc:
                    log.error(
                        "Disabling invalid/inaccessible F-Sub channel %s: %s",
                        channel,
                        exc,
                    )
                    continue

                link = self.cfg.fsub_links[idx] if idx < len(self.cfg.fsub_links) else ""
                if not link:
                    username = getattr(chat, "username", None)
                    if username:
                        link = f"https://t.me/{username.lstrip('@')}"
                    else:
                        log.error(
                            "Disabling private F-Sub channel %s because no invite link is configured.",
                            channel,
                        )
                        continue

                valid_channels.append(chat.id)
                valid_links.append(link)

            self.cfg.fsub_channels = valid_channels
            self.cfg.fsub_links = valid_links
            if not valid_channels:
                self.cfg.require_fsub = False
                log.warning(
                    "No usable F-Sub channels remain. Force Subscribe has been disabled; "
                    "the bot will continue starting normally."
                )

        log.info(
            "Telegram configuration validated: @%s | DB=%s | Request=%s | FSub=%s",
            self.cfg.bot_username,
            self.cfg.database_channel,
            self.cfg.request_group,
            self.cfg.fsub_channels if self.cfg.require_fsub else "disabled",
        )

    async def _database_worker(self):
        delay = 3
        while not self._stopping:
            try:
                await self.db.init()
                self.db_ready = True
                self.cfg._runtime_db_ready = True
                log.info("MongoDB is ready.")
                return
            except Exception as exc:
                self.db_ready = False
                log.exception("MongoDB background initialization failed: %s", exc)
                if self._stopping:
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _telegram_worker(self):
        delay = self.cfg.startup_retry_delay
        while not self._stopping:
            if not self.db_ready:
                await asyncio.sleep(2)
                continue
            try:
                app = self._build_app()
                self.app = app
                await app.initialize()
                await self._resolve_and_validate(app)
                app.bot_data["cfg"] = self.cfg
                app.bot_data["db"] = self.db
                app.bot_data["ingestor"] = self.ingestor
                await app.start()
                await app.updater.start_polling(
                    allowed_updates=["message", "callback_query", "channel_post", "edited_channel_post"],
                    drop_pending_updates=True,
                )
                self.telegram_ready = True
                self.cfg._runtime_telegram_ready = True
                log.info("Telegram polling started successfully.")
                if self.cfg.ia_ingest_enabled and self.ingestor:
                    await self.ingestor.start()
                if self.cfg.index_on_start and not self.index_started:
                    self.index_started = True
                    asyncio.create_task(historical_index(self.cfg, self.db))
                delay = self.cfg.startup_retry_delay
                while not self._stopping and self.app is app and app.updater.running:
                    await asyncio.sleep(10)
                if self._stopping:
                    return
                log.warning("Telegram polling stopped unexpectedly; reconnecting.")
                if self.ingestor:
                    await self.ingestor.stop()
                self.cfg._runtime_telegram_ready = False
                await self._cleanup_app()
                await asyncio.sleep(delay)
                continue
            except Exception as exc:
                self.telegram_ready = False
                self.cfg._runtime_telegram_ready = False
                if self.ingestor:
                    await self.ingestor.stop()
                log.exception("Telegram startup/connection attempt failed: %s", exc)
                await self._cleanup_app()
                if self._stopping:
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def start(self):
        # Never block Render's HTTP server startup on external networks. Both
        # MongoDB and Telegram are retried in background workers.
        self.db_task = asyncio.create_task(self._database_worker())
        self.telegram_task = asyncio.create_task(self._telegram_worker())

    async def stop(self):
        self._stopping = True
        for task in (self.telegram_task, self.db_task):
            if task:
                task.cancel()
        for task in (self.telegram_task, self.db_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self.ingestor:
            await self.ingestor.stop()
        self.cfg._runtime_db_ready = False
        self.cfg._runtime_telegram_ready = False
        await self._cleanup_app()
        try:
            await self.db.close()
        except Exception:
            log.exception("MongoDB shutdown failed")


async def create_api(runtime):
    @asynccontextmanager
    async def lifespan(api):
        await runtime.start()
        yield
        await runtime.stop()

    api = FastAPI(lifespan=lifespan)

    @api.get("/")
    async def root():
        return {
            "status": "ok",
            "service": "autofilter-movie-bot",
        }

    @api.get("/health")
    async def health():
        return {
            "status": "ok",
            "database_ready": runtime.db_ready,
            "telegram_ready": runtime.telegram_ready,
        }

    return api


def run():
    async def runner():
        runtime = Runtime()
        api = await create_api(runtime)
        port = int(os.getenv("PORT", "10000"))
        server = uvicorn.Server(
            uvicorn.Config(
                api,
                host="0.0.0.0",
                port=port,
                log_level="info",
            )
        )
        await server.serve()

    asyncio.run(runner())


if __name__ == "__main__":
    run()
