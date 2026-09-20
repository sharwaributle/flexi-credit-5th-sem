import os
import sqlite3
from datetime import datetime, date, timedelta
from functools import wraps
import csv
import io
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, g, Response
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

DB_PATH = os.path.join(os.path.dirname(__file__), "expenses.db")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

CATEGORIES = ["Food", "Transport", "Housing", "Utilities", "Health", "Shopping", "Entertainment", "Other"]


# ---------- Database helpers ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            note TEXT,
            spent_on TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            amount REAL,
            due_date TEXT NOT NULL,
            note TEXT,
            is_recurring INTEGER DEFAULT 0,
            is_done INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );
        """
    )
    db.commit()
    db.close()


def ensure_income_and_budget_schema():
    db = get_db()
    user_cols = [row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()]
    if "monthly_income" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN monthly_income REAL DEFAULT 25000.0")
        db.commit()

    db.execute("""
        CREATE TABLE IF NOT EXISTS category_budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            budget_limit REAL NOT NULL,
            UNIQUE(user_id, category),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    db.commit()


with app.app_context():
    init_db()
    ensure_income_and_budget_schema()


# ---------- Auth helpers ----------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    return {"current_user": session.get("username")}


# ---------- Auth routes ----------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not username or not email or not password:
            flash("All fields are required.", "error")
            return redirect(url_for("register"))
        if password != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register"))
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for("register"))

        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (username, email, generate_password_hash(password), datetime.utcnow().isoformat()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("Username or email already taken.", "error")
            return redirect(url_for("register"))

        flash("Account created. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip().lower()
        password = request.form.get("password", "")

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ? OR email = ?", (identifier, identifier)
        ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Invalid credentials.", "error")
            return redirect(url_for("login"))

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


# ---------- Core pages ----------

@app.route("/expenses/export")
@login_required
def export_expenses_csv():
    db = get_db()
    user_id = session["user_id"]

    expenses = db.execute(
        "SELECT spent_on, category, note, amount FROM expenses WHERE user_id = ? ORDER BY spent_on DESC",
        (user_id,),
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Category", "Note", "Amount"])

    for row in expenses:
        writer.writerow(
            [row["spent_on"], row["category"], row["note"] or "", f"{row['amount']:.2f}"]
        )

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=expenses.csv"},
    )


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/update-budget-profile", methods=["POST"])
@login_required
def update_budget_profile():
    db = get_db()
    user_id = session["user_id"]

    try:
        raw_income = request.form.get("monthly_income", "25000").strip()
        income = float(raw_income) if raw_income else 25000.0
    except ValueError:
        income = 25000.0

    db.execute("UPDATE users SET monthly_income = ? WHERE id = ?", (income, user_id))

    for cat in CATEGORIES:
        limit_val = request.form.get(f"budget_{cat}")
        if limit_val:
            try:
                val = float(limit_val)
                db.execute("""
                    INSERT INTO category_budgets (user_id, category, budget_limit)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, category) DO UPDATE SET budget_limit = excluded.budget_limit
                """, (user_id, cat, val))
            except ValueError:
                pass

    db.commit()
    flash(f"Monthly Income updated to ₹{income:,.0f}!", "success")
    return redirect(url_for("dashboard"))


@app.route("/api/ai-audit", methods=["GET"])
@login_required
def ai_audit():
    db = get_db()
    user_id = session["user_id"]
    month_start = date.today().replace(day=1).isoformat()

    expenses = db.execute(
        "SELECT category, SUM(amount) as total FROM expenses WHERE user_id = ? AND spent_on >= ? GROUP BY category",
        (user_id, month_start)
    ).fetchall()

    user = db.execute("SELECT monthly_income FROM users WHERE id = ?", (user_id,)).fetchone()
    income = user["monthly_income"] if user and user["monthly_income"] else 25000.0

    default_budgets = {
        "Food": 5000.0, "Transport": 2500.0, "Housing": 10000.0, "Utilities": 3000.0,
        "Health": 2000.0, "Shopping": 3000.0, "Entertainment": 1500.0, "Other": 1000.0
    }
    budget_rows = db.execute(
        "SELECT category, budget_limit FROM category_budgets WHERE user_id = ?", (user_id,),
    ).fetchall()
    budget_map = default_budgets.copy()
    for row in budget_rows:
        budget_map[row["category"]] = float(row["budget_limit"])

    expense_map = {row["category"]: float(row["total"]) for row in expenses}
    budget_summary = ", ".join(
        f"{cat}: spent ₹{expense_map.get(cat, 0):.0f} / budget ₹{budget_map[cat]:.0f}" for cat in CATEGORIES
    )

    overspent = [
        (cat, expense_map.get(cat, 0), budget_map[cat]) for cat in CATEGORIES if expense_map.get(cat, 0) > budget_map[cat]
    ]

    prompt = (
        f"User Monthly Income: ₹{income:.0f}. "
        f"Actual category spending and budgets: {budget_summary}. "
        f"Give exactly 2 short sentences of proactive financial advice in simple Hinglish. "
        f"Use only these numbers. Mention the actual over-budget category and its actual "
        f"spent/limit values if one exists. If none is over budget, say so. "
        f"Never invent a fixed percentage or category."
    )

    if overspent:
        cat, spent, cap = max(overspent, key=lambda x: x[1] - x[2])
        insight = (
            f"{cat} budget cross ho raha hai: ₹{spent:.0f} spent vs ₹{cap:.0f} limit. "
            f"Is category ke non-essential expenses ko control karein."
        )
    else:
        highest = max(CATEGORIES, key=lambda c: expense_map.get(c, 0))
        insight = (
            f"Abhi koi category apne budget limit se upar nahi hai. "
            f"{highest} mein sabse zyada spending ₹{expense_map.get(highest, 0):.0f} hai."
        )

    if GROQ_API_KEY:
        try:
            from groq import Groq
            client = Groq(api_key=GROQ_API_KEY)
            resp = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=90
            )
            insight = resp.choices[0].message.content.strip()
        except Exception:
            pass

    return jsonify({"insight": insight})


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    db = get_db()
    user_id = session["user_id"]

    if request.method == "POST":
        amount = request.form.get("amount", "")
        category = request.form.get("category", "Other")
        note = request.form.get("note", "").strip()
        spent_on = request.form.get("spent_on") or date.today().isoformat()

        try:
            amount_val = float(amount)
            if amount_val <= 0:
                raise ValueError
        except ValueError:
            flash("Enter a valid amount greater than 0.", "error")
            return redirect(url_for("dashboard"))

        db.execute(
            "INSERT INTO expenses (user_id, amount, category, note, spent_on, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, amount_val, category, note, spent_on, datetime.utcnow().isoformat()),
        )
        db.commit()
        flash("Expense logged.", "success")
        return redirect(url_for("dashboard"))

    expenses = db.execute(
        "SELECT * FROM expenses WHERE user_id = ? ORDER BY spent_on DESC, id DESC LIMIT 100",
        (user_id,),
    ).fetchall()

    total = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE user_id = ?", (user_id,)
    ).fetchone()["total"]

    month_start = date.today().replace(day=1).isoformat()
    month_total = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE user_id = ? AND spent_on >= ?",
        (user_id, month_start),
    ).fetchone()["total"]

    user_row = db.execute(
        "SELECT monthly_income FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    monthly_income = (
        float(user_row["monthly_income"]) if user_row and user_row["monthly_income"] is not None else 25000.0
    )
    remaining_balance = max(0.0, monthly_income - month_total)
    savings_rate = round((remaining_balance / monthly_income) * 100) if monthly_income > 0 else 0

    by_category_raw = db.execute(
        """SELECT category, COALESCE(SUM(amount), 0) AS total FROM expenses
           WHERE user_id = ? AND spent_on >= ? GROUP BY category ORDER BY total DESC""",
        (user_id, month_start),
    ).fetchall()
    category_map = {row["category"]: row["total"] for row in by_category_raw}

    saved_budgets = db.execute(
        "SELECT category, budget_limit FROM category_budgets WHERE user_id = ?", (user_id,),
    ).fetchall()
    saved_budget_map = {row["category"]: float(row["budget_limit"]) for row in saved_budgets}

    default_category_caps = {
        "Food": 5000, "Transport": 2500, "Housing": 10000, "Utilities": 3000,
        "Health": 2000, "Shopping": 3000, "Entertainment": 1500, "Other": 1000
    }
    category_caps = {
        cat: saved_budget_map.get(cat, default_category_caps.get(cat, 3000)) for cat in CATEGORIES
    }

    # Legacy 10x-value protection
    if monthly_income == 250000.0 and all(
        category_caps.get(cat) == default_category_caps[cat] * 10 for cat in CATEGORIES
    ):
        monthly_income = 25000.0
        db.execute("UPDATE users SET monthly_income = ? WHERE id = ?", (monthly_income, user_id))
        for cat, default_val in default_category_caps.items():
            db.execute(
                "UPDATE category_budgets SET budget_limit = ? WHERE user_id = ? AND category = ?",
                (default_val, user_id, cat),
            )
        db.commit()
        category_caps = default_category_caps.copy()

    category_icons = {
        "Food": "🥗", "Transport": "🚌", "Housing": "🏠", "Utilities": "⚡",
        "Health": "💊", "Shopping": "🛍️", "Entertainment": "🎮", "Other": "💵"
    }

    category_cards = []
    for cat in CATEGORIES:
        spent = category_map.get(cat, 0.0)
        cap = category_caps.get(cat, 3000)
        pct = min(100, int((spent / cap) * 100)) if cap > 0 else 0
        category_cards.append({
            "name": cat,
            "spent": spent,
            "cap": cap,
            "percent": pct,
            "icon": category_icons.get(cat, "📁")
        })

    today_date = date.today()
    monthly_budget = sum(category_caps.values())
    budget_percent = min(100, int((month_total / monthly_budget) * 100)) if monthly_budget > 0 else 0
    days_in_month = 30
    days_left = max(0, days_in_month - today_date.day)

    current_hour = datetime.now().hour
    if current_hour < 12:
        greeting = "Good Morning"
    elif current_hour < 17:
        greeting = "Good Afternoon"
    else:
        greeting = "Good Evening"

    upcoming_cutoff = (today_date + timedelta(days=7)).isoformat()
    upcoming_reminders = db.execute(
        """SELECT * FROM reminders WHERE user_id = ? AND is_done = 0
           AND due_date <= ? ORDER BY due_date ASC LIMIT 5""",
        (user_id, upcoming_cutoff),
    ).fetchall()

    return render_template(
        "dashboard.html",
        expenses=expenses,
        total=total,
        month_total=month_total,
        monthly_income=monthly_income,
        remaining_balance=remaining_balance,
        savings_rate=savings_rate,
        monthly_budget=monthly_budget,
        budget_percent=budget_percent,
        days_left=days_left,
        greeting=greeting,
        categories=CATEGORIES,
        category_cards=category_cards,
        today=today_date.isoformat(),
        upcoming_reminders=upcoming_reminders,
    )


@app.route("/expenses/<int:expense_id>/delete", methods=["POST"])
@login_required
def delete_expense(expense_id):
    db = get_db()
    db.execute(
        "DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, session["user_id"])
    )
    db.commit()
    flash("Expense removed.", "success")
    return redirect(url_for("dashboard"))


# ---------- Reminders ----------

@app.route("/quick-allocate-budget", methods=["POST"])
@login_required
def quick_allocate_budget():
    db = get_db()
    user_id = session["user_id"]

    try:
        income = float(request.form.get("monthly_income", 25000.0))
    except ValueError:
        income = 25000.0

    priority = request.form.get("priority", "balanced")

    db.execute("UPDATE users SET monthly_income = ? WHERE id = ?", (income, user_id))

    if priority == "rent_heavy":
        allocations = {
            "Housing": income * 0.45, "Food": income * 0.20, "Utilities": income * 0.10,
            "Transport": income * 0.08, "Shopping": income * 0.07, "Entertainment": income * 0.05,
            "Health": income * 0.03, "Other": income * 0.02
        }
    elif priority == "food_lifestyle":
        allocations = {
            "Food": income * 0.30, "Housing": income * 0.25, "Shopping": income * 0.15,
            "Transport": income * 0.10, "Utilities": income * 0.08, "Entertainment": income * 0.07,
            "Health": income * 0.03, "Other": income * 0.02
        }
    elif priority == "savings_first":
        allocations = {
            "Housing": income * 0.25, "Food": income * 0.15, "Utilities": income * 0.05,
            "Transport": income * 0.05, "Shopping": income * 0.04, "Entertainment": income * 0.03,
            "Health": income * 0.02, "Other": income * 0.01
        }
    else:  # Balanced
        allocations = {
            "Housing": income * 0.35, "Food": income * 0.20, "Utilities": income * 0.10,
            "Transport": income * 0.10, "Shopping": income * 0.10, "Entertainment": income * 0.05,
            "Health": income * 0.05, "Other": income * 0.05
        }

    for cat, cap in allocations.items():
        db.execute("""
            INSERT INTO category_budgets (user_id, category, budget_limit)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, category) DO UPDATE SET budget_limit = excluded.budget_limit
        """, (user_id, cat, round(cap, -1)))

    db.commit()
    flash(f"Income set to ₹{income:,.0f} & Budget auto-distributed based on your priority!", "success")
    return redirect(url_for("dashboard"))


@app.route("/reminders", methods=["GET", "POST"])
@login_required
def reminders():
    db = get_db()
    user_id = session["user_id"]

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        amount = request.form.get("amount", "")
        due_date = request.form.get("due_date", "")
        note = request.form.get("note", "").strip()
        is_recurring = 1 if request.form.get("is_recurring") == "on" else 0

        if not title or not due_date:
            flash("A title and due date are required.", "error")
            return redirect(url_for("reminders"))

        amount_val = None
        if amount:
            try:
                amount_val = float(amount)
            except ValueError:
                amount_val = None

        db.execute(
            """INSERT INTO reminders (user_id, title, amount, due_date, note, is_recurring, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, title, amount_val, due_date, note, is_recurring, datetime.utcnow().isoformat()),
        )
        db.commit()
        flash("Reminder set.", "success")
        return redirect(url_for("reminders"))

    all_reminders = db.execute(
        "SELECT * FROM reminders WHERE user_id = ? ORDER BY is_done ASC, due_date ASC",
        (user_id,),
    ).fetchall()

    return render_template("reminders.html", reminders=all_reminders, today=date.today().isoformat())


@app.route("/reminders/<int:reminder_id>/done", methods=["POST"])
@login_required
def complete_reminder(reminder_id):
    db = get_db()
    reminder = db.execute(
        "SELECT * FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, session["user_id"])
    ).fetchone()

    if reminder:
        if reminder["is_recurring"]:
            try:
                next_due = (datetime.fromisoformat(reminder["due_date"]) + timedelta(days=30)).date().isoformat()
            except ValueError:
                next_due = reminder["due_date"]
            db.execute("UPDATE reminders SET due_date = ?, is_done = 0 WHERE id = ?", (next_due, reminder_id))
        else:
            db.execute("UPDATE reminders SET is_done = 1 WHERE id = ?", (reminder_id,))
        db.commit()

    return redirect(url_for("reminders"))


@app.route("/reminders/<int:reminder_id>/delete", methods=["POST"])
@login_required
def delete_reminder(reminder_id):
    db = get_db()
    db.execute(
        "DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, session["user_id"])
    )
    db.commit()
    flash("Reminder deleted.", "success")
    return redirect(url_for("reminders"))


# ---------- Chatbot (Groq) ----------

@app.route("/chat")
@login_required
def chat():
    db = get_db()
    history = db.execute(
        "SELECT * FROM chat_messages WHERE user_id = ? ORDER BY id ASC LIMIT 50",
        (session["user_id"],),
    ).fetchall()
    return render_template("chat.html", history=history, has_key=bool(GROQ_API_KEY))


@app.route("/chat/clear")
@login_required
def clear_chat():
    db = get_db()
    db.execute("DELETE FROM chat_messages WHERE user_id = ?", (session["user_id"],))
    db.commit()
    return redirect(url_for("chat"))


@app.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    if not GROQ_API_KEY:
        return jsonify({"error": "GROQ_API_KEY is not configured on the server. Add it to your .env file."}), 500

    data = request.get_json(force=True)
    user_message = (data.get("message") or "").strip()
    if not user_message:
        return jsonify({"error": "Message cannot be empty."}), 400

    db = get_db()
    user_id = session["user_id"]

    db.execute(
        "INSERT INTO chat_messages (user_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
        (user_id, user_message, datetime.utcnow().isoformat()),
    )
    db.commit()

    month_start = date.today().replace(day=1).isoformat()
    month_total = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE user_id = ? AND spent_on >= ?",
        (user_id, month_start),
    ).fetchone()["total"]
    by_category = db.execute(
        """SELECT category, COALESCE(SUM(amount), 0) AS total FROM expenses
           WHERE user_id = ? AND spent_on >= ? GROUP BY category ORDER BY total DESC""",
        (user_id, month_start),
    ).fetchall()
    category_summary = ", ".join(f"{row['category']}: {row['total']:.2f}" for row in by_category) or "no expenses logged yet"

    system_prompt = (
        "You are a concise, friendly personal finance assistant embedded in an expense "
        "tracker app. Use the user's spending data when relevant. Keep answers short and "
        "practical. Do not give regulated investment or tax advice; suggest a professional "
        f"for that. This month's spending so far: {month_total:.2f} total. By category: {category_summary}."
    )

    recent_history = db.execute(
        "SELECT role, content FROM chat_messages WHERE user_id = ? ORDER BY id DESC LIMIT 10",
        (user_id,),
    ).fetchall()
    messages = [{"role": "system", "content": system_prompt}]
    for row in reversed(recent_history):
        role = "assistant" if row["role"] == "assistant" else "user"
        messages.append({"role": role, "content": row["content"]})

    try:
        response = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"model": GROQ_MODEL, "messages": messages, "max_tokens": 500, "temperature": 0.5},
            timeout=30,
        )
        response.raise_for_status()
        reply = response.json()["choices"][0]["message"]["content"].strip()
    except requests.exceptions.RequestException as exc:
        reply = f"Sorry, I couldn't reach the chat service right now ({exc})."

    db.execute(
        "INSERT INTO chat_messages (user_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
        (user_id, reply, datetime.utcnow().isoformat()),
    )
    db.commit()

    return jsonify({"reply": reply})


if __name__ == "__main__":
    with app.app_context():
        init_db()
        ensure_income_and_budget_schema()
    app.run(debug=True)
