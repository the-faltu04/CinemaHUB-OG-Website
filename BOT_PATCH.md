# Cinema HUB OG — website-to-bot bridge

The website uses Telegram deep links in the form:
`https://t.me/<BOT_USERNAME>?start=web_<MONGO_OBJECT_ID>`

Website-originated requests keep the exact Mongo media ID through Force-Subscribe and Linkpays. After verification, the existing delivery function copies the selected indexed Telegram database message directly. The website never redirects the visitor into the request group for this flow.

The existing expiry/deletion settings remain controlled by the bot deployment environment (`DELETE_AFTER_SECONDS`, `EXPIRY_NOTICE`). Keep `BOT_TOKEN`, Telegram API credentials, MongoDB credentials, Linkpays credentials and session strings only in Render environment variables.
