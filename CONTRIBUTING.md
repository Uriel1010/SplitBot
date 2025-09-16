# Contributing to SplitBot

Thanks for your interest!

## Development Quickstart
1. Clone the repo
2. Create a virtualenv and install requirements: `pip install -r requirements.txt`
3. Set environment variables (see `.env.example`)
4. Run locally: `python bot.py`
5. Or use Docker: `docker compose up --build`

## Coding Guidelines
- Python 3.12+
- Keep functions small and cohesive
- Prefer explicit logging over silent failure
- Use type hints where practical
- Avoid breaking existing command contracts

## Pull Requests
1. Create a feature branch
2. Add or update documentation (README, comments)
3. Make sure Docker build succeeds
4. Describe the change & rationale
5. Reference related issues

## Database Migrations
Currently lightweight: schema evolves via conditional `ALTER TABLE` in `init_db`. For larger future changes, introduce a migration tool.

## Features Roadmap (High-Level)
- Weighted shares commands (`/setweight`, `/weights`)
- Language toggle `/lang`
- JSON → DB one-time migration helper
- Time-window stats: `/stats30`, `/monthly`

## Reporting Issues
Please include:
- Steps to reproduce
- Expected vs actual behavior
- Logs (DEBUG level if relevant)
- Environment info (OS, Python version)

## Security
Do not post secrets in issues. See `SECURITY.md` for reporting guidelines.

## License
By contributing you agree your code is licensed under MIT (see `LICENSE`).
