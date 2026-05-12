import os
import re
import time
import hmac
import hashlib
from datetime import datetime

import telebot
import razorpay
from flask import Flask, request
from pymongo import MongoClient
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


# ================= ENV =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "").strip()
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip()

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN missing")
if not MONGO_URI:
    raise Exception("MONGO_URI missing")
if not ADMIN_ID:
    raise Exception("ADMIN_ID missing")
if not WEBHOOK_URL:
    raise Exception("WEBHOOK_URL missing")
if not RAZORPAY_KEY_ID:
    raise Exception("RAZORPAY_KEY_ID missing")
if not RAZORPAY_KEY_SECRET:
    raise Exception("RAZORPAY_KEY_SECRET missing")

ADMIN_ID = int(ADMIN_ID)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
rzp = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

client = MongoClient(MONGO_URI)
db = client["sub_management"]
users_col = db["users"]
links_col = db["short_links"]
payments_col = db["payments"]

app = Flask(__name__)

BOT_USERNAME = bot.get_me().username


# ================= PLANS =================
PLANS = {
    "2880": {"name": "2 Days", "price": 50},
    "43200": {"name": "1 Month", "price": 200},
    "129600": {"name": "3 Months", "price": 650},
}


# ================= HELPERS =================
def now_ts():
    return int(datetime.now().timestamp())


def is_prime(uid):
    user = users_col.find_one({"user_id": uid})
    return user and int(user.get("expiry", 0)) > now_ts()


def add_prime(uid, mins):
    user = users_col.find_one({"user_id": uid})
    old_expiry = int(user.get("expiry", 0)) if user else 0
    base = max(old_expiry, now_ts())
    new_expiry = base + int(mins) * 60

    users_col.update_one(
        {"user_id": uid},
        {
            "$set": {
                "user_id": uid,
                "expiry": new_expiry,
                "updated_at": datetime.now()
            },
            "$setOnInsert": {
                "joined": datetime.now()
            }
        },
        upsert=True
    )
    return new_expiry


def format_time(ts):
    return datetime.fromtimestamp(int(ts)).strftime("%d-%m-%Y %I:%M %p")


def verify_rzp(body, signature):
    if not RAZORPAY_WEBHOOK_SECRET:
        print("WARNING: RAZORPAY_WEBHOOK_SECRET missing, signature check skipped")
        return True

    if not signature:
        return False

    generated = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(generated, signature)


def create_payment_link(uid, mins, price, fid):
    try:
        data = {
            "amount": int(price) * 100,
            "currency": "INR",
            "accept_partial": False,
            "description": f"Prime Access - {mins} minutes",
            "customer": {
                "name": f"User_{uid}"
            },
            "notify": {
                "sms": False,
                "email": False
            },
            "reminder_enable": False,
            "notes": {
                "user_id": str(uid),
                "mins": str(mins),
                "file_id": str(fid)
            },
            "callback_url": f"https://t.me/{BOT_USERNAME}",
            "callback_method": "get"
        }

        link = rzp.payment_link.create(data)
        return link.get("short_url")

    except Exception as e:
        print("Payment link error:", e)
        return None


# ================= FLASK =================
@app.route("/")
def home():
    return "OK Bot is running ✅", 200


@app.route("/health")
def health():
    return "OK ✅", 200


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") == "application/json":
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
        return "OK", 200

    return "Forbidden", 403


@app.route("/razorpay_webhook", methods=["POST"])
def razorpay_webhook():
    body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature")

    if not verify_rzp(body, signature):
        return "Invalid signature", 400

    data = request.json or {}

    if data.get("event") != "payment_link.paid":
        return "OK ignored", 200

    try:
        payment_link = data["payload"]["payment_link"]["entity"]
        payment = data["payload"]["payment"]["entity"]

        payment_id = payment.get("id")

        if payments_col.find_one({"payment_id": payment_id}):
            return "Already processed", 200

        notes = payment_link.get("notes", {})
        uid = int(notes.get("user_id"))
        mins = int(notes.get("mins"))
        fid = str(notes.get("file_id"))

        amount = int(payment.get("amount", 0)) // 100

        new_expiry = add_prime(uid, mins)

        payments_col.insert_one({
            "payment_id": payment_id,
            "user_id": uid,
            "amount": amount,
            "mins": mins,
            "file_id": fid,
            "created_at": datetime.now()
        })

        link_data = links_col.find_one({"file_id": fid})
        content = ""

        if link_data:
            content = f"\n\n🍿 *Your Link:*\n{link_data.get('url')}"

        bot.send_message(
            uid,
            f"✅ *Payment Successful!*\n\n"
            f"Prime activated.\n"
            f"Valid till: `{format_time(new_expiry)}`"
            f"{content}",
            disable_web_page_preview=True
        )

        bot.send_message(
            ADMIN_ID,
            f"💰 *Payment Success*\n\n"
            f"User: `{uid}`\n"
            f"Amount: ₹{amount}\n"
            f"Payment ID: `{payment_id}`"
        )

    except Exception as e:
        print("Razorpay webhook error:", e)
        try:
            bot.send_message(ADMIN_ID, f"⚠️ Webhook error:\n`{e}`")
        except:
            pass

    return "OK", 200


# ================= BOT =================
@bot.message_handler(commands=["start"])
def start(message):
    uid = message.from_user.id

    users_col.update_one(
        {"user_id": uid},
        {
            "$set": {
                "user_id": uid,
                "username": message.from_user.username,
                "first_name": message.from_user.first_name,
                "last_seen": datetime.now()
            },
            "$setOnInsert": {
                "joined": datetime.now(),
                "expiry": 0
            }
        },
        upsert=True
    )

    text = message.text or ""
    match = re.search(r"vid_([a-zA-Z0-9_-]+)", text)

    if match:
        fid = match.group(1)
        link_data = links_col.find_one({"file_id": fid})

        if not link_data:
            bot.send_message(uid, "❌ Link not found.")
            return

        if is_prime(uid):
            bot.send_message(
                uid,
                f"🍿 *Content Unlocked:*\n\n{link_data.get('url')}",
                disable_web_page_preview=True
            )
            return

        markup = InlineKeyboardMarkup()

        for mins, plan in PLANS.items():
            markup.add(
                InlineKeyboardButton(
                    f"💳 {plan['name']} - ₹{plan['price']}",
                    callback_data=f"pay|{fid}|{mins}"
                )
            )

        bot.send_message(
            uid,
            "🔒 *Subscription Required!*\n\nPlan select karo:",
            reply_markup=markup
        )
        return

    bot.send_message(
        uid,
        "👋 Bot working hai ✅\n\n"
        "Commands:\n"
        "/myplan - plan check\n\n"
        "Admin:\n"
        "/addlink fileid link"
    )


@bot.message_handler(commands=["myplan"])
def myplan(message):
    uid = message.from_user.id
    user = users_col.find_one({"user_id": uid})

    if not user or int(user.get("expiry", 0)) <= now_ts():
        bot.send_message(uid, "❌ Prime active nahi hai.")
        return

    bot.send_message(
        uid,
        f"✅ *Prime Active*\n\nValid till: `{format_time(user.get('expiry'))}`"
    )


@bot.message_handler(commands=["addlink"])
def addlink(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ Admin only.")
        return

    parts = message.text.split(" ", 2)

    if len(parts) < 3:
        bot.send_message(message.chat.id, "Use:\n`/addlink movie1 https://example.com/video`")
        return

    fid = parts[1].strip()
    url = parts[2].strip()

    links_col.update_one(
        {"file_id": fid},
        {
