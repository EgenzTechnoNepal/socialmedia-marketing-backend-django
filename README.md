# socialmedia-marketing-backend-django

Django REST Framework API (ASGI via Daphne). Login, signup, inbox, campaigns, chatbot, settings, billing, and Meta webhooks run here.

Voice calling, IVR, outbound calls, call logs, and call transfers stay on the **Go sidecar** (`:8080`). Django returns `501` if those routes are hit directly.

## Hybrid runtime (Vue + Django + Go)

Run all three against the same Postgres and Redis:

1. Postgres + Redis
2. Django on `:8000` (this repo)
3. Go `whatomate` on `:8080` (calling sidecar only)
4. Vue Vite on `:3000` (proxies product `/api` here, calling `/api/calls*` `/api/ivr-flows*` `/api/call-logs*` `/api/call-transfers*` to Go, inbox `/ws` here, live-call `/ws-go` to Go)

`JWT_SECRET` in `.env` **must match** Go `config.toml` `[jwt] secret` so a Django login cookie works on Go calling APIs.

## Setup

Postgres and Redis are required. From this folder:

```bash
docker compose up -d
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python manage.py migrate
python manage.py seed_admin
daphne -b 127.0.0.1 -p 8000 config.asgi:application
```

If Docker says `whatomate_db` is already running, skip `docker compose up` and use that database.

API: [http://127.0.0.1:8000](http://127.0.0.1:8000)

Login: `admin@admin.com` / `admin` (after `seed_admin`, when the `users` table already exists).

`seed_admin` cannot create tables. Product tables come from the shared team database. If login says the database is not ready, ask the lead for Postgres access.

Chat, login, and settings work with Django + Vue only. Calling pages also need Go on `:8080`.

Never commit `.env`.
