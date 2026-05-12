import os
import re
import time
import hmac
import hashlib
from datetime import datetime, timedelta

import telebot
import razorpay
from flask import Flask, request
from pymongo import MongoClient
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


# ================= CONFIG CHECK =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_ID = os.getenv("ADMIN_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")

REQUIRED = {
    "BOT_TOKEN": BOT_TOKEN,
    "MONGO_URI": MONGO_URI,
    "ADMIN_ID": ADMIN_ID,
    "WEBHOOK_URL": WEBHOOK_URL,
    "RAZORPAY_KEY_ID": RAZORPAY_KEY_ID,
    "RAZORPAY_KEY_SECRET": RAZORPAY_KEY_SECRET,
    "RAZORPAY_WEBHOOK_SECRET": RAZORPAY_WEBHOOK_SECRET,
}

missing = [k for k, v in REQUIRED.items() if not v]
if missing:
    raise Exception(f"Missing ENV variables: {', '.join(missing)}")

ADMIN_ID = int(ADMIN_ID)
WEBHOOK_URL = WEBHOOK_URL.rstrip("/")


# ================= INIT =================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
rzp = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

client = MongoClient(MONGO_URI)
db = client["sub_management"]
users_col = db["users"]
links_col = db["short_links"]
payments_col = db["payments"]

app = Flask(__name__)


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
    current_expiry = int(user.get("expiry", 0)) if user else 0

    base_time = max(current_expiry, now_ts())
    new_expiry = base_time + int(mins) * 60

    users_col.update_one(
        {"user_id": uid},
        {
            "$set": {
                "user_id": uid,
                "expiry": new_expiry,
                "updated_at": datetime.now(),
            },
            "$setOnInsert": {
                "joined": datetime.now(),
            },
        },
        upsert=True,
    )
    return new_expiry


def format_expiry(ts):
    return datetime.fromtimestamp(int(ts)).strftime("%d-%m-%Y %I:%M %p")


def verify_razorpay_signature(body, signature):
    if not signature:
        return False

    generated = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(generated, signature)


def create_razorpay_link(uid, mins, price, fid):
    try:
        amount_paise = int(price) * 100

        data = {
            "amount": amount_paise,
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

        payment_link = rzp.payment_link.create(data)
        return payment_link.get("short_url")

    except Exception as e:
        print("Razorpay Link Error:", e)
        return None


# ================= BOT USERNAME =================
try:
    BOT_USERNAME = bot.get_me().username
except Exception as e:
    raise Exception(f"Bot token wrong or Telegram error: {e}")


# ================= FLASK ROUTES =================
@app.route("/")
def home():
    return "🚀 Razorpay Telegram Bot Live", 200


@app.route("/health")
def health():
    return {
        "status": "ok",
        "bot": BOT_USERNAME,
        "time": datetime.now().isoformat()
    }, 200


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

    if not verify_razorpay_signature(body, signature):
        print("Invalid Razorpay Signature")
        return "Invalid signature", 400

    data = request.json or {}

    if data.get("event") != "payment_link.paid":
        return "Ignored", 200

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
            "mins": mins,
            "file_id": fid,
            "amount": amount,
            "created_at": datetime.now(),
            "raw_status": payment.get("status")
        })

        link_data = links_col.find_one({"file_id": fid})
        content_text = ""

        if link_data:
            content_text = f"\n\n🍿 *Your Content Link:*\n{link_data.get('url')}"

        bot.send_message(
            uid,
            f"✅ *Payment Successful!*\n\n"
            f"Prime activated.\n"
            f"Valid till: `{format_expiry(new_expiry)}`"
            f"{content_text}",
            disable_web_page_preview=True
        )

        bot.send_message(
            ADMIN_ID,
            f"💰 *Razorpay Payment Success*\n\n"
            f"User ID: `{uid}`\n"
            f"Amount: ₹{amount}\n"
            f"Plan: {mins // 1440} days\n"
            f"Payment ID: `{payment_id}`"
        )

    except Exception as e:
        print("Webhook Processing Error:", e)
        try:
            bot.send_message(ADMIN_ID, f"⚠️ Razorpay webhook error:\n`{e}`")
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
            bot.send_message(uid, "❌ Content link not found.")
            return

        if is_prime(uid):
            bot.send_message(
                uid,
                f"🍿 *Content Unlocked:*\n\n{link_data.get('url')}",
                disable_web_page_preview=True
            )
            return

        markup = InlineKeyboardMarkup()

        for mins, p in PLANS.items():
            markup.add(
                InlineKeyboardButton(
                    f"💳 {p['name']} - ₹{p['price']}",
                    callback_data=f"pay|{fid}|{mins}"
                )
            )

        bot.send_message(
            uid,
            "🔒 *Subscription Required!*\n\n"
            "Payment ke baad Prime automatically activate ho jayega.",
            reply_markup=markup
        )
        return

    bot.send_message(
        uid,
        "👋 Welcome!\n\n"
        "Ye Razorpay auto subscription bot live hai.\n\n"
        "Commands:\n"
        "/myplan - apna plan check karo"
    )


@bot.message_handler(commands=["myplan"])
def myplan_cmd(message):
    uid = message.from_user.id
    user = users_col.find_one({"user_id": uid})

    if not user or int(user.get("expiry", 0)) <= now_ts():
        bot.send_message(uid, "❌ Aapka Prime active nahi hai.")
        return

    bot.send_message(
        uid,
        f"✅ *Prime Active*\n\nValid till: `{format_expiry(user.get('expiry'))}`"
    )


@bot.message_handler(commands=["addlink"])
def addlink_cmd(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "❌ Admin only.")
        return

    try:
        parts = message.text.split(" ", 2)

        if len(parts) < 3:
            bot.send_message(
                message.chat.id,
                "Use:\n`/addlink fileid https://example.com/video`"
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

        start_link = f"https://t.me/{BOT_USERNAME}?start=vid_{fid}"

        bot.send_message(
            message.chat.id,
            f"✅ Link added!\n\n"
            f"*User Start Link:*\n{start_link}",
            disable_web_page_preview=True
        )

    except Exception as e:
        bot.send_message(message.chat.id, f"Error: `{e}`")


@bot.callback_query_handler(func=lambda call: call.data.startswith("pay|"))
def payment_callback(call):
    try:
        bot.answer_callback_query(call.id, "Generating payment link...")

        _, fid, mins = call.data.split("|")
        uid = call.from_user.id

        plan = PLANS.get(mins)
        if not plan:
            bot.send_message(uid, "❌ Invalid plan.")
            return

        pay_url = create_razorpay_link(
            uid=uid,
            mins=int(mins),
            price=plan["price"],
            fid=fid
        )

        if not pay_url:
            bot.send_message(uid, "⚠️ Razorpay link create nahi hua. Admin se contact karo.")
            return

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🚀 Pay with Razorpay", url=pay_url))

        bot.send_message(
            uid,
            f"💰 *Amount:* ₹{plan['price']}\n"
            f"📦 *Plan:* {plan['name']}\n\n"
            f"Payment successful hote hi Prime automatic activate ho jayega.",
            reply_markup=markup
        )

    except Exception as e:
        print("Callback error:", e)
        bot.send_message(call.from_user.id, "⚠️ Error. Please try again.")


# ================= RUN =================
if __name__ == "__main__":
    print("Bot username:", BOT_USERNAME)

    bot.remove_webhook()
    time.sleep(1)

    webhook_url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
    bot.set_webhook(url=webhook_url)

    print("Telegram webhook set:", webhook_url)
    print("Razorpay webhook URL:", f"{WEBHOOK_URL}/razorpay_webhook")

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
