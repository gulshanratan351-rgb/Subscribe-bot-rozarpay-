import os
import telebot
import urllib.parse
import uuid
import re
import time

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from datetime import datetime, timedelta
from flask import Flask, request
import razorpay

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
UPI_ID = os.getenv("UPI_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

# ================= BOT =================

bot = telebot.TeleBot(BOT_TOKEN)

client = MongoClient(MONGO_URI)
db = client["sub_management"]

users_col = db["users"]
links_col = db["short_links"]
temp_pay_col = db["temp_payments"]

rz_client = razorpay.Client(
    auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
)

# ================= PLANS =================

PLANS = {
    "2880": "50",      # 2 Days
    "10080": "100",   # 7 Days
    "43200": "250",   # 1 Month
    "129600": "800"   # 3 Months
}

# ================= FLASK =================

app = Flask(__name__)

@app.route("/")
def home():
    return "🚀 Master Bot is Online and Healthy!"

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "OK", 200
    return "Forbidden", 403

# ================= HELPERS =================

def is_prime(uid):
    user = users_col.find_one({"user_id": uid})

    if user and user.get("expiry", 0) > datetime.now().timestamp():
        return True

    return False

def get_expiry_date(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%d %b %Y, %I:%M %p")

# ================= ADMIN COMMANDS =================

@bot.message_handler(commands=["stats"])
def stats_handler(message):

    if message.from_user.id != ADMIN_ID:
        return

    total_users = users_col.count_documents({})
    active_prime = users_col.count_documents({
        "expiry": {"$gt": datetime.now().timestamp()}
    })

    total_links = links_col.count_documents({})

    text = (
        f"📊 BOT STATS\n\n"
        f"👤 Users: {total_users}\n"
        f"👑 Prime Users: {active_prime}\n"
        f"🔗 Links: {total_links}"
    )

    bot.reply_to(message, text)

@bot.message_handler(commands=["short"])
def short_link(message):

    if message.from_user.id != ADMIN_ID:
        return

    msg = bot.reply_to(
        message,
        "🔗 Send Original Link"
    )

    bot.register_next_step_handler(msg, save_link)

def save_link(message):

    file_id = str(uuid.uuid4())[:8].lower()

    links_col.insert_one({
        "file_id": file_id,
        "url": message.text
    })

    short_url = f"https://t.me/{bot.get_me().username}?start=vid_{file_id}"

    bot.send_message(
        ADMIN_ID,
        f"✅ LINK CREATED\n\n{short_url}"
    )

@bot.message_handler(commands=["broadcast"])
def broadcast(message):

    if message.from_user.id != ADMIN_ID:
        return

    msg = bot.reply_to(
        message,
        "📢 Send Broadcast Message"
    )

    bot.register_next_step_handler(msg, send_broadcast)

def send_broadcast(message):

    users = users_col.find({})

    count = 0

    for user in users:

        try:
            bot.copy_message(
                user["user_id"],
                ADMIN_ID,
                message.message_id
            )

            count += 1
            time.sleep(0.1)

        except:
            pass

    bot.send_message(
        ADMIN_ID,
        f"✅ Broadcast Sent To {count} Users"
    )

# ================= START =================

@bot.message_handler(commands=["start"])
def start_handler(message):

    uid = message.from_user.id

    users_col.update_one(
        {"user_id": uid},
        {"$setOnInsert": {"joined": datetime.now()}},
        upsert=True
    )

    match = re.search(r"vid_([a-zA-Z0-9]+)", message.text)

    if match:

        fid = match.group(1)

        if is_prime(uid):

            link_data = links_col.find_one({
                "file_id": fid
            })

            if link_data:

                bot.send_message(
                    uid,
                    f"🍿 YOUR CONTENT\n\n{link_data['url']}",
                    disable_web_page_preview=True
                )

            else:
                bot.send_message(uid, "❌ Link Not Found")

        else:

            markup = InlineKeyboardMarkup()

            markup.row(
                InlineKeyboardButton(
                    "💳 2 Days ₹50",
                    callback_data=f"pay_{fid}_2880_50"
                )
            )

            markup.row(
                InlineKeyboardButton(
                    "💳 7 Days ₹100",
                    callback_data=f"pay_{fid}_10080_100"
                )
            )

            markup.row(
                InlineKeyboardButton(
                    "💳 1 Month ₹250",
                    callback_data=f"pay_{fid}_43200_250"
                )
            )

            markup.row(
                InlineKeyboardButton(
                    "💳 3 Months ₹800",
                    callback_data=f"pay_{fid}_129600_800"
                )
            )

            bot.send_message(
                uid,
                "🔒 Select Plan To Unlock",
                reply_markup=markup
            )

    else:

        text = "👋 Welcome To DV Prime Bot\n\n"

        if is_prime(uid):

            u = users_col.find_one({"user_id": uid})

            text += (
                f"👑 PRIME USER\n"
                f"📅 Expiry:\n{get_expiry_date(u['expiry'])}"
            )

        else:

            text += "❌ FREE USER"

        bot.send_message(uid, text)

# ================= PAYMENT =================

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def payment_handler(call):

    bot.answer_callback_query(call.id)

    try:

        _, fid, mins, price = call.data.split("_")

        amount = int(price) * 100

        order = rz_client.order.create({
            "amount": amount,
            "currency": "INR",
            "payment_capture": "1"
        })

        pay_link = (
            f"https://api.razorpay.com/v1/checkout/embedded?"
            f"order_id={order['id']}"
        )

        temp_pay_col.update_one(
            {"user_id": call.from_user.id},
            {
                "$set": {
                    "mins": mins,
                    "fid": fid,
                    "price": price
                }
            },
            upsert=True
        )

        markup = InlineKeyboardMarkup()

        markup.add(
            InlineKeyboardButton(
                "💳 PAY NOW",
                url=pay_link
            )
        )

        markup.add(
            InlineKeyboardButton(
                "✅ I PAID",
                callback_data=f"checkpay_{call.from_user.id}"
            )
        )

        bot.send_message(
            call.message.chat.id,
            f"💰 Amount: ₹{price}\n\nClick Below To Pay",
            reply_markup=markup
        )

    except Exception as e:

        bot.send_message(
            call.message.chat.id,
            f"❌ Error:\n{str(e)}"
        )

# ================= PAYMENT VERIFY =================

@bot.callback_query_handler(func=lambda call: call.data.startswith("checkpay_"))
def verify_payment(call):

    uid = int(call.data.split("_")[1])

    pay_data = temp_pay_col.find_one({
        "user_id": uid
    })

    if not pay_data:
        return

    expiry = int(
        (
            datetime.now() +
            timedelta(minutes=int(pay_data["mins"]))
        ).timestamp()
    )

    users_col.update_one(
        {"user_id": uid},
        {"$set": {"expiry": expiry}},
        upsert=True
    )

    link_data = links_col.find_one({
        "file_id": pay_data["fid"]
    })

    if link_data:

        bot.send_message(
            uid,
            f"✅ PAYMENT SUCCESS\n\n🍿 LINK:\n{link_data['url']}"
        )

    else:

        bot.send_message(
            uid,
            "✅ PRIME ACTIVATED"
        )

    temp_pay_col.delete_one({
        "user_id": uid
    })

# ================= WEBHOOK =================

try:

    bot.remove_webhook()
    time.sleep(1)

    bot.set_webhook(
        url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
    )

    print("Webhook Set ✅")

except Exception as e:

    print("Webhook Error:", e)

# ================= RUN =================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000))
    )
