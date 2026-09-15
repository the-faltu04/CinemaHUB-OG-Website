# Cinema HUB OG — A-to-Z QA Report

## Scope
The website build and website-to-bot bridge were reviewed together: frontend structure, responsive styling, catalog logic, metadata provider fallbacks, MongoDB access paths, Telegram deep-link handoff, verification-flow preservation, and deployment files.

## Static checks
- `app/main.py` Python compile: PASS
- `bot_patched.py` Python compile: PASS
- `landing.js` syntax check: PASS
- `cinema.js` syntax check: PASS
- `title.js` syntax check: PASS
- CSS brace balance: PASS
- HTML document structure: PASS
- No `.pyc` / `__pycache__` included in distribution: PASS
- No TMDB/API-key dependency remains in the website bundle: PASS

## Application smoke test
A local isolated FastAPI test used stubbed MongoDB/`ObjectId` modules and sample indexed titles.

Verified routes:
- `/`
- `/cinema`
- `/title/<slug>`
- `/poster-fallback`
- `/api/home`
- `/api/search`
- `/api/catalog`
- `/api/title/<slug>`
- `/api/years`
- `/api/health`

Verified behavior:
- duplicate file versions collapse into one catalogue card;
- title detail resolves multiple versions;
- poster fallback always exists;
- Telegram deep-link payload is generated from the exact Mongo media ID;
- invalid/missing records return controlled HTTP errors instead of crashing the page.

## Metadata layer
- TV/web categories use TVmaze public search with no API key.
- TVmaze requests use connection reuse, capped concurrency and 429 backoff.
- Metadata is cached for 24 hours in-process and written back to the Mongo movie document when available.
- Movie artwork uses best-effort keyless IMDb search suggestions.
- Movie overview uses best-effort Wikipedia REST summaries.
- Provider failure never prevents the title from rendering; the generated poster is used as a fallback.

## Frontend QA
- Dedicated landing page and dedicated catalogue page.
- `ENTER THE CINEMA` opens `/cinema` in a new tab.
- Real artwork cards when provider metadata is available.
- Animated 3D-style particle/orbit hero background built without an external rendering dependency.
- Poster wall parallax motion and hover depth.
- Animated search scan-line + live suggestion dropdown.
- Search, category and year filtering.
- Responsive desktop/tablet/mobile layouts.
- Reduced-motion support.
- Title page supports poster, backdrop, synopsis, year, rating, language, genres, versions and direct Telegram buttons.

## Telegram bridge QA
The patched bot preserves the existing verification engine. Website requests use:
`https://t.me/<BOT_USERNAME>?start=web_<MONGO_OBJECT_ID>`

The exact selected media ID is preserved through Force-Subscribe and Linkpays token storage. After verification, the existing `deliver()` function is called directly; the website flow does not redirect into the request group.

## Deployment note
A true production test of Telegram, MongoDB, Linkpays, Render and provider network calls requires the user's deployment credentials and external services. Secrets are intentionally not embedded in the ZIP.
