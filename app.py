import os, json, re, requests, time, uuid
from datetime import timedelta
from flask import Flask, render_template, request, session, redirect, url_for, flash
from dotenv import load_dotenv
import stripe

load_dotenv()  # load .env locally

# --- App setup ---
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))
app.permanent_session_lifetime = timedelta(days=180)

# Cookie hardening (safe once deployed on HTTPS; OK locally too)
app.config.update(
    SESSION_COOKIE_SECURE=True,   # only send cookies over HTTPS
    SESSION_COOKIE_SAMESITE="Lax" # good default for web apps
)

OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
FREE_DAILY      = int(os.environ.get("FREE_DAILY", "5"))

# Stripe config
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID   = os.environ.get("STRIPE_PRICE_ID", "")
BASE_URL          = os.environ.get("BASE_URL", "http://localhost:10000")

# Optional fallbacks
STRIPE_LINK    = os.environ.get("STRIPE_LINK", "")
ADMIN_PRO_CODE = os.environ.get("ADMIN_PRO_CODE", "")

if not OPENAI_API_KEY:
    raise RuntimeError("Set OPENAI_API_KEY in environment.")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# --- Game config ---
CATEGORIES = ["Trigger", "Coping", "Healing", "Wild"]
TONES = ["Classic", "Sassy", "Spicy", "Extra Spicy"]
TONE_GUIDE = {
    "Classic": "balanced, gently witty, broadly appealing",
    "Sassy": "campy, flirtatious, bold quips, playful shade",
    "Spicy": "edgier quips, darker humor, more bite (still kind)",
    "Extra Spicy": "max sass and innuendo; toe the PG-13 line without crossing it",
}

# --- Prompting ---
SAFETY_RAILS = (
    "Keep it PG-13. No slurs, hate, graphic violence, explicit sexual content, "
    "self-harm instructions, or illegal advice. Do not demean or target protected groups. "
    "Snark is fine, cruelty is not. Innuendo allowed; keep it tasteful."
)

SYSTEM_PROMPT = f"""
You write cards for the satirical healing card game "Triggers & Triumphs".

VOICE: Samantha Jones meets a drag show emcee — witty, camp, irreverent, flirtatious; confident,
sharp one-liners; a little savage but ultimately kind. Embrace double entendre and theatrical flair.
Never punch down. Keep it cathartic and empowering.

Each card must be STRICT JSON with keys:
- title (≤ 8 words)
- subtitle (≤ 14 words)
- body (≤ 80 words)
- category (one of: Trigger, Coping, Healing, Wild)
- tags (2–5 simple words)

Tone: darkly comedic + empathetic + clever. {SAFETY_RAILS}
Output JSON only. No commentary, no backticks.
"""

# -------------------------- OPENAI --------------------------

def call_openai(category: str, theme: str, tone: str = "Sassy") -> dict:
    """Call OpenAI Chat Completions for one card with a tone knob."""
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    tone_desc = TONE_GUIDE.get(tone, TONE_GUIDE["Sassy"])
    user_prompt = (
        f"Create one {category} card. Target tone: {tone} ({tone_desc})."
        + (f" Theme: {theme.strip()}." if theme and theme.strip() else "")
        + " Keep it original and on-brand. Return strict JSON only."
    )
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 1.0,
        "max_tokens": 300,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()
    return parse_json_safe(text)

def parse_json_safe(text: str) -> dict:
    """Extract first JSON object from text safely."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return {"title": "Card Error", "subtitle": "Parsing issue",
                "body": "We couldn't parse the AI response as JSON. Try again.",
                "category": "Trigger", "tags": ["error"]}
    try:
        data = json.loads(match.group(0))
        data.setdefault("category", "Trigger")
        data.setdefault("tags", [])
        for k in ["title", "subtitle", "body"]:
            data.setdefault(k, "")
        return data
    except Exception:
        return {"title": "Card Error", "subtitle": "JSON decode failed",
                "body": "The response wasn't valid JSON. Try again.",
                "category": "Trigger", "tags": ["error"]}

# -------------------------- FREE QUOTA (per email/IP) --------------------------

# In-memory usage store: { (key_id, 'YYYY-MM-DD'): int_count }
USAGE = {}

def _today() -> str:
    return time.strftime("%Y-%m-%d")

def usage_key() -> str:
    """
    Stable key for free quota:
      - signed-in users: their email
      - anonymous users: their IP (from X-Forwarded-For or remote_addr)
    """
    if session.get("email"):
        return f"email:{session['email'].lower()}"
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "0.0.0.0").split(",")[0].strip()
    return f"ip:{ip}"

def uses_left() -> int:
    if session.get("pro"):
        return 9999
    key = usage_key()
    today = _today()
    used = USAGE.get((key, today), 0)
    try:
        daily = int(os.environ.get("FREE_DAILY", str(FREE_DAILY)))
    except Exception:
        daily = FREE_DAILY
    return max(daily - used, 0)

def mark_use():
    if session.get("pro"):
        return
    key = usage_key()
    today = _today()
    USAGE[(key, today)] = USAGE.get((key, today), 0) + 1

# -------------------------- STRIPE HELPERS --------------------------

def stripe_email_is_pro(user_email: str) -> bool:
    """
    True if the email has:
      - an ACTIVE subscription, OR
      - a Stripe Customer with metadata['lifetime_pro']=="true" (from one-time purchase).
    """
    if not (STRIPE_SECRET_KEY and user_email):
        return False
    try:
        customers = stripe.Customer.search(query=f"email:'{user_email}'", limit=1)
        if not customers.data:
            return False
        cust = customers.data[0]

        # Lifetime flag from one-time checkout
        meta = getattr(cust, "metadata", None) or {}
        if str(meta.get("lifetime_pro", "")).lower() == "true":
            return True

        # Any active subscription
        subs = stripe.Subscription.list(customer=cust.id, status="active", limit=1)
        return bool(subs and subs.data)
    except Exception:
        return False

# -------------------------- ROUTES --------------------------

@app.route("/", methods=["GET"])
def home():
    session.setdefault("sid", str(uuid.uuid4()))
    return render_template("index.html",
                           categories=CATEGORIES,
                           tones=TONES,
                           remaining=uses_left(),
                           last=session.get("last_card"),
                           stripe=bool(STRIPE_PRICE_ID or STRIPE_LINK))

@app.route("/generate", methods=["POST"])
def generate():
    if uses_left() <= 0 and not session.get("pro"):
        flash("You’ve used today’s free cards. Upgrade for unlimited.", "error")
        return redirect(url_for("upgrade"))

    category = request.form.get("category", "Trigger")
    tone = request.form.get("tone", "Sassy")
    theme = request.form.get("theme", "")
    if category not in CATEGORIES:
        category = "Trigger"

    try:
        card = call_openai(category, theme, tone)
    except Exception as e:
        card = {"title": "Network Error", "subtitle": "",
                "body": f"OpenAI request failed: {e}", "category": category, "tags": ["error"]}

    mark_use()
    session["last_card"] = card
    return render_template("index.html",
                           categories=CATEGORIES,
                           tones=TONES,
                           remaining=uses_left(),
                           last=card,
                           stripe=bool(STRIPE_PRICE_ID or STRIPE_LINK))

# ---------- Upgrade (Payment) ----------

@app.route("/buy")
def buy():
    """
    Create a Stripe Checkout Session that adapts to the price type.
    - If STRIPE_PRICE_ID is recurring -> mode='subscription'
    - If one-time -> mode='payment'
    success_url includes the session_id so /pro can tag lifetime buyers.
    """
    if STRIPE_SECRET_KEY and STRIPE_PRICE_ID:
        try:
            price = stripe.Price.retrieve(STRIPE_PRICE_ID)
            is_recurring = bool(getattr(price, "recurring", None))

            checkout_kwargs = dict(
                line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
                success_url=f"{BASE_URL}/pro?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{BASE_URL}/upgrade",
                allow_promotion_codes=True,
                customer_email=session.get("email") or None,
            )
            checkout_kwargs["mode"] = "subscription" if is_recurring else "payment"

            session_obj = stripe.checkout.Session.create(**checkout_kwargs)

            # same-device guard: expect this exact checkout session to return to /pro
            session["pending_checkout_id"] = session_obj.id

            return redirect(session_obj.url, code=303)
        except Exception as e:
            flash(f"Stripe error: {e}", "error")
            return redirect(url_for("upgrade"))

    if STRIPE_LINK:
        return redirect(STRIPE_LINK)
    flash("Upgrade not configured yet.", "error")
    return redirect(url_for("upgrade"))

@app.route("/upgrade", methods=["GET", "POST"])
def upgrade():
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        if ADMIN_PRO_CODE and code == ADMIN_PRO_CODE:
            session.permanent = True
            session["pro"] = True
            flash("Pro unlocked. Enjoy unlimited cards!", "success")
            return redirect(url_for("home"))
        flash("Invalid code.", "error")
    return render_template("upgrade.html", stripe_link=STRIPE_LINK, remaining=uses_left())

@app.route("/pro")
def pro():
    """
    Landing page after Stripe success.
    SECURITY:
      - Requires a valid Stripe Checkout session_id
      - Must match the same device's pending_checkout_id
      - Verifies payment_status == 'paid'
      - If one-time, marks lifetime_pro on the Stripe Customer
      - Saves email for login restoration
    """
    sid = request.args.get("session_id")
    if not sid or not STRIPE_SECRET_KEY:
        flash("Invalid access. Please complete checkout first.", "error")
        return redirect(url_for("upgrade"))

    # same-device guard (prevents unlocking by pasting a session_id elsewhere)
    if session.get("pending_checkout_id") != sid:
        flash("Invalid access. Please complete checkout on this device.", "error")
        return redirect(url_for("upgrade"))

    try:
        chk = stripe.checkout.Session.retrieve(
            sid, expand=["customer", "line_items", "customer_details"]
        )

        # Confirm payment completed
        if chk.payment_status != "paid":
            flash("Payment not completed. Please try again.", "error")
            return redirect(url_for("upgrade"))

        # If user is already signed in, require email match with Checkout email
        if session.get("email") and getattr(chk, "customer_details", None):
            checkout_email = (chk.customer_details.get("email") or "").lower()
            if checkout_email and session["email"].lower() != checkout_email:
                flash("This checkout used a different email. Please sign in with that email.", "error")
                return redirect(url_for("login"))

        # Mark session as Pro
        session.permanent = True
        session["pro"] = True

        # Tag lifetime for one-time purchases
        if getattr(chk, "mode", None) == "payment" and getattr(chk, "customer", None):
            cust_id = chk.customer.id if hasattr(chk.customer, "id") else chk.customer
            cust = stripe.Customer.retrieve(cust_id)
            meta = (getattr(cust, "metadata", None) or {}).copy()
            meta["lifetime_pro"] = "true"
            stripe.Customer.modify(cust_id, metadata=meta)

        # Remember email locally
        if getattr(chk, "customer_details", None) and chk.customer_details.get("email"):
            session["email"] = chk.customer_details["email"].lower()

        # Success; clear pending id
        session.pop("pending_checkout_id", None)

        flash("Thanks for upgrading! Pro is active.", "success")
        return redirect(url_for("home"))

    except Exception as e:
        flash(f"Stripe verification failed: {e}", "error")
        return redirect(url_for("upgrade"))

# ---------- Email sign-in for cross-device Pro ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if not email:
            flash("Please enter a valid email.", "error")
            return redirect(url_for("login"))
        session.permanent = True
        session["email"] = email
        if stripe_email_is_pro(email):
            session["pro"] = True
            flash("Welcome back! Pro recognized for this email.", "success")
        else:
            flash("Signed in. Upgrade anytime to activate Pro on all devices.", "success")
        return redirect(url_for("home"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("home"))

# Optional health check
@app.route("/health")
def health():
    return "ok", 200

# --------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
