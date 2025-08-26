# Triggers & Triumphs – AI Cards (Pro-ready)

## Quick Start (Local)
```bash
python -m venv .venv
# Windows: . .venv/Scripts/activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# .env (example)
SECRET_KEY="change-this"
OPENAI_API_KEY="sk-..."
FREE_DAILY="5"
STRIPE_SECRET_KEY="sk_test_..."
STRIPE_PRICE_ID="price_123..."
BASE_URL="http://localhost:10000"

python app.py  # http://localhost:10000
