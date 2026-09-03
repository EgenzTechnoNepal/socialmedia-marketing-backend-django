# socialmedia-marketing-backend-django

Django REST Framework API (ASGI via Daphne, background jobs via Celery).

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Postgres and Redis should already be running. Then:

```bash
daphne -b 127.0.0.1 -p 8000 config.asgi:application
```

The API listens on [http://127.0.0.1:8000](http://127.0.0.1:8000). Copy `.env.example` to `.env` and fill in local values; never commit `.env`.
