# SplitBot

Telegram-first expense splitting bot (Hebrew ↔ English toggle) with multi-currency conversion, optional AI free-text parsing (Gemini or Ollama), emoji categories, weighted share engine (schema + balance logic), and SQLite persistence. Lightweight single-container deployment.

## ✨ Features
- ➕ Add expenses via `/add` or free-text (AI or regex parser).
- 🤖 AI extraction (Gemini API or local/remote Ollama) with confirmation inline keyboard.
- 💱 Multi-currency detection: ILS/₪/ש"ח/שח/nis + many ISO codes. Conversion using `yfinance` with layered fallback (inverse, USD bridge, static). Approximate conversions flagged with `~`.
- 🗂 Categories + emojis; `/stats` category totals with percentages.
- ⚖️ Balances use per-expense weight snapshots (weights column already stored; commands coming soon).
- 🤝 Debt settlement suggestions (`/settle`) via greedy pairing.
- 📄 Pagination for `/list` with inline navigation.
- 📤 `/export` full CSV including FX metadata & participants.
- 👥 Virtual participants (negative IDs) + interactive name capture.
- � Live language toggle `/lang` (Hebrew ↔ English).
- 🪵 Structured logging with separate FX and AI loggers.

## 🏗 Architecture (Current Simplified)
Single Python process using `python-telegram-bot` + SQLite. Legacy multi-service spec retained in `instruction.md` for future expansion (API, workers, OCR, etc.).

## 🚀 Quick Start (Docker, PowerShell)
```powershell
$env:TELEGRAM_BOT_TOKEN="123456789:REAL_TOKEN"
# Optional AI
$env:GEMINI_API_KEY="your_gemini_key"
docker compose up --build
```
Add the bot to a Telegram group and run `/start`.

## 🛠 Local Development
```powershell
python -m venv .venv
. .venv/Scripts/Activate.ps1
pip install -r requirements.txt
$env:TELEGRAM_BOT_TOKEN="123:token"; python bot.py
```

## 🔧 Environment Variables
| Var | Description | Default |
|-----|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token (required) | — |
| `DEFAULT_CURRENCY` | Base currency for new chats | `USD` |
| `GEMINI_API_KEY` | Enable Gemini AI parsing (if AI_PROVIDER=GEMINI or unset) | (disabled) |
| `GEMINI_MODEL` | Gemini model | `gemini-1.5-flash` |
| `AI_PROVIDER` | `GEMINI` or `OLLAMA` (auto = GEMINI if key present) | (auto) |
| `OLLAMA_BASE_URL` | Ollama server base URL | `http://localhost:11434` |
| `OLLAMA_MODEL` | Ollama model name | `qwen3:8b` |
| `LOG_LEVEL` | Logging level | `INFO` |

See `.env.example`.

### AI Provider Selection
Set `AI_PROVIDER=OLLAMA` to use a remote/local Ollama instance (e.g. `http://192.168.1.20:11434`) with `OLLAMA_MODEL` (defaults `qwen3:8b`). If `AI_PROVIDER` is unset and a `GEMINI_API_KEY` exists, Gemini is used. If neither is configured, the bot falls back to a lightweight regex parser.

## 💬 Commands
| Command | Purpose |
|---------|---------|
| `/start` | Welcome + prompt to add users |
| `/help` | Help text with current currency |
| `/setcurrency` | Set base currency (before first expense) |
| `/currency` | Show base currency |
| `/add` | Manual add: `/add 50 ILS lunch` |
| `/adduser` | Add virtual user or capture sender name |
| `/users` | List participants |
| `/list [page]` | Paginated expenses |
| `/bal` | Weighted balances |
| `/settle` | Suggested settlements |
| `/stats` | Category totals |
| `/export` | CSV export |
| (free text) | Parse & confirm expense |

Planned: `/setweight`, `/weights`, `/stats30`, `/monthly`.

### Localization
Use `/lang` anytime to switch the chat language. The choice is stored per chat (currently in JSON; DB column planned). Most user-facing strings are localized; a few edge responses will be migrated to the translation layer in upcoming refactors.

## 🗃 Data Model (SQLite)
Tables include `users(weight)`, `expenses(original_amount, original_currency, fx_rate, fx_fallback)`, `expense_participants(weight)` enabling proportional splits.

## 💱 FX Strategy
1. Direct pair (`FROMTO=X`)
2. Inverse pair (invert)
3. Bridge via USD (fallback, marks `fx_fallback`)
4. Static approximate table (fallback)
6h cache. Approximate conversions in lists get a `~` marker.

## 🧮 Balances (Weighted Engine)
Every expense stores a snapshot of participant weights (already in DB). Current user commands still assume equal weights until `/setweight` & `/weights` are added. Once implemented, future weight changes won’t retroactively alter past expenses because of the snapshot model.

## 🧪 Future Test Coverage (Planned)
- Currency detection & fallback flag
- Weighted balance edge cases
- Settlement convergence
- AI parse fallback to regex

## 🗺 Roadmap Snapshot
- Weight commands: `/setweight`, `/weights` UI + validation
- JSON → DB migration helper (one-off import of legacy chat JSONs)
- Time windows: `/stats30`, `/monthly` summaries
- Per-expense participant selection (custom splits)
- Full translation extraction & English coverage for every minor string

## 🤝 Contributing
See `CONTRIBUTING.md`. Please open an issue before large refactors. MIT license.

## 🔐 Security
Do not commit secrets. Report issues per `SECURITY.md`.

## 📄 License
MIT © 2025 SplitBot Contributors

---
See `instruction.md` for long-form product & architecture spec aimed at future multi-service expansion.

