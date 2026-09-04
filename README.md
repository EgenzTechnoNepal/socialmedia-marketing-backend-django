# socialmedia-marketing-backend-django

Django REST Framework API (ASGI via Daphne). **Do not run Go.** Voice calling stays on a separate Go sidecar that only the lead runs.

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

`seed_admin` cannot create tables. Product tables come from the shared team database. If login says the database is not ready, ask the lead for Postgres access — do not start Go.

Never commit `.env`.
