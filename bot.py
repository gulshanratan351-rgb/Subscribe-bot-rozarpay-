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
    "2880": {
        "name": "2 Days",
        "price": 50
    },
    "43200": {
        "name": "1 Month",
        "price": 200
    },
    "129600": {
        "name": "3 Months",
        "price": 650
    }
}


# ================= HELPERS =================
def now_ts():
    return int(datetime.now().timestamp())


def is_prime(uid):
    user = users_col.find_one({"user_id": uid})

    if not user:
        return False

    return int(user.get("expiry", 0)) > now_ts()


def add_prime(uid, mins):
    user = users_col.find_one({"user_id": uid})

    old_expiry = 0

    if user:
        old_expiry = int(user.get("expiry", 0))

    base = max(old_expiry, now_ts())

    new_expiry = base + (int(mins) * 60)

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
    return datetime.fromtimestamp(
        int(ts)
    ).strftime("%d-%m-%Y %I:%M %p")


def verify_signature(body, signature):

    if not RAZORPAY_WEBHOOK_SECRET:
        print("Webhook secret missing")
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
            "description": f"Prime Access - {mins} mins",
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

        payment = rzp.payment_link.create(data)

        return payment.get("short_url")

    except Exception as e:

        print("Payment Link Error:", e)

        return None


# ================= FLASK =================
@app.route("/")
def home():
    return "Bot Running ✅", 200


@app.route("/health")
def health():
    return "OK", 200


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():

    if request.headers.get("content-type") == "application/json":

        json_string = request.get_data().decode("utf-8")

        update = telebot.types.Update.de_json(json_string)

        bot.process_new_updates([update])

        return "OK", 200

    return "Forbidden", 403


@app.route("/razorpay_webhook", methods=["POST"])
def razorpay_webhook():

    body = request.get_data()

    signature = request.headers.get("X-Razorpay-Signature")

    if not verify_signature(body, signature):
        return "Invalid Signature", 400

    data = request.json or {}

    if data.get("event") != "payment_link.paid":
        return "Ignored", 200

    try:

        payment_link = data["payload"]["payment_link"]["entity"]

        payment = data["payload"]["payment"]["entity"]

        payment_id = payment.get("id")

        already = payments_col.find_one({
            "payment_id": payment_id
        })

        if already:
            return "Already Processed", 200

        notes = payment_link.get("notes", {})

        uid = int(notes.get("user_id"))

        mins = int(notes.get("mins"))

        fid = str(notes.get("file_id"))

        amount = int(payment.get("amount", 0)) // 100

        expiry = add_prime(uid, mins)

        payments_col.insert_one({
            "payment_id": payment_id,
            "user_id": uid,
            "amount": amount,
            "mins": mins,
            "file_id": fid,
            "created_at": datetime.now()
        })

        link_data = links_col.find_one({
            "file_id": fid
        })

        content = ""

        if link_data:
            content = (
                f"\n\n🍿 *Your Content Link:*\n"
                f"{link_data.get('url')}"
            )

        bot.send_message(
            uid,
            f"✅ *Payment Successful!*\n\n"
            f"Prime Activated.\n"
            f"Valid Till: `{format_time(expiry)}`"
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

        print("Webhook Error:", e)

        try:
            bot.send_message(
                ADMIN_ID,
                f"⚠️ Webhook Error:\n`{e}`"
            )
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

        link_data = links_col.find_one({
            "file_id": fid
        })

        if not link_data:
            bot.send_message(uid, "❌ Link not found.")
            return

        if is_prime(uid):

            bot.send_message(
                uid,
                f"🍿 *Content Unlocked:*\n\n"
                f"{link_data.get('url')}",
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
            "🔒 *Subscription Required!*\n\n"
            "Plan Select Karo:",
            reply_markup=markup
        )

        return

    bot.send_message(
        uid,
        "👋 Bot Working Hai ✅\n\n"
        "/myplan - Plan Check"
    )


@bot.message_handler(commands=["myplan"])
def myplan(message):

    uid = message.from_user.id

    user = users_col.find_one({
        "user_id": uid
    })

    if not user:
        bot.send_message(uid, "❌ No Active Plan.")
        return

    expiry = int(user.get("expiry", 0))

    if expiry <= now_ts():
        bot.send_message(uid, "❌ Prime Expired.")
        return

    bot.send_message(
        uid,
        f"✅ *Prime Active*\n\n"
        f"Valid Till: `{format_time(expiry)}`"
    )


@bot.message_handler(commands=["addlink"])
def addlink(message):

    if message.from_user.id != ADMIN_ID:
        bot.send_message(
            message.chat.id,
            "❌ Admin Only"
        )
        return

    parts = message.text.split(" ", 2)

    if len(parts) < 3:

        bot.send_message(
            message.chat.id,
            "Use:\n"
            "`/addlink movie1 https://example.com/video`"
        )

        return

    fid = parts[1].strip()

    url = parts[2].strip()

    links_col.update_one(
        {"file_id": fid},
        {
            "$set": {
                "file_id": fid,
                "url": url,
                "created_at": datetime.now()
            }
        },
        upsert=True
    )

    start_link = (
        f"https://t.me/{BOT_USERNAME}?start=vid_{fid}"
    )

    bot.send_message(
        message.chat.id,
        f"✅ Link Added\n\n{start_link}",
        disable_web_page_preview=True
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("pay|")
)
def payment_callback(call):

    try:

        bot.answer_callback_query(
            call.id,
            "Payment Link Bana Raha Hu..."
        )

        _, fid, mins = call.data.split("|")

        uid = call.from_user.id

        plan = PLANS.get(mins)

        if not plan:
            bot.send_message(uid, "❌ Invalid Plan")
            return

        pay_url = create_payment_link(
            uid,
            mins,
            plan["price"],
            fid
        )

        if not pay_url:
            bot.send_message(
                uid,
                "⚠️ Payment Link Error"
            )
            return

        markup = InlineKeyboardMarkup()

        markup.add(
            InlineKeyboardButton(
                "🚀 Pay Now",
                url=pay_url
            )
        )

        bot.send_message(
            uid,
            f"💰 *Amount:* ₹{plan['price']}\n"
            f"📦 *Plan:* {plan['name']}\n\n"
            f"Payment Ke Baad Auto Activate Ho Jayega.",
            reply_markup=markup
        )

    except Exception as e:

        print("Callback Error:", e)

        bot.send_message(
            call.from_user.id,
            "⚠️ Error Aa Gaya"
        )


# ================= RUN =================
if __name__ == "__main__":

    print("Bot Username:", BOT_USERNAME)

    bot.remove_webhook()

    time.sleep(1)

    webhook = f"{WEBHOOK_URL}/{BOT_TOKEN}"

    bot.set_webhook(url=webhook)

    print("Webhook Set:", webhook)

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
