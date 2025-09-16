
# instruction.md
Product & Engineering Specification — Telegram Split Bot + Admin Web (MVP)

**Status:** Finalized for MVP  
**Owner:** Admin-only Web + Telegram Group Bot  
**Hosting:** Local (self-hosted), Dockerized.  
**Date:** 2025‑09‑15

---

## 1) Executive Summary
We’re building a **Telegram-first group expense split bot** with an **admin-only web console**. Users add expenses directly in a Telegram group (text or receipt photo). An on-prem LLM extracts the structured data; amounts are converted and **FX rates are fixed at input time**. Every expense requires a quick **inline approval** before it’s saved. Admin gets a web console for global management, editing, exports, and analytics.

**Why this wins:** frictionless group capture inside Telegram, AI-powered extraction, robust currency handling, simple approvals, no external cloud dependencies for the LLM or receipt storage.

---

## 2) Goals and Non‑Goals
### Goals
- Fast, low-friction expense entry inside Telegram (text + receipts).
- Accurate, reviewable AI extraction with **editable** approval card.
- Multi-currency with **Yahoo Finance** as primary FX source; **ECB/Frankfurter** as fallback.
- **FX rate fixed at input time** (i.e., message time) and stored per expense.
- Period close with debt simplification and export to **Excel by default**.
- Admin-only responsive web app for cross-group management.
- Local hosting, containers, simple baseline security, audit logs.

### Non‑Goals (MVP)
- No live money transfer.
- No per-user FX conversion fee logic (deferred).
- No complex role model in groups (any member can add/edit via bot; admin-only web).
- No advanced encryption regime (note for future hardening).
- No aggressive rate limiting (only scaffolding).

---

## 3) High-Level Architecture
- **Telegram Bot Service (Python)** — Webhook handler, inline keyboards, LLM/OCR orchestration, FX fetcher, writes to API.
- **API Service (Flask, Python)** — REST endpoints, business logic, auth for web admin, audit logging.
- **Worker (Celery, Python)** — OCR and long-running tasks; optional LLM proxy calls if async.
- **Database (PostgreSQL)** — Core data (users, groups, expenses, rates, settlements, audit).
- **Cache/Queue (Redis)** — FX caching, background jobs, future rate-limit counters.
- **Web Admin (Flask templates or API + HTMX/Alpine; Tailwind with RTL support)** — Admin-only, mobile friendly.
- **Reverse Proxy (nginx)** — TLS termination & routing.
- **Local Storage** — Receipts stored on disk per group; no cloud for MVP.
- **External** — Yahoo Finance (primary FX), Frankfurter/ECB (fallback).  
- **LLM (Local, e.g., Ollama)** — Hosted on a separate machine; exposed via HTTP/gRPC.

All services run in Docker (docker-compose).

---

## 4) Core Decisions (Final)
- **Split types:** Equal by default; allow explicit per-expense override through message text (weights later).
- **Debt simplification:** On period close.
- **Expense approval:** Mandatory via inline keyboard before persist.
- **Participants default:** All group members on each expense (can edit on approval card).
- **FX fixing:** At **message/ingest time** (UTC timestamp of bot receive). Persist FX rate per expense.
- **FX sources:** Primary **Yahoo Finance**; Fallback **Frankfurter/ECB**; 1h cache in Redis.
- **Conversion failure behavior:** If both sources fail → mark expense “awaiting_rate”; show actionable error and retry job. No save without a rate unless expense currency == group base.
- **Export default:** **Excel (.xlsx)**; also support CSV on demand. (PDF optional later)
- **Languages:** Hebrew primary, English secondary (LLM prompt supports both).
- **AI confidence threshold:** **0.75**. Below threshold → forced edit before “Approve” enabled.
- **Receipts:** Stored **locally**; path per group; sanitized filenames; basic MIME validation.
- **Categories (10):** 
  1) Food & Drinks  
  2) Transport  
  3) Lodging  
  4) Groceries  
  5) Entertainment  
  6) Utilities  
  7) Shopping  
  8) Fees & Services  
  9) Health  
  10) Miscellaneous

---

## 5) User Flows

### 5.1 Admin Onboarding (one-time)
1. Admin opens Web Console → Registers/Logs in (simple session auth).
2. Creates **Group**: name, description, base currency, period close policy.
3. Configures **LLM** endpoint (base URL, token), **OCR** (local Tesseract), **FX** (Yahoo + ECB fallback, cache TTL).
4. Retrieves Bot token & webhook instructions; adds bot to Telegram group.

### 5.2 Add Bot to Telegram Group
- Bot is added → `/start` in group triggers wizard: link Telegram chat to a system group, confirm base currency and defaults, list available commands.

### 5.3 Add Expense — Free Text
1. Member posts: “שילמתי 120₪ על אוכל לכולם”.
2. Bot sends text to LLM with context (group settings, members).
3. LLM returns JSON (amount, currency, date_hint, category, description, participants, split, confidence).
4. Bot calculates FX (fix at ingest time) & amount_in_base.
5. Bot replies with **Approval Card** (inline keyboard):
   - Approve ✅ | Cancel ❌
   - Edit: Amount | Currency | Date | Category | Participants | Split | Description
6. On Approve → API persists expense with fixed `fx_rate` and `fx_fixed_at` set to message time.

### 5.4 Add Expense — Receipt
1. Member uploads a photo/PDF receipt to group.
2. Bot runs OCR (local Tesseract) → passes to LLM for normalization/classification.
3. Same Approval Card. Edits allowed; then Approve saves expense.

### 5.5 Summary
- `/summary [from to]` returns totals, category breakdown, and per‑member balances for the range. Buttons: Change Range, Export (Excel/CSV), Open in Web.

### 5.6 Close Period
- `/close [from to]` (or via Web): compute net debts, **simplify** graph, produce settlement suggestions (“who pays who and how much”). Mark period locked; allow admin re‑open.

### 5.7 Web Admin
- **Dashboard:** KPIs (total, per member, top categories, FX mix).
- **Expenses:** searchable table; edit/delete; view audit trail; attach/view receipts.
- **Members:** list and sync from Telegram; remove or merge duplicates.
- **Settings:** group base currency, FX policy, categories.
- **Exports:** Excel default; CSV optional.
- **Analytics:** basic trend charts (later OK).

---

## 6) Telegram UX

### Commands
- `/start` — setup/link group.
- `/add` — guides user to send text or receipt.
- `/receipt` — prompts for image/PDF.
- `/summary [from to]` — period summary.
- `/close [from to]` — close and simplify debts.

### Inline Keyboard (Approval Card)
- Row 1: **Approve ✅** | **Cancel ❌**
- Row 2: **Amount** | **Currency** | **Date**
- Row 3: **Category** | **Participants** | **Split**
- Row 4: **Description** | **More…**

Button presses open short flows (CallbackQuery → bot edits message text with state and validates inputs).

---

## 7) API Design (Flask)

### Auth
- `POST /api/auth/login` — Admin login (session cookie).

### Telegram
- `POST /api/telegram/webhook` — signature-verified webhook; receives updates, routes commands, handles callbacks.

### Groups
- `GET /api/groups` — list
- `POST /api/groups` — create
- `GET /api/groups/{id}` — details
- `PATCH /api/groups/{id}` — update
- `DELETE /api/groups/{id}` — delete
- `POST /api/groups/{id}/members/sync` — sync from telegram chat
- `POST /api/groups/{id}/close-period` — compute & lock settlements

### Expenses
- `GET /api/groups/{id}/expenses?from=&to=&q=`
- `POST /api/groups/{id}/expenses` — create (after approval)
- `GET /api/expenses/{id}`
- `PATCH /api/expenses/{id}`
- `DELETE /api/expenses/{id}`
- `POST /api/expenses/{id}/receipt` — upload/view receipt

### Rates
- `GET /api/rates?base=&quote=&at=` — historical fetch (uses cache; hits Yahoo/ECB if missing)
- `POST /internal/rates/prefetch` — cron/worker prefetch for popular pairs

### Exports
- `GET /api/groups/{id}/export?format=xlsx|csv&from=&to=`

### Audit
- `GET /api/audit?entity=expense|group&id=…`

**Notes:** All write endpoints audited. Webhook endpoint must validate Telegram signature/token.

---

## 8) Data Model (PostgreSQL)
```
users(
  id PK, telegram_user_id UNIQUE, username, display_name, created_at
)

groups(
  id PK, name, description, base_currency, settings JSONB, created_at
)

memberships(
  id PK, group_id FK, user_id FK, role TEXT DEFAULT 'member', joined_at
)

expenses(
  id PK, group_id FK, created_by_user_id FK, paid_by_user_id FK,
  amount NUMERIC(18,2), currency CHAR(3),
  amount_in_base NUMERIC(18,2),
  fx_rate NUMERIC(18,8), fx_fixed_at TIMESTAMP WITH TIME ZONE,
  date DATE, category TEXT, description TEXT,
  status TEXT CHECK (status IN ('approved','void')) DEFAULT 'approved',
  created_at, updated_at
)

expense_participants(
  id PK, expense_id FK, user_id FK,
  share_type TEXT CHECK (share_type IN ('equal','weight')) DEFAULT 'equal',
  weight NUMERIC(9,4), amount_in_base NUMERIC(18,2)
)

receipts(
  id PK, expense_id FK, file_path TEXT, ocr_meta JSONB, stored_at
)

settlements(
  id PK, group_id FK, period_from DATE, period_to DATE,
  graph JSONB, is_closed BOOLEAN, closed_at TIMESTAMP WITH TIME ZONE
)

audit_log(
  id PK, actor_user_id FK, entity_type TEXT, entity_id BIGINT,
  action TEXT, before JSONB, after JSONB, at TIMESTAMP WITH TIME ZONE
)
```

**Computation:** `amount_in_base` is computed at save-time using the fixed `fx_rate`.  
**Indices:** common FK indices; btree on (`group_id`, `date`), GIN on `settings` and text-search if needed.

---

## 9) FX Logic
- **Primary source:** Yahoo Finance (e.g., `yfinance` or REST wrapper).
- **Fallback:** Frankfurter/ECB.
- **Fixing rule:** `fx_fixed_at = message_received_at`. Use the nearest available rate at or before this timestamp (hourly bucket). Cache key: `(base, quote, YYYY-MM-DD HH:00)`.
- **Failure:** If both fail and expense currency != base, do **not** save; return “awaiting_rate” and prompt to retry or switch currency.

---

## 10) AI & OCR Integration
- **OCR:** Local Tesseract. Output: amount, currency, date, merchant (when possible).
- **LLM (Local, e.g., Ollama):**
  - **Input:** free text or OCR text + group context (members, base currency, categories).
  - **Output JSON Schema:**
    ```json
    {
      "amount": 120.0,
      "currency": "ILS",
      "date_hint": "2025-09-15T14:20:00Z",
      "category": "Food & Drinks",
      "description": "Restaurant",
      "participants": ["telegram_user_id1","telegram_user_id2", "..."],
      "split": "equal",
      "confidence": 0.83
    }
    ```
- **Threshold:** If `confidence < 0.75`, Approve button disabled until manual edit.
- **Category set:** fixed 10 categories listed above; allow override via edit UI.

---

## 11) Security, Privacy, Audit
- **Web Auth:** simple session (admin only). CSRF enabled. HTTPS via nginx.
- **Telegram:** verify webhook signature and bot token. Store `chat_id` and `message_id` for traceability.
- **Receipts:** local filesystem storage under `/data/receipts/{group_id}/`. Validate MIME, sanitize filenames, size limit.
- **Encryption (basic):** .env for secrets; at-rest encryption optional note for future (KMS later).
- **Audit:** all create/update/delete actions on expenses/groups captured with before/after snapshots.

---

## 12) Rate Limiting (Scaffold Only)
- Middleware stubs with Redis counters by `ip`/`chat_id`/`route`. Disabled by default via config flag.
- Metrics emitted; thresholds configurable for future activation.

---

## 13) Deployment
- **docker-compose services:** `api`, `bot`, `worker`, `db`, `redis`, `nginx`.
- Healthchecks; log to stdout; rotate via Docker; volumes for DB and receipts.
- Example env (non-secret placeholders):
  ```env
  APP_ENV=prod
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_WEBHOOK_SECRET=...
  LLM_BASE_URL=http://ollama-host:11434
  LLM_TOKEN=optional
  OCR_ENABLED=true
  FX_PRIMARY=yahoo
  FX_FALLBACK=ecb
  FX_CACHE_TTL_SECONDS=3600
  DB_URL=postgresql://user:pass@db:5432/splitbot
  REDIS_URL=redis://redis:6379/0
  ADMIN_EMAIL=admin@example.local
  SESSION_SECRET=change-me
  FILES_ROOT=/data/receipts
  ```

---

## 14) Testing & Acceptance
**Unit:** parsers, FX module (sources, cache, failure), settlement math.  
**Integration:** webhook → LLM/OCR → approval card → save; summary; close period.  
**E2E happy paths:**
- Text expense (ILS) approved and saved with fixed rate=1.0.
- Receipt expense (USD) approved; FX fetched and fixed; shows in summary.
- Summary export to Excel. 
- Close period computes simplified debts and renders “who pays who.”
- Admin edits expense; audit logs capture before/after.
**E2E failure paths:**
- FX primary+fallback fail → “awaiting_rate” surfaced; no save.
- LLM `confidence < 0.75` → edits required before Approve.
- Invalid receipt MIME → rejected with helpful message.

**Acceptance Criteria (MVP):**
- Add bot to Telegram group, run `/start`, link to system group.
- Add expense via text and via receipt; edit via inline; approve persists.
- Summary works; export Excel downloads with valid data.
- Close period calculates and persists settlements.
- Web admin accessible on mobile; can view/edit expenses and members.
- Audit log visible for expense changes.

---

## 15) Backlog (+1 After MVP)
- Per-item receipt parsing and per-person item assignment.
- Multiple splits (weights/ratios) UI.
- Charts/analytics in web.
- Optional PDF report.
- Stronger security hardening (KMS, full at-rest encryption).
- Real rate limiting and quotas.
- Payment link integrations.
