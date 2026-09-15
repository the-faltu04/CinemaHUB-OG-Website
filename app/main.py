from __future__ import annotations

import asyncio
import base64
import html
import math
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pymongo import DESCENDING, AsyncMongoClient

load_dotenv()

SITE_NAME = os.getenv("SITE_NAME", "Cinema HUB OG")
SITE_TAGLINE = os.getenv("SITE_TAGLINE", "THE CINEMA EXPERIENCE, REIMAGINED")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")
DB_NAME = os.getenv("DB_NAME", "autofilter")
MONGO_URI = os.getenv("MONGO_URI", "")
DEVELOPER_NAME = os.getenv("DEVELOPER_NAME", "Cinema HUB OG Developer")
DEVELOPER_USERNAME = os.getenv("DEVELOPER_USERNAME", "thevisionaryoffc").lstrip("@")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL", "").lstrip("@")
TELEGRAM_GROUP = os.getenv("TELEGRAM_GROUP", "").lstrip("@")

TVMAZE_BASE = "https://api.tvmaze.com"
IMDB_SUGGESTION_BASE = "https://v3.sg.media-imdb.com/suggestion/x"
WIKI_SUMMARY_BASE = "https://en.wikipedia.org/api/rest_v1/page/summary"
CATEGORY_NAMES = ["All", "Movies", "Web Series", "Anime", "K-Drama", "Serials", "Cartoons", "WWE"]
PROVIDER_CACHE_TTL = 24 * 60 * 60
HEALTH_CACHE_TTL = 10
MAX_DB_SCAN = 5000

mongo: AsyncMongoClient | None = AsyncMongoClient(MONGO_URI, serverSelectionTimeoutMS=2500) if MONGO_URI else None
http_client: httpx.AsyncClient | None = None
provider_semaphore = asyncio.Semaphore(3)
_provider_cache: dict[str, tuple[float, dict | None]] = {}
_provider_inflight: dict[str, asyncio.Task] = {}
_health_cache: tuple[float, dict] | None = None


def database():
    if mongo is None:
        raise RuntimeError("MONGO_URI is not configured")
    return mongo[DB_NAME]


def safe_text(value: Any) -> str:
    return str(value or "").strip()


def clean_title(value: Any) -> str:
    s = safe_text(value)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\([^)]*(?:1080p|720p|WEB[- ]?DL|WEBRip|BluRay|x264|x265|HDR|AAC|DDP|Hindi|English).*?\)", " ", s, flags=re.I)
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"\b(?:2160p|1080p|720p|480p|360p|4k|8k|hdrip|web[- ]?dl|webrip|bluray|blu-ray|hevc|x264|x265|h264|h265|aac|ddp|dd\+|proper|repack|uncut|sample)\b", " ", s, flags=re.I)
    s = re.sub(r"\b(?:hindi|english|tamil|telugu|malayalam|kannada|bengali|punjabi|multi|dual audio|dubbed|original)\b", " ", s, flags=re.I)
    s = re.sub(r"\b(?:season|s)\s*\d{1,2}\b", " ", s, flags=re.I)
    s = re.sub(r"\b(?:episode|ep|e)\s*\d{1,3}\b", " ", s, flags=re.I)
    s = re.sub(r"\b(?:part|pt)\s*\d{1,2}\b", " ", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" -|_")
    # Keep real titles clean for cards while retaining years separately in metadata.
    s = re.sub(r"(?:^|\s)(?:19\d{2}|20\d{2})$", "", s).strip(" -|_")
    return s or "Untitled"


def normalize_compare(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def similarity_score(a: str, b: str) -> float:
    aa, bb = normalize_compare(a), normalize_compare(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    if aa in bb or bb in aa:
        return 0.82
    # Cheap token overlap; enough for choosing among a handful of provider hits.
    at, bt = set(re.findall(r"[a-z0-9]+", a.lower())), set(re.findall(r"[a-z0-9]+", b.lower()))
    if not at or not bt:
        return 0.0
    return len(at & bt) / max(len(at | bt), 1)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "title"


def infer_year(doc: dict) -> str:
    for key in ("year", "release_year", "release_date", "date"):
        m = re.search(r"\b(19\d{2}|20\d{2})\b", safe_text(doc.get(key)))
        if m:
            return m.group(1)
    text = " ".join(safe_text(doc.get(k)) for k in ("title", "filename", "caption"))
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return m.group(1) if m else ""


def infer_category(doc: dict) -> str:
    raw = " ".join(safe_text(doc.get(k)) for k in ("title", "filename", "caption", "category", "type")).lower()
    if any(x in raw for x in ("anime", "one piece", "jujutsu", "naruto", "bleach", "attack on titan", "demon slayer")):
        return "Anime"
    if any(x in raw for x in ("wwe", "smackdown", "wrestlemania", "wrestling")):
        return "WWE"
    if any(x in raw for x in ("k-drama", "kdrama", "korean drama")):
        return "K-Drama"
    if any(x in raw for x in ("cartoon", "ben 10", "doraemon", "shinchan", "tom and jerry")):
        return "Cartoons"
    if any(x in raw for x in ("serial", "tv serial", "kundali", "anupama")):
        return "Serials"
    if re.search(r"\b(?:season|s\d{1,2}|episode|e\d{1,3}|web series)\b", raw):
        return "Web Series"
    return "Movies"


def format_size(n: Any) -> str:
    try:
        n = int(n or 0)
    except Exception:
        return ""
    if n <= 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    power = min(int(math.log(n, 1024)), len(units) - 1)
    return f"{n / (1024 ** power):.2f} {units[power]}"


def quality_score(doc: dict) -> int:
    value = " ".join(safe_text(doc.get(k)) for k in ("quality", "filename", "title")).lower()
    if "2160p" in value or "4k" in value:
        return 4
    if "1080p" in value:
        return 3
    if "720p" in value:
        return 2
    if "480p" in value:
        return 1
    return 0


def fallback_poster(title: str, seed: int = 0, landscape: bool = False) -> str:
    hue = sum(ord(c) * (i + 1) for i, c in enumerate(title)) % 360
    hue2 = (hue + 52 + seed * 11) % 360
    width, height = (1200, 675) if landscape else (600, 900)
    safe = html.escape(title[:52])
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">
    <defs>
      <linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="hsl({hue},68%,18%)"/><stop offset=".55" stop-color="hsl({hue2},62%,11%)"/><stop offset="1" stop-color="#040508"/></linearGradient>
      <radialGradient id="r"><stop stop-color="hsla({hue2},90%,68%,.42)"/><stop offset="1" stop-color="transparent"/></radialGradient>
      <filter id="b"><feGaussianBlur stdDeviation="42"/></filter>
    </defs>
    <rect width="100%" height="100%" fill="url(#g)"/>
    <circle cx="78%" cy="22%" r="24%" fill="url(#r)" filter="url(#b)"/>
    <path d="M0 {int(height*.82)} Q{int(width*.35)} {int(height*.56)} {width} {int(height*.70)}" fill="none" stroke="rgba(255,255,255,.09)" stroke-width="2"/>
    <text x="6%" y="12%" fill="rgba(255,255,255,.55)" font-family="Arial" font-size="18" letter-spacing="7">CINEMA HUB OG</text>
    <text x="6%" y="82%" fill="#fff" font-family="Arial" font-size="44" font-weight="700">{safe}</text>
    <text x="6%" y="88%" fill="rgba(255,255,255,.52)" font-family="Arial" font-size="15" letter-spacing="4">DISCOVER • VERIFY • RECEIVE</text>
    </svg>'''
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def group_key(title: str, year: str) -> str:
    return f"{slugify(title)}__{year or 'na'}"


def item_href(title: str, year: str, oid: str) -> str:
    return f"/title/{slugify(title)}--{year or 'na'}--{oid}"


def telegram_deep_link(movie_id: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=web_{movie_id}" if BOT_USERNAME and movie_id else ""


def normalize_db_item(doc: dict, idx: int = 0) -> dict:
    oid = str(doc.get("_id"))
    title = clean_title(doc.get("title") or doc.get("filename") or "Untitled")
    year = infer_year(doc)
    poster = safe_text(doc.get("poster_url") or doc.get("poster")) or fallback_poster(title, idx)
    backdrop = safe_text(doc.get("backdrop_url")) or safe_text(doc.get("backdrop")) or poster
    category = safe_text(doc.get("category")) or infer_category(doc)
    genres = doc.get("genres") or []
    if isinstance(genres, str):
        genres = [x.strip() for x in re.split(r"[,|]", genres) if x.strip()]
    rating = doc.get("rating") if doc.get("rating") is not None else doc.get("vote_average")
    try:
        rating = float(rating) if rating not in (None, "") else None
    except (TypeError, ValueError):
        rating = None
    return {
        "id": oid,
        "title": title,
        "slug": slugify(title),
        "url": item_href(title, year, oid),
        "year": year,
        "category": category,
        "quality": safe_text(doc.get("quality")),
        "language": safe_text(doc.get("language")),
        "season": safe_text(doc.get("season")),
        "size": safe_text(doc.get("size")) or format_size(doc.get("file_size")),
        "poster": poster,
        "backdrop": backdrop,
        "overview": re.sub(r"\s+", " ", safe_text(doc.get("overview"))).strip(),
        "rating": rating,
        "genres": genres,
        "metadata_provider": safe_text(doc.get("metadata_provider")),
        "provider_id": safe_text(doc.get("provider_id")),
        "provider_type": safe_text(doc.get("provider_type")),
        "telegram_url": telegram_deep_link(oid),
    }


async def mongo_ready() -> bool:
    if mongo is None:
        return False
    try:
        await mongo.admin.command("ping")
        return True
    except Exception:
        return False


async def fetch_movies(criteria: dict | None = None, limit: int = MAX_DB_SCAN) -> list[dict]:
    if not await mongo_ready():
        return []
    cursor = database().movies.find(criteria or {}).sort("indexed_at", DESCENDING).limit(limit)
    return await cursor.to_list(length=limit)


async def http_json(url: str, params: dict | None = None, attempts: int = 2) -> dict | list | None:
    if http_client is None:
        return None
    delay = 0.8
    for attempt in range(attempts):
        try:
            async with provider_semaphore:
                response = await http_client.get(url, params=params)
            if response.status_code == 404:
                return None
            if response.status_code == 429 and attempt + 1 < attempts:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            if attempt + 1 >= attempts:
                return None
            await asyncio.sleep(delay)
            delay *= 2
    return None


async def _provider_search_uncached(title: str, year: str, category: str) -> dict | None:
    is_tv = category in {"Web Series", "K-Drama", "Serials", "Cartoons", "WWE", "Anime"}
    if is_tv:
        data = await http_json(f"{TVMAZE_BASE}/search/shows", {"q": title}, attempts=3)
        if not isinstance(data, list) or not data:
            return None
        best, best_score = None, -1.0
        target_year = int(year) if year.isdigit() else None
        for row in data[:10]:
            show = row.get("show") or {}
            name = safe_text(show.get("name"))
            score = similarity_score(title, name)
            premiered = safe_text(show.get("premiered"))
            show_year = int(premiered[:4]) if premiered[:4].isdigit() else None
            if target_year and show_year:
                score += max(0.0, 0.20 - min(abs(show_year - target_year), 10) * 0.02)
            if category == "K-Drama" and safe_text(show.get("language")).lower() == "korean":
                score += 0.15
            if score > best_score:
                best_score, best = score, show
        if not best or best_score < 0.25:
            return None
        image = best.get("image") or {}
        premiered = safe_text(best.get("premiered"))
        summary = re.sub(r"<[^>]+>", " ", safe_text(best.get("summary")))
        summary = re.sub(r"\s+", " ", summary).strip()
        return {
            "provider": "tvmaze",
            "provider_id": best.get("id"),
            "provider_type": "show",
            "poster_url": image.get("original") or image.get("medium"),
            "backdrop_url": image.get("original") or image.get("medium"),
            "overview": summary,
            "rating": (best.get("rating") or {}).get("average"),
            "genres": best.get("genres") or [],
            "year": premiered[:4] if premiered else "",
            "language": safe_text(best.get("language")),
        }

    # Movies: keyless IMDb suggestion search provides real title artwork.
    q = re.sub(r"\s+", " ", title).strip()
    data = await http_json(f"{IMDB_SUGGESTION_BASE}/{quote(q.lower())}.json", attempts=2)
    items = (data or {}).get("d") if isinstance(data, dict) else None
    if not items:
        return None
    target_year = int(year) if year.isdigit() else None
    candidates = [x for x in items if safe_text(x.get("id")).startswith("tt")]
    if target_year:
        candidates.sort(key=lambda x: abs(int(x.get("y")) - target_year) if str(x.get("y", "")).isdigit() else 999)
    def movie_bonus(x: dict) -> int:
        qid = safe_text(x.get("qid")).lower()
        qtext = safe_text(x.get("q")).lower()
        return 5 if qid == "movie" or qtext in {"feature", "movie"} else 0
    candidates.sort(key=movie_bonus, reverse=True)
    best = candidates[0] if candidates else None
    if not best:
        return None
    image = best.get("i") or {}
    return {
        "provider": "imdb-suggestion",
        "provider_id": best.get("id"),
        "provider_type": "movie",
        "poster_url": image.get("imageUrl"),
        "backdrop_url": image.get("imageUrl"),
        "overview": "",
        "rating": None,
        "genres": [],
        "year": safe_text(best.get("y")),
        "imdb_title": safe_text(best.get("l")),
    }


async def _wikipedia_summary(title: str, year: str) -> dict | None:
    if http_client is None:
        return None
    candidates = [title]
    if year:
        candidates.append(f"{title} {year}")
    for candidate in candidates:
        slug = quote(candidate.replace(" ", "_"), safe="_")
        data = await http_json(f"{WIKI_SUMMARY_BASE}/{slug}", attempts=2)
        if isinstance(data, dict) and data.get("type") != "https://mediawiki.org/wiki/HyperSwitch/errors/unknown_title":
            image = data.get("originalimage") or {}
            thumb = data.get("thumbnail") or {}
            return {
                "overview": safe_text((data.get("extract") or "").strip()),
                "poster_url": safe_text(image.get("source") or thumb.get("source")),
                "wikipedia_url": safe_text((data.get("content_urls") or {}).get("desktop", {}).get("page")),
            }
    return None


async def provider_search(title: str, year: str | None, category: str) -> dict | None:
    key = f"{category}:{normalize_compare(title)}:{year or ''}"
    now = time.time()
    cached = _provider_cache.get(key)
    if cached and now - cached[0] < PROVIDER_CACHE_TTL:
        return cached[1]
    inflight = _provider_inflight.get(key)
    if inflight:
        try:
            return await inflight
        except Exception:
            return None

    async def work():
        result = await _provider_search_uncached(title, year or "", category)
        if result and result.get("provider") == "imdb-suggestion" and not result.get("overview"):
            wiki = await _wikipedia_summary(title, year or "")
            if wiki:
                if wiki.get("overview"):
                    result["overview"] = wiki["overview"]
                if wiki.get("wikipedia_url"):
                    result["wikipedia_url"] = wiki["wikipedia_url"]
                if not result.get("poster_url") and wiki.get("poster_url"):
                    result["poster_url"] = wiki["poster_url"]
        return result

    task = asyncio.create_task(work())
    _provider_inflight[key] = task
    try:
        result = await task
    finally:
        _provider_inflight.pop(key, None)
    _provider_cache[key] = (now, result)
    return result


async def hydrate_doc(doc: dict, idx: int = 0) -> dict:
    item = normalize_db_item(doc, idx)
    needs_metadata = item["poster"].startswith("data:") or not item["overview"] or not item["metadata_provider"]
    if not needs_metadata:
        return item
    result = await provider_search(item["title"], item["year"], item["category"])
    if not result or mongo is None:
        return item
    update = {
        "metadata_provider": result.get("provider"),
        "provider_id": result.get("provider_id"),
        "provider_type": result.get("provider_type"),
        "poster_url": result.get("poster_url") or doc.get("poster_url"),
        "backdrop_url": result.get("backdrop_url") or doc.get("backdrop_url"),
        "overview": result.get("overview") or doc.get("overview") or "",
        "rating": result.get("rating") if result.get("rating") is not None else doc.get("rating"),
        "genres": result.get("genres") or doc.get("genres") or [],
        "metadata_updated_at": datetime.now(timezone.utc),
    }
    if result.get("year") and not doc.get("year"):
        update["year"] = result["year"]
    if result.get("language") and not doc.get("language"):
        update["language"] = result["language"]
    if result.get("wikipedia_url"):
        update["wikipedia_url"] = result["wikipedia_url"]
    update = {k: v for k, v in update.items() if v is not None}
    try:
        await database().movies.update_one({"_id": doc["_id"]}, {"$set": update})
        doc.update(update)
    except Exception:
        pass
    return normalize_db_item(doc, idx)


async def unique_catalog_docs(docs: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for doc in docs:
        title = clean_title(doc.get("title") or doc.get("filename") or "Untitled")
        year = infer_year(doc)
        key = group_key(title, year)
        current = groups.get(key)
        if current is None:
            groups[key] = doc
            continue
        # Prefer a record with artwork, then highest available quality, then latest indexed.
        current_has_art = bool(safe_text(current.get("poster_url") or current.get("poster")))
        candidate_has_art = bool(safe_text(doc.get("poster_url") or doc.get("poster")))
        if (candidate_has_art, quality_score(doc), safe_text(doc.get("indexed_at"))) > (current_has_art, quality_score(current), safe_text(current.get("indexed_at"))):
            groups[key] = doc
    return list(groups.values())


async def catalog_items(category: str, year: str, query: str, limit: int = 24) -> list[dict]:
    docs = await fetch_movies({}, MAX_DB_SCAN)
    unique_docs = await unique_catalog_docs(docs)
    q = query.strip().lower()
    selected: list[dict] = []
    for doc in unique_docs:
        item = normalize_db_item(doc)
        if category != "All" and item["category"] != category:
            continue
        if year != "All" and item["year"] != year:
            continue
        hay = " ".join([
            item["title"], safe_text(doc.get("filename")), safe_text(doc.get("caption")),
            safe_text(doc.get("language")), safe_text(doc.get("quality")), safe_text(doc.get("genre")),
        ]).lower()
        if q and q not in hay:
            continue
        selected.append(doc)
        if len(selected) >= limit:
            break
    # Hydrate every visible card, but capped by a small provider semaphore and cached for 24h.
    return await asyncio.gather(*(hydrate_doc(doc, i) for i, doc in enumerate(selected))) if selected else []


async def resolve_title(slug_path: str) -> list[dict]:
    docs = await fetch_movies({}, MAX_DB_SCAN)
    parts = slug_path.rsplit("--", 2)
    if len(parts) == 3 and ObjectId.is_valid(parts[2]):
        target_id = parts[2]
        first = next((d for d in docs if str(d.get("_id")) == target_id), None)
        if first is None:
            return []
        title = clean_title(first.get("title") or first.get("filename"))
        year = infer_year(first)
        key = group_key(title, year)
    else:
        key = parts[0]
    matches = []
    for doc in docs:
        item = normalize_db_item(doc)
        if group_key(item["title"], item["year"]) == key:
            matches.append(doc)
    if not matches:
        slug = slug_path.split("--", 1)[0]
        matches = [doc for doc in docs if normalize_db_item(doc)["slug"] == slug]
    return matches


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(8.0, connect=4.0),
        headers={"User-Agent": "CinemaHUBOG/3.0 (metadata client; TVmaze credited)"},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    )
    yield
    if http_client is not None:
        await http_client.aclose()
        http_client = None
    if mongo is not None:
        mongo.close()


BASE_DIR = Path(__file__).resolve().parents[1]
app = FastAPI(title=SITE_NAME, docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    docs = await unique_catalog_docs(await fetch_movies({}, MAX_DB_SCAN))
    featured = await asyncio.gather(*(hydrate_doc(d, i) for i, d in enumerate(docs[:6]))) if docs else []
    return templates.TemplateResponse("landing.html", {
        "request": request,
        "site_name": SITE_NAME,
        "tagline": SITE_TAGLINE,
        "bot_username": BOT_USERNAME,
        "developer_name": DEVELOPER_NAME,
        "developer_username": DEVELOPER_USERNAME,
        "telegram_channel": TELEGRAM_CHANNEL,
        "telegram_group": TELEGRAM_GROUP,
        "visual_posters": featured,
    })


@app.get("/cinema", response_class=HTMLResponse)
async def cinema(request: Request):
    return templates.TemplateResponse("cinema.html", {
        "request": request,
        "site_name": SITE_NAME,
        "tagline": SITE_TAGLINE,
    })


@app.get("/title/{slug_path:path}", response_class=HTMLResponse)
async def title_page(request: Request, slug_path: str):
    return templates.TemplateResponse("title.html", {
        "request": request,
        "site_name": SITE_NAME,
        "slug": slug_path,
    })


@app.get("/poster-fallback")
async def poster_fallback(title: str = Query("Cinema HUB OG"), landscape: bool = False):
    data = fallback_poster(title, 3, landscape)
    return Response(
        content=base64.b64decode(data.split(",", 1)[1]),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@app.get("/api/home")
async def api_home():
    items = await catalog_items("All", "All", "", 24)
    return {"site_name": SITE_NAME, "featured": items[:6], "recent": items[6:18], "categories": CATEGORY_NAMES}


@app.get("/api/search")
async def api_search(q: str = Query("", min_length=1), category: str = "All", year: str = "All", limit: int = Query(24, ge=1, le=60)):
    q = q.strip()
    if not q:
        return {"results": [], "query": "", "count": 0}
    results = await catalog_items(category, year, q, limit)
    return {"results": results, "query": q, "count": len(results)}


@app.get("/api/catalog")
async def api_catalog(category: str = "All", year: str = "All", limit: int = Query(24, ge=1, le=60)):
    results = await catalog_items(category, year, "", limit)
    return {"results": results, "categories": CATEGORY_NAMES}


@app.get("/api/title/{slug_path:path}")
async def api_title(slug_path: str):
    matches = await resolve_title(slug_path)
    if not matches:
        raise HTTPException(status_code=404, detail="Title not found")
    base = await hydrate_doc(matches[0])
    versions = [normalize_db_item(d, i) for i, d in enumerate(sorted(matches, key=lambda x: (-quality_score(x), safe_text(x.get("language")), safe_text(x.get("season")))))]
    return {
        "title": base,
        "versions": versions,
        "version_count": len(versions),
        "bot_configured": bool(BOT_USERNAME),
    }


@app.get("/api/years")
async def api_years():
    docs = await fetch_movies({}, MAX_DB_SCAN)
    years = sorted({infer_year(doc) for doc in docs if infer_year(doc)}, reverse=True)
    return {"years": years}


@app.get("/api/health")
async def health():
    global _health_cache
    now = time.time()
    if _health_cache and now - _health_cache[0] < HEALTH_CACHE_TTL:
        return _health_cache[1]
    ready = await mongo_ready()
    count = None
    if ready:
        try:
            count = await database().movies.count_documents({})
        except Exception:
            count = None
    payload = {
        "status": "ok" if ready else "degraded",
        "database_ready": ready,
        "movie_count": count,
        "metadata": {
            "tvmaze": {"enabled": True, "key_required": False},
            "movie_artwork": {"provider": "IMDb search suggestions", "key_required": False, "best_effort": True},
            "movie_overview": {"provider": "Wikipedia REST summary", "key_required": False, "best_effort": True},
        },
        "bot_configured": bool(BOT_USERNAME),
    }
    _health_cache = (now, payload)
    return payload


@app.get("/go/{movie_id}")
async def go_telegram(movie_id: str):
    if not BOT_USERNAME:
        return JSONResponse({"error": "BOT_USERNAME is not configured"}, status_code=503)
    if not ObjectId.is_valid(movie_id):
        raise HTTPException(status_code=404, detail="Invalid title id")
    if mongo is None or not await mongo_ready():
        raise HTTPException(status_code=503, detail="Database unavailable")
    doc = await database().movies.find_one({"_id": ObjectId(movie_id)}, {"_id": 1})
    if not doc:
        raise HTTPException(status_code=404, detail="Title not found")
    return RedirectResponse(telegram_deep_link(movie_id), status_code=307)
