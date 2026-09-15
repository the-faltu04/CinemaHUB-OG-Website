# Cinema HUB OG — A-to-Z Final Website Build

## What this build fixes
- Dedicated landing / brand page at `/`.
- `ENTER THE CINEMA` opens `/cinema` in a new tab.
- Dedicated catalogue page with animated search, live suggestions, category filters and year filters.
- One catalogue card per title rather than one card per file version.
- Cinematic title detail page at `/title/...` with real provider artwork when available.
- Exact-version Telegram deep links: `web_<mongo_object_id>`.
- Existing bot Force-Subscribe + Linkpays + Telegram database delivery flow remains the verification/delivery engine.
- TV metadata: public TVmaze API, no API key required. TVmaze documents public search, images, caching guidance and a rate limit of at least 20 calls per 10 seconds; the app uses capped concurrency, retry/backoff and a 24-hour in-process cache.
- Movie artwork: best-effort keyless IMDb search suggestions. Movie overview: best-effort Wikipedia REST summary. These are metadata/artwork helpers only and are not media sources.
- Graceful fallbacks: generated poster artwork, offline/empty states, invalid title handling, missing BOT_USERNAME handling.
- Mobile-first responsive layout with reduced-motion support.

## Render
Root Directory: blank
Build Command:
`python -m pip install -r requirements.txt`

Start Command:
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## Website environment
Required:
- `MONGO_URI`
- `DB_NAME`
- `BOT_USERNAME`

Optional branding:
- `SITE_NAME`
- `SITE_TAGLINE`
- `DEVELOPER_NAME`
- `DEVELOPER_USERNAME`
- `TELEGRAM_CHANNEL`
- `TELEGRAM_GROUP`

## Secrets
Do not commit `BOT_TOKEN`, `API_ID`, `API_HASH`, `SESSION_STRING`, `LINKPAYS_API`, or database credentials to GitHub.

## Deployment architecture
Telegram Database Channel → existing AutoIndexer → MongoDB `movies` → Cinema HUB OG website → Telegram deep link → F-Sub → Linkpays → exact-file delivery.

The catalogue and bot should only distribute media the operator has the rights or permission to distribute.
