# Cinema HUB OG — Render Deploy Notes

## Website service
Root Directory: blank

Build Command:
```bash
python -m pip install -r requirements.txt
```

Start Command:
```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Website environment
Required:
```text
MONGO_URI
DB_NAME
BOT_USERNAME
```

Use the exact variable name `BOT_USERNAME`.

Optional:
```text
SITE_NAME
SITE_TAGLINE
DEVELOPER_NAME
DEVELOPER_USERNAME
TELEGRAM_CHANNEL
TELEGRAM_GROUP
```

## Bot deployment
Keep the user's existing production bot environment values for the separate bot service. The website bridge only requires `BOT_USERNAME` on the website side, while the bot itself still needs its own Telegram/Mongo/Linkpays/session settings.

## GitHub root
```text
app/
static/
templates/
bot_reference/
.env.example
BOT_PATCH.md
DEPLOY_NOTES.md
QA_REPORT.md
README.md
bot_patched.py
render.yaml
requirements.txt
```
