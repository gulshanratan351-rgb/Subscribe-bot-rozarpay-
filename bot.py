import os
import re
import time
import uuid
import hmac
import hashlib
import threading
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


# ================= INIT =================
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
    "129600": {"name": "3 Months", "price": 650}
}


# ================= HELPERS =================
def now_ts():
    return int(datetime.now().timestamp())


def format_time(ts):
    return datetime.fromtimestamp(int(ts)).strftime("%d-%m-%Y %I:%M %p")


def is_prime(uid):
    user = users_col.find_one({"user_id": uid})
    if not user:
        return False
    return int(user.get("expiry", 0)) > now_ts()


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
                "expired": False,
                "updated_at": datetime.now()
            },
            "$setOnInsert": {
                "joined": datetime.now()
            }
        },
        upsert=True
    )

    return new_expiry


def verify_signature(body, signature):
    if not RAZORPAY_WEBHOOK_SECRET:
        print("WARNING: RAZORPAY_WEBHOOK_SECRET missing")
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
            "customer": {"name": f"User_{uid}"},
            "notify": {"sms": False, "email": False},
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


def remove_expired_users():
    while True:
        try:
            users_col.update_many(
                {
                    "expiry": {"$lte": now_ts()},
                    "expiry": {"$gt": 0}
                },
                {
                    "$set": {
                        "expired": True
                    }
                }
            )
        except Exception as e:
            print("Expire Check Error:", e)

        time.sleep(300)


# ================= FLASK ROUTES =================
@app.route("/")
def home():
    return "Bot Running ✅", 200


@app.route("/health")
def health():
    return "OK ✅", 200


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") == "application/json":
        update = telebot.types.Update.de_json(
            request.get_data().decode("utf-8")
        )
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

        if payments_col.find_one({"payment_id": payment_id}):
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

        link_data = links_col.find_one({"file_id": fid})
        content = ""

        if link_data:
            content = f"\n\n🍿 *Your Content Link:*\n{link_data.get('url')}"

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
            bot.send_message(ADMIN_ID, f"⚠️ Webhook Error:\n`{e}`")
        except:
            pass

    return "OK", 200


# ================= BOT COMMANDS =================
@bot.message_handler(commands=["start"])
def start_cmd(message):
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
                "expiry": 0,
                "expired": False
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
        "👋 *Bot Working Hai* ✅\n\n"
        "/myplan - Plan Check"
    )


@bot.message_handler(commands=["myplan"])
def myplan_cmd(message):
    uid = message.from_user.id
    user = users_col.find_one({"user_id": uid})

    if not user:
        bot.send_message(uid, "❌ No Active Plan.")
        return

    expiry = int(user.get("expiry", 0))

    if expiry <= now_ts():
        users_col.update_one(
            {"user_id": uid},
            {"$set": {"expired": True}}
        )
        bot.send_message(uid, "❌ Prime Expired.")
        return

    bot.send_message(
        uid,
        f"✅ *Prime Active*\n\nValid Till: `{format_time(expiry)}`"
    )


@bot.message_handler(commands=["short"])
def short_cmd(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ Admin Only")
        return

    parts = message.text.split(" ", 1)

    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "Use:\n`/short https://example.com/video`"
        )
        return

    url = parts[1].strip()

    if not url.startswith("http"):
        bot.send_message(
            message.chat.id,
            "❌ Valid URL do.\nExample:\n`/short https://google.com`"
        )
        return

    fid = uuid.uuid4().hex[:8]

    links_col.insert_one({
        "file_id": fid,
        "url": url,
        "created_at": datetime.now(),
        "created_by": message.from_user.id
    })

    start_link = f"https://t.me/{BOT_USERNAME}?start=vid_{fid}"

    bot.send_message(
        message.chat.id,
        f"✅ *Prime Lock Link Created*\n\n"
        f"`{start_link}`\n\n"
        f"Prime user ko link milega.\n"
        f"Non-prime user ko plan dikhega.",
        disable_web_page_preview=True
    )


@bot.message_handler(commands=["stats"])
def stats_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return

    total_users = users_col.count_documents({})
    active_users = users_col.count_documents({
        "expiry": {"$gt": now_ts()}
    })
    total_links = links_col.count_documents({})
    total_payments = payments_col.count_documents({})

    bot.send_message(
        message.chat.id,
        f"📊 *Bot Stats*\n\n"
        f"👥 Total Users: `{total_users}`\n"
        f"💎 Prime Users: `{active_users}`\n"
        f"🔗 Total Links: `{total_links}`\n"
        f"💰 Payments: `{total_payments}`"
    )


@bot.message_handler(commands=["approve"])
def approve_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()

    if len(parts) < 3:
        bot.send_message(
            message.chat.id,
            "Use:\n`/approve USER_ID DAYS`"
        )
        return

    try:
        uid = int(parts[1])
        days = int(parts[2])
        mins = days * 1440

        expiry = add_prime(uid, mins)

        bot.send_message(
            uid,
            f"✅ *Prime Approved By Admin*\n\n"
            f"Valid Till: `{format_time(expiry)}`"
        )

        bot.send_message(
            message.chat.id,
            f"✅ User Approved\n\nUser: `{uid}`"
        )

    except Exception as e:
        bot.send_message(message.chat.id, f"Error:\n`{e}`")


@bot.message_handler(commands=["deactivate"])
def deactivate_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()

    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "Use:\n`/deactivate USER_ID`"
        )
        return

    try:
        uid = int(parts[1])

        users_col.update_one(
            {"user_id": uid},
            {
                "$set": {
                    "expiry": 0,
                    "expired": True
                }
            }
        )

        bot.send_message(message.chat.id, "❌ Prime Removed")

        try:
            bot.send_message(uid, "❌ Your Prime Has Been Removed")
        except:
            pass

    except Exception as e:
        bot.send_message(message.chat.id, f"Error:\n`{e}`")


broadcast_mode = {}


@bot.message_handler(commands=["broadcast"])
def broadcast_cmd(message):
    if message.from_user.id != ADMIN_ID:
        return

    broadcast_mode[message.from_user.id] = True

    bot.send_message(
        message.chat.id,
        "📢 Ab message bhejo.\n\nYe sab users ko send hoga."
    )


@bot.message_handler(func=lambda m: m.from_user.id in broadcast_mode)
def send_broadcast(message):
    if message.from_user.id != ADMIN_ID:
        return

    users = users_col.find({})

    sent = 0
    failed = 0

    for user in users:
        try:
            bot.copy_message(
                user["user_id"],
                message.chat.id,
                message.message_id
            )
            sent += 1
        except:
            failed += 1

    del broadcast_mode[message.from_user.id]

    bot.send_message(
        message.chat.id,
        f"✅ Broadcast Done\n\n"
        f"Sent: `{sent}`\n"
        f"Failed: `{failed}`"
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("pay|"))
def payment_callback(call):
    try:
        bot.answer_callback_query(call.id, "Payment link bana raha hu...")

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
            bot.send_message(uid, "⚠️ Payment Link Error")
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
            f"Payment ke baad automatic Prime activate ho jayega.",
            reply_markup=markup
        )

    except Exception as e:
        print("Callback Error:", e)
        bot.send_message(call.from_user.id, "⚠️ Error Aa Gaya")


# ================= RUN =================
if __name__ == "__main__":
    print("Bot Username:", BOT_USERNAME)

    threading.Thread(
        target=remove_expired_users,
        daemon=True
    ).start()

    bot.remove_webhook()
    time.sleep(1)

    webhook = f"{WEBHOOK_URL}/{BOT_TOKEN}"
    bot.set_webhook(url=webhook)

    print("Webhook Set:", webhook)

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
)
