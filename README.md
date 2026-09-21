# Whatomate Django backend

This repo is the **Django** API (`socialmedia-marketing-backend-django`). Pair it with the Vue app (`frontend/`). You do **not** need Go for normal product work.

| Who | What works |
|---|---|
| **Django + Vue only** | Login, users/roles, WhatsApp accounts and credentials, inbox chat, campaigns, chatbot, settings, billing, analytics, reports, catalogs, WhatsApp Flows |
| **Go sidecar** (`:8080`) | Live calling only: call button, IVR builder, call logs, call transfers, WebRTC |

Django returns `501` if a calling route is hit on `:8000`. That is expected. Calling is a different process.

---

## What you need

- Python 3.12+ (3.14 is fine)
- Redis (local Docker or any Redis URL)
- Postgres: **Neon** (`DATABASE_URL`) **or** local Docker Postgres
- Node.js only if you also run Vue in this workspace
- Docker Desktop only if you use the compose file in this folder (Postgres + Redis)

---

## 1. Clone and virtualenv

```bash
cd socialmedia-marketing-backend-django
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

macOS / Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Never commit `.env`. It is gitignored.

---

## 2. Add credentials (`.env`)

Open `.env` and fill these in order.

### 2.1 Django secrets

```env
DJANGO_SECRET_KEY=change-me-in-production
DJANGO_DEBUG=true
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
```

### 2.2 JWT (must match Go if you also run calling)

```env
JWT_SECRET=your-super-secret-jwt-key-change-in-production
JWT_ACCESS_EXPIRY_MINS=15
JWT_REFRESH_EXPIRY_DAYS=1
COOKIE_SECURE=false
COOKIE_DOMAIN=
CORS_ALLOWED_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
```

If a teammate runs the Go calling sidecar, copy the same `JWT_SECRET` into Go `config.toml` `[jwt] secret`. Django login cookies then work on calling APIs.

### 2.3 Database — Neon (recommended)

1. Open [Neon Console](https://console.neon.tech) → your project → **Connect**.
2. Branch `production` (or your branch), database `neondb`, role `neondb_owner`.
3. Copy the connection string.
4. Paste it into `.env` as **one line** (no quotes):

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST/neondb?sslmode=require
```

`DATABASE_URL` **overrides** `POSTGRES_*`. A Neon **pooler** hostname (`-pooler.`) is OK; Django uses the direct compute host for migrations.

To move later to another Postgres: change `DATABASE_URL`, then either run `bootstrap` on an empty database (schema + admin user) or restore a dump (schema + rows). See [commands](#6-command-reference).

### 2.4 Database — local Docker instead of Neon

Leave `DATABASE_URL` unset (or delete that line). Keep:

```env
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_USER=whatomate
POSTGRES_PASSWORD=whatomate
POSTGRES_DB=whatomate
```

Then:

```bash
docker compose up -d
```

Wait until `whatomate_db` and `whatomate_redis` are healthy.

### 2.5 Redis

```env
REDIS_URL=redis://127.0.0.1:6379/0
```

If you skipped Docker because you use Neon, still run Redis:

```bash
docker compose up -d redis
```

or install Redis locally.

### 2.6 WhatsApp / Meta (app-level fallback)

These are optional fallbacks. Org settings and **Settings → WhatsApp accounts** override them per number.

```env
ENCRYPTION_KEY=
WHATSAPP_WEBHOOK_VERIFY_TOKEN=pick-a-long-random-string
WHATSAPP_API_VERSION=v24.0
META_APP_ID=
META_APP_SECRET=
META_CONFIG_ID=
STORAGE_LOCAL_PATH=./media
CALLING_AUDIO_DIR=./audio
```

`ENCRYPTION_KEY` empty = store tokens in plaintext (same as Go dev). Set a 32+ character key in production.

Webhook URL you give Meta:

`https://YOUR-PUBLIC-HOST/api/webhook`

Verify token must match `WHATSAPP_WEBHOOK_VERIFY_TOKEN`.

For a shared Meta test app, one machine runs `cloudflared tunnel --url http://127.0.0.1:8000` and that hostname is what Meta calls. Put the same host in every developer’s `.env` as `PUBLIC_API_URL` so Settings → WhatsApp shows the shared callback, not `localhost`. Keep Vue on `http://localhost:3000` and leave `VITE_API_URL` empty (login cookies will not work if the frontend calls the tunnel host). Do not paste `http://localhost:3000/api/webhook` into Meta.

### 2.7 Billing (Dodo) — optional until you test checkout

```env
PUBLIC_APP_URL=http://localhost:3000
DODO_PAYMENTS_API_KEY=
DODO_WEBHOOK_SECRET=
DODO_ENVIRONMENT=test_mode
DODO_RETURN_URL=http://localhost:3000/settings/billing
DODO_PRODUCT_PRO=
DODO_PRODUCT_BUSINESS=
DODO_ADDON_SEAT=
DODO_METER_MESSAGE_SENT=message.sent
DODO_METER_AI_COMPLETION=ai.completion
DODO_METER_CAMPAIGN_RECIPIENT=campaign.recipient
```

Dodo webhook path: `POST /api/webhooks/dodo`. Keep this endpoint; do not add a second billing webhook route.

#### Approved billing matrix

Prices below are represented in cents in the API and seed data. Yearly prices are optional but currently seeded and documented here.

| Plan | Monthly | Yearly | Included WhatsApp accounts | Message quotas (`message.sent` / `ai.completion` / `campaign.recipient`) |
|---|---:|---:|---:|---:|
| Free | $0 | $0 | 1 | 100 / 0 / 0 |
| Pro | $29 | $290 | 2 | 5,000 / 1,000 / 5,000 |
| Business | $99 | $990 | 10 | 25,000 / 10,000 / 25,000 |

Feature flags are seeded as follows:

- Free: all approved flags disabled.
- Pro: `campaigns`, `ai`, `calling`, `extra_wa_accounts`, and `teams_basic` enabled; the remaining approved flags disabled.
- Business: all approved flags enabled.

The billing API retains monthly/yearly checkout, `cancel_at_period_end`, seat and WhatsApp-account downgrade blockers, webhook signature tolerance and idempotency, `BillingPayment` and refund events, and plan responses containing prices, extra-seat prices, included WhatsApp accounts, and checkout readiness.

---

## 3. Create tables and the first admin

From this folder, with venv active:

```bash
python manage.py bootstrap
```

That runs `migrate` then `seed_admin`. On an empty Postgres it:

1. Creates Django/billing tables
2. Creates product tables from `sql/product_schema.sql` (same schema Go used)
3. Seeds permissions and admin/manager/agent roles
4. Creates the login user
5. Seeds billing plans

Check tables:

```bash
python manage.py check_schema
```

You should see: `all expected product tables are present`.

---

## 4. Run the API

```bash
daphne -b 127.0.0.1 -p 8000 config.asgi:application
```

- Health: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)
- Ready (DB + product tables): [http://127.0.0.1:8000/ready](http://127.0.0.1:8000/ready)

Leave this terminal open.

---

## 5. Run Vue (separate frontend folder)

From the Vue project:

```bash
npm install
npm run dev
```

Vite should be on [http://localhost:3000](http://localhost:3000). Dev proxy:

- `/api` and `/ws` → Django `:8000`
- calling `/api/calls*`, `/api/ivr-flows*`, `/api/call-logs*`, `/api/call-transfers*`, `/ws-go` → Go `:8080` (only if that process is running)

### Login

| Field | Value |
|---|---|
| Email | `admin@admin.com` |
| Password | `admin` |

Reset that password back to `admin`:

```bash
python manage.py seed_admin --reset
```

### Add users (in the app)

1. Log in as admin
2. **Settings → Users** → create user (email, name, role)
3. **Settings → Roles / Teams** as needed
4. To deactivate: open the user → disable / delete according to the screen

### Add a WhatsApp number and credentials (in the app)

1. **Settings → WhatsApp accounts** (or **Accounts**)
2. Add account: phone number ID, WABA ID, access token (Meta)
3. Save → **Test** / **Subscribe** / **Register** if the UI shows those actions
4. Embedded signup uses `META_APP_ID`, `META_APP_SECRET`, `META_CONFIG_ID` when the org has not set its own
5. Point Meta’s webhook at Django `/api/webhook` with the verify token from `.env`

You can chat, change settings, and open analytics/reports **without Go**.

### Calling (Go only — skip unless you have the Go sidecar)

Live calls, IVR, call logs, and call transfers need the Go app on `:8080` with the **same** Postgres, Redis, and `JWT_SECRET`. This Django repo does not include that binary.

---

## 6. Command reference

Run these from this folder with `.venv` active.

| Command | What it does |
|---|---|
| `python manage.py bootstrap` | `migrate` + `seed_admin` (first-time or empty DB) |
| `python manage.py migrate` | Django + billing tables only |
| `python manage.py seed_admin` | Product schema if missing, RBAC, `admin@admin.com` |
| `python manage.py seed_admin --reset` | Reset admin password to `admin` |
| `python manage.py check_schema` | Confirm product tables exist |
| `python manage.py dump_database` | `pg_dump` to `backups/whatomate.dump` (needs `pg_dump` on PATH) |
| `python manage.py restore_database backups/whatomate.dump` | Restore that dump onto current `DATABASE_URL` |
| `python manage.py seed_billing` | Billing plans (also called from `seed_admin`) |
| `daphne -b 127.0.0.1 -p 8000 config.asgi:application` | Run API + WebSocket |

Move to a **new empty** Postgres:

```bash
# .env → new DATABASE_URL
python manage.py bootstrap
```

Move **data** as well:

```bash
python manage.py dump_database
# point DATABASE_URL at the new server
python manage.py restore_database backups/whatomate.dump
```

---

## 7. If something fails

Try these in order.

| Symptom | Fix |
|---|---|
| `No module named 'django'` | Activate `.venv` and `pip install -r requirements.txt` |
| Login 503 / “tables are missing” | `python manage.py bootstrap` |
| `users` table missing / `check_schema` fails | `DATABASE_URL` wrong, or Neon compute asleep — retry; confirm `.env` is in **this** folder |
| Neon migrate / SSL errors | Keep `?sslmode=require`. Do not put the password in git |
| Redis errors, Celery, WebSocket | Start Redis (`docker compose up -d redis`) |
| Vue login works but API CORS | `CORS_ALLOWED_ORIGINS` must include `http://localhost:3000` |
| Calling pages empty / 501 | Expected without Go. Product chat/settings still use Django |
| `pg_dump` not found | Install PostgreSQL client tools, or skip dump until you migrate hosts |
| Admin password unknown | `python manage.py seed_admin --reset` |

Do **not** commit `.env`, dumps under `backups/`, or real Neon passwords.

---

## 8. Errors outside this README

If the error is **not** in the table above (new traceback, Meta API, Docker, OS, IDE, or something this repo does not own), use an AI coding assistant in the project folder rather than guessing:

- **Cursor**
- **VS Code Copilot**
- **Claude Code**
- **Google Antigravity**
- Other VS Code AI extensions such as **Kilo Code**

Paste the **full traceback**, the command you ran, and whether `DATABASE_URL` is Neon or Docker. Do not paste production passwords into a public chat.

Calling/IVR/WebRTC bugs belong in the **Go** repo, not here.

---

## Layout

```text
config/          settings, URLs, ASGI
apps/            accounts, billing, whatsapp, contacts, messaging, campaigns,
                 chatbot, catalogs, analytics, webhooks, realtime, calling (501 facade)
sql/             frozen GORM product schema + extra tables
docker-compose.yml   local Postgres + Redis only (not the Django process)
```

Never invent a new product schema. Vue’s `/api` JSON envelope stays `{ status, data, message, error }`. Cookies: `whm_access`, `whm_csrf`. Headers: `X-Organization-ID`, `X-CSRF-Token`, `X-API-Key`.
