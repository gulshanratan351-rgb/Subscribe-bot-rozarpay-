import os, telebot, urllib.parse, uuid, datetime, re, threading, random, time
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from datetime import datetime, timedelta
from flask import Flask, request

# ================= CONFIGURATION =================
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
ADMIN_ID = int(os.getenv('ADMIN_ID'))
UPI_ID = os.getenv('UPI_ID')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')

bot = telebot.TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URI)
db = client['sub_management']
users_col = db['users']
links_col = db['short_links']
temp_pay_col = db['temp_payments']

PLANS = {
    "2880": "50",
    "10080": "100",
    "43200": "200",
    "129600": "400"
}

app = Flask(__name__)

@app.route('/')
def home():
    return "🚀 Master Bot is Online and Healthy!"

@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "OK", 200
    return "Forbidden", 403

def is_prime(uid):
    user = users_col.find_one({"user_id": uid})
    return user and user.get('expiry', 0) > datetime.now().timestamp()

def get_expiry_date(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%d %b %Y, %I:%M %p')

# ================= ADMIN COMMANDS =================

@bot.message_handler(commands=['stats'], func=lambda m: m.from_user.id == ADMIN_ID)
def stats_handler(message):
    total_users = users_col.count_documents({})
    active_prime = users_col.count_documents({"expiry": {"$gt": datetime.now().timestamp()}})
    total_links = links_col.count_documents({})

    text = (
        f"📊 **Bot Statistics**\n\n"
        f"👤 Total Users: {total_users}\n"
        f"👑 Active Prime: {active_prime}\n"
        f"🔗 Total Links: {total_links}"
    )
    bot.reply_to(message, text)

@bot.message_handler(commands=['approve'], func=lambda m: m.from_user.id == ADMIN_ID)
def manual_approve(message):
    try:
        args = message.text.split()
        if len(args) < 3:
            return bot.reply_to(message, "❌ Format: /approve User_ID Days")

        target_id = int(args[1])
        days = int(args[2])
        expiry = int((datetime.now() + timedelta(days=days)).timestamp())

        users_col.update_one(
            {"user_id": target_id},
            {"$set": {"expiry": expiry}},
            upsert=True
        )

        bot.send_message(target_id, f"✅ Congratulations!\nAdmin has activated your Prime for {days} days.")
        bot.reply_to(message, f"✅ User {target_id} approved for {days} days.")

    except Exception as e:
        bot.reply_to(message, f"⚠️ Error: {str(e)}")

@bot.message_handler(commands=['unapprove', 'deactivate'], func=lambda m: m.from_user.id == ADMIN_ID)
def deapprove_user(message):
    try:
        target_id = None

        if message.reply_to_message:
            target_id = message.reply_to_message.from_user.id
        else:
            args = message.text.split()
            if len(args) > 1:
                target_id = int(args[1])

        if target_id:
            users_col.update_one({"user_id": target_id}, {"$set": {"expiry": 0}})
            bot.send_message(target_id, "❌ Your Prime membership has been revoked by Admin.")
            bot.reply_to(message, f"✅ User {target_id} ko Deapprove kar diya gaya hai.")
        else:
            bot.reply_to(message, "❌ Use: /unapprove 12345")

    except Exception as e:
        bot.reply_to(message, f"⚠️ Error: {str(e)}")

@bot.message_handler(commands=['broadcast'], func=lambda m: m.from_user.id == ADMIN_ID)
def broadcast_msg(message):
    msg = bot.send_message(ADMIN_ID, "📢 Send the message you want to broadcast to ALL users:")
    bot.register_next_step_handler(msg, start_broadcasting)

def start_broadcasting(message):
    all_users = users_col.find({})
    count = 0

    for user in all_users:
        try:
            bot.copy_message(user['user_id'], ADMIN_ID, message.message_id)
            count += 1
            time.sleep(0.1)
        except:
            pass

    bot.send_message(ADMIN_ID, f"✅ Broadcast sent to {count} users.")

@bot.message_handler(commands=['short'], func=lambda m: m.from_user.id == ADMIN_ID)
def short_link(message):
    msg = bot.reply_to(message, "🔗 Paste the original link to shorten:")
    bot.register_next_step_handler(msg, save_link)

def save_link(message):
    file_id = str(uuid.uuid4())[:8].lower()

    links_col.insert_one({
        "file_id": file_id,
        "url": message.text
    })

    bot.send_message(
        ADMIN_ID,
        f"✅ Link Created!\n\nURL: https://t.me/{bot.get_me().username}?start=vid_{file_id}"
    )

# ================= USER LOGIC =================

@bot.message_handler(commands=['start'])
def handle_start(message):
    uid = message.from_user.id

    users_col.update_one(
        {"user_id": uid},
        {"$setOnInsert": {"joined": datetime.now()}},
        upsert=True
    )

    # OLD + NEW LINK SUPPORT
    match = re.search(r'(?:vid_)?([a-zA-Z0-9]{8})', message.text)

    if match:
        fid = match.group(1)

        if is_prime(uid):
            link_data = links_col.find_one({"file_id": fid})

            if link_data:
                bot.send_message(
                    uid,
                    f"🍿 **Your Content is Ready:**\n\n{link_data['url']}",
                    disable_web_page_preview=True
                )
            else:
                bot.send_message(uid, "❌ Link expired or removed.")

        else:
            markup = InlineKeyboardMarkup(row_width=2)

            markup.add(
                InlineKeyboardButton("⚡ 2 Days\n₹50", callback_data=f"pay_{fid}_2880_50"),
                InlineKeyboardButton("🔥 7 Days\n₹100", callback_data=f"pay_{fid}_10080_100")
            )

            markup.add(
                InlineKeyboardButton("👑 1 Month\n₹200", callback_data=f"pay_{fid}_43200_200"),
                InlineKeyboardButton("💎 3 Months\n₹400", callback_data=f"pay_{fid}_129600_400")
            )

            bot.send_message(
                uid,
                "🔒 **Membership Required!**\n\n"
                "✨ Upgrade to Prime and unlock premium content instantly.\n\n"
                "💳 Select your plan below:",
                reply_markup=markup
            )

    else:
        text = "👋 Welcome to the Movie Bot!\n\n"

        if is_prime(uid):
            u = users_col.find_one({"user_id": uid})
            text += f"👑 Status: **Prime User**\n📅 Expiry: `{get_expiry_date(u['expiry'])}`"
        else:
            text += "👑 Status: **Free User**\nJoin Prime to access premium links."

        bot.send_message(uid, text)

@bot.callback_query_handler(func=lambda call: call.data.startswith('pay_'))
def show_qr(call):
    bot.answer_callback_query(call.id)

    try:
        data_parts = call.data.split('_')
        if len(data_parts) < 4:
            return

        _, fid, mins, price = data_parts
        uid = call.from_user.id

        temp_pay_col.update_one(
            {"user_id": uid},
            {"$set": {"mins": mins, "fid": fid, "price": price}},
            upsert=True
        )

        upi_params = {
            "pa": UPI_ID,
            "pn": "VRPRIME",
            "am": price,
            "cu": "INR",
            "tn": f"User{uid}"
        }

        upi_url = f"upi://pay?{urllib.parse.urlencode(upi_params)}"
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={urllib.parse.quote(upi_url)}"

        caption = (
            f"💰 **Plan Amount: ₹{price}**\n\n"
            f"1️⃣ QR Code scan karein.\n"
            f"2️⃣ Ya UPI ID copy karein:\n"
            f"👉 `{UPI_ID}`\n\n"
            f"3️⃣ Payment ke baad screenshot yahan bhejein.\n\n"
            f"✅ Amount auto-fill ho jayega."
        )

        bot.send_photo(
            call.message.chat.id,
            qr_url,
            caption=caption,
            parse_mode="Markdown"
        )

    except Exception as e:
        bot.send_message(call.message.chat.id, f"⚠️ Error: {str(e)}")

@bot.message_handler(content_types=['photo'])
def process_screenshot(message):
    uid = message.from_user.id

    if uid == ADMIN_ID:
        return

    pending = temp_pay_col.find_one({"user_id": uid})

    if pending:
        bot.send_message(uid, "⏳ Screenshot Received!\nAdmin is verifying. You will be notified shortly.")

        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("Approve ✅", callback_data=f"adm_ok_{uid}"),
            InlineKeyboardButton("Reject ❌", callback_data=f"adm_no_{uid}")
        )

        bot.send_photo(
            ADMIN_ID,
            message.photo[-1].file_id,
            caption=f"📩 New Payment!\nUser: {uid}\nPlan: ₹{pending['price']}",
            reply_markup=markup
        )
    else:
        bot.send_message(uid, "❌ Please select a plan first by clicking a movie link.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('adm_'))
def handle_admin_decision(call):
    _, decision, uid = call.data.split('_')
    uid = int(uid)

    if decision == "ok":
        pay_data = temp_pay_col.find_one({"user_id": uid})

        if pay_data:
            expiry = int((datetime.now() + timedelta(minutes=int(pay_data['mins']))).timestamp())

            users_col.update_one(
                {"user_id": uid},
                {"$set": {"expiry": expiry}},
                upsert=True
            )

            l_data = links_col.find_one({"file_id": pay_data['fid']})

            if l_data:
                msg = f"✅ Payment Approved!\n\n🎁 Your Link: {l_data['url']}"
            else:
                msg = "✅ Payment Approved! Access granted."

            bot.send_message(uid, msg)
            bot.edit_message_caption("✅ User Approved!", ADMIN_ID, call.message.message_id)
            temp_pay_col.delete_one({"user_id": uid})

    else:
        bot.send_message(uid, "❌ Payment Rejected!\nPlease send a valid screenshot or contact admin.")
        bot.edit_message_caption("❌ User Rejected!", ADMIN_ID, call.message.message_id)

# ================= RUNNER =================

if __name__ == '__main__':
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
