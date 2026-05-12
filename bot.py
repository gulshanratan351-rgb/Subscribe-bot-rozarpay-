import os, time, uuid, urllib.parse, threading
from datetime import datetime
from flask import Flask
from pymongo import MongoClient
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from bson import ObjectId

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
UPI_ID = os.getenv("UPI_ID", "").strip()
CONTACT_USERNAME = os.getenv("CONTACT_USERNAME", "").replace("@", "").strip()

if not BOT_TOKEN: raise Exception("BOT_TOKEN missing")
if not MONGO_URI: raise Exception("MONGO_URI missing")
if not ADMIN_ID: raise Exception("ADMIN_ID missing")
if not UPI_ID: raise Exception("UPI_ID missing")
if not CONTACT_USERNAME: raise Exception("CONTACT_USERNAME missing")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
client = MongoClient(MONGO_URI)
db = client["sub_management"]

users_col = db["users"]
links_col = db["short_links"]
channels_col = db["channels"]
requests_col = db["payment_requests"]

BOT_USERNAME = bot.get_me().username

PLANS = {
    "2880": {"name": "2 Days", "price": 50},
    "43200": {"name": "1 Month", "price": 200},
    "129600": {"name": "3 Months", "price": 650},
}

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot Running OK ✅", 200

@app.route("/health")
def health():
    return "OK", 200

def web_run():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

def now_ts():
    return int(datetime.now().timestamp())

def fmt(ts):
    return datetime.fromtimestamp(int(ts)).strftime("%d-%m-%Y %I:%M %p")

def is_admin(uid):
    return int(uid) == ADMIN_ID

def qr_url(price):
    upi = f"upi://pay?pa={UPI_ID}&pn=Prime Access&am={price}&cu=INR"
    return "https://api.qrserver.com/v1/create-qr-code/?size=350x350&data=" + urllib.parse.quote(upi)

def save_user(user):
    users_col.update_one(
        {"user_id": user.id},
        {"$set": {
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_seen": datetime.now()
        }, "$setOnInsert": {"joined": datetime.now(), "expiry": 0}},
        upsert=True
    )

def is_prime(uid):
    u = users_col.find_one({"user_id": int(uid)})
    return bool(u and int(u.get("expiry", 0)) > now_ts())

def add_prime(uid, mins):
    u = users_col.find_one({"user_id": int(uid)}) or {}
    old = int(u.get("expiry", 0))
    expiry = max(old, now_ts()) + int(mins) * 60
    users_col.update_one(
        {"user_id": int(uid)},
        {"$set": {"expiry": expiry, "active": True, "updated_at": datetime.now()}},
        upsert=True
    )
    return expiry

def set_menu():
    bot.set_my_commands([
        BotCommand("short", "Create Link"),
        BotCommand("stats", "Check Users"),
        BotCommand("broadcast", "Message All"),
        BotCommand("approve", "ID Days"),
        BotCommand("deactivate", "ID"),
    ])

@bot.message_handler(commands=["start"])
def start(message):
    save_user(message.from_user)
    uid = message.from_user.id
    parts = message.text.split()

    if len(parts) > 1:
        code = parts[1]

        if code.startswith("vid_"):
            fid = code.replace("vid_", "")
            data = links_col.find_one({"file_id": fid})

            if not data:
                bot.send_message(uid, "❌ Link not found.")
                return

            if is_prime(uid):
                bot.send_message(uid, f"🍿 <b>Content Unlocked:</b>\n\n{data['url']}", disable_web_page_preview=True)
                return

            markup = InlineKeyboardMarkup()
            for mins, p in PLANS.items():
                markup.add(InlineKeyboardButton(f"💳 {p['name']} - ₹{p['price']}", callback_data=f"oldpay|{fid}|{mins}"))

            bot.send_message(uid, "🔒 <b>Prime Required</b>\n\nPlan select karo:", reply_markup=markup)
            return

        try:
            ch_id = int(code)
            ch = channels_col.find_one({"channel_id": ch_id})

            if not ch:
                bot.send_message(uid, "❌ Channel data not found.")
                return

            active = users_col.find_one({"user_id": uid, "channel_id": ch_id, "expiry": {"$gt": now_ts()}, "active": True})

            if active:
                bot.send_message(uid, f"✅ Subscription active hai.\nValid till: <code>{fmt(active['expiry'])}</code>")
                return

            markup = InlineKeyboardMarkup()
            for mins, price in ch.get("plans", {}).items():
                markup.add(InlineKeyboardButton(f"💳 {mins} Min - ₹{price}", callback_data=f"newpay|{ch_id}|{mins}"))

            bot.send_message(uid, f"📢 <b>{ch.get('name','Private Channel')}</b>\n\nPlan select karo:", reply_markup=markup)
            return

        except:
            bot.send_message(uid, "❌ Invalid link.")
            return

    if is_admin(uid):
        bot.send_message(uid, "✅ <b>Admin Panel Active</b>\n\n/short URL\n/stats\n/broadcast\n/approve USER_ID DAYS\n/deactivate USER_ID")
    else:
        bot.send_message(uid, "👋 Welcome! Link open karo.")

@bot.message_handler(commands=["short"])
def short_cmd(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Admin Only")
        return

    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Use:\n<code>/short https://example.com/video</code>")
        return

    url = parts[1].strip()
    if not url.startswith("http"):
        bot.send_message(message.chat.id, "❌ Valid URL do.")
        return

    fid = uuid.uuid4().hex[:8]
    links_col.insert_one({"file_id": fid, "url": url, "created_at": datetime.now()})

    link = f"https://t.me/{BOT_USERNAME}?start=vid_{fid}"
    bot.send_message(message.chat.id, f"✅ Link Created:\n\n<code>{link}</code>")

@bot.callback_query_handler(func=lambda c: c.data.startswith("oldpay|"))
def old_pay(call):
    _, fid, mins = call.data.split("|")
    plan = PLANS[mins]

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ I Have Paid", callback_data=f"oldpaid|{fid}|{mins}"))
    markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))

    bot.send_photo(
        call.message.chat.id,
        qr_url(plan["price"]),
        caption=f"💳 <b>Pay ₹{plan['price']}</b>\nPlan: {plan['name']}\nUPI: <code>{UPI_ID}</code>",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("oldpaid|"))
def old_paid(call):
    _, fid, mins = call.data.split("|")
    user = call.from_user
    plan = PLANS[mins]

    req = requests_col.insert_one({
        "type": "old",
        "user_id": user.id,
        "file_id": fid,
        "mins": int(mins),
        "price": int(plan["price"]),
        "status": "pending",
        "created_at": datetime.now()
    })

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"approveold|{req.inserted_id}"))
    markup.add(InlineKeyboardButton("❌ Reject", callback_data=f"reject|{req.inserted_id}"))

    bot.send_message(
        ADMIN_ID,
        f"🔔 Payment Request\nUser ID: <code>{user.id}</code>\nFile: <code>{fid}</code>\nPrice: ₹{plan['price']}",
        reply_markup=markup
    )
    bot.send_message(call.message.chat.id, "✅ Request admin ko bhej di gayi hai.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("approveold|"))
def approve_old(call):
    if not is_admin(call.from_user.id): return

    req_id = call.data.split("|")[1]
    req = requests_col.find_one({"_id": ObjectId(req_id)})
    if not req: return

    expiry = add_prime(req["user_id"], req["mins"])
    data = links_col.find_one({"file_id": req["file_id"]})

    requests_col.update_one({"_id": ObjectId(req_id)}, {"$set": {"status": "approved"}})

    bot.send_message(int(req["user_id"]), f"✅ Prime Approved\nValid till: <code>{fmt(expiry)}</code>\n\n🍿 Link:\n{data['url'] if data else 'Not found'}")
    bot.edit_message_text("✅ Approved", call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("newpay|"))
def new_pay(call):
    _, ch_id, mins = call.data.split("|")
    ch = channels_col.find_one({"channel_id": int(ch_id)})
    if not ch:
        bot.answer_callback_query(call.id, "Channel not found")
        return

    price = ch["plans"][mins]

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ I Have Paid", callback_data=f"newpaid|{ch_id}|{mins}"))
    markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))

    bot.send_photo(
        call.message.chat.id,
        qr_url(price),
        caption=f"💳 <b>Pay ₹{price}</b>\nChannel: <b>{ch.get('name')}</b>\nUPI: <code>{UPI_ID}</code>",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("newpaid|"))
def new_paid(call):
    _, ch_id, mins = call.data.split("|")
    user = call.from_user
    ch = channels_col.find_one({"channel_id": int(ch_id)})
    price = ch["plans"][mins]

    req = requests_col.insert_one({
        "type": "channel",
        "user_id": user.id,
        "channel_id": int(ch_id),
        "channel_name": ch.get("name", "Private Channel"),
        "mins": int(mins),
        "price": int(price),
        "status": "pending",
        "created_at": datetime.now()
    })

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"approvenew|{req.inserted_id}"))
    markup.add(InlineKeyboardButton("❌ Reject", callback_data=f"reject|{req.inserted_id}"))

    bot.send_message(ADMIN_ID, f"🔔 Channel Payment\nUser: <code>{user.id}</code>\nChannel: {ch.get('name')}\nPrice: ₹{price}", reply_markup=markup)
    bot.send_message(call.message.chat.id, "✅ Request admin ko bhej di gayi hai.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("approvenew|"))
def approve_new(call):
    if not is_admin(call.from_user.id): return

    req_id = call.data.split("|")[1]
    req = requests_col.find_one({"_id": ObjectId(req_id)})
    if not req: return

    expiry = now_ts() + int(req["mins"]) * 60

    try:
        invite = bot.create_chat_invite_link(int(req["channel_id"]), member_limit=1, expire_date=expiry)
        users_col.update_one(
            {"user_id": int(req["user_id"]), "channel_id": int(req["channel_id"])},
            {"$set": {"expiry": expiry, "active": True, "channel_id": int(req["channel_id"])}},
            upsert=True
        )
        requests_col.update_one({"_id": ObjectId(req_id)}, {"$set": {"status": "approved"}})

        bot.send_message(int(req["user_id"]), f"🥳 Approved\nValid till: <code>{fmt(expiry)}</code>\n\nJoin Link:\n{invite.invite_link}")
        bot.edit_message_text("✅ Approved", call.message.chat.id, call.message.message_id)

    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Error:\n<code>{e}</code>")

@bot.callback_query_handler(func=lambda c: c.data.startswith("reject|"))
def reject(call):
    if not is_admin(call.from_user.id): return

    req_id = call.data.split("|")[1]
    req = requests_col.find_one({"_id": ObjectId(req_id)})

    if req:
        requests_col.update_one({"_id": ObjectId(req_id)}, {"$set": {"status": "rejected"}})
        try: bot.send_message(int(req["user_id"]), "❌ Payment rejected.")
        except: pass

    bot.edit_message_text("❌ Rejected", call.message.chat.id, call.message.message_id)

@bot.message_handler(commands=["approve"])
def approve_cmd(message):
    if not is_admin(message.from_user.id): return

    p = message.text.split()
    if len(p) < 3:
        bot.send_message(message.chat.id, "Use:\n<code>/approve USER_ID DAYS</code>")
        return

    expiry = add_prime(int(p[1]), int(p[2]) * 1440)
    bot.send_message(message.chat.id, f"✅ Approved till <code>{fmt(expiry)}</code>")

@bot.message_handler(commands=["deactivate"])
def deactivate(message):
    if not is_admin(message.from_user.id): return

    p = message.text.split()
    if len(p) < 2:
        bot.send_message(message.chat.id, "Use:\n<code>/deactivate USER_ID</code>")
        return

    users_col.update_many({"user_id": int(p[1])}, {"$set": {"expiry": 0, "active": False}})
    bot.send_message(message.chat.id, "❌ Deactivated")

@bot.message_handler(commands=["stats"])
def stats(message):
    if not is_admin(message.from_user.id): return

    bot.send_message(
        message.chat.id,
        f"📊 Stats\nUsers: <code>{users_col.count_documents({})}</code>\nLinks: <code>{links_col.count_documents({})}</code>\nPending: <code>{requests_col.count_documents({'status':'pending'})}</code>"
    )

broadcast_mode = set()

@bot.message_handler(commands=["broadcast"])
def broadcast(message):
    if not is_admin(message.from_user.id): return
    broadcast_mode.add(message.from_user.id)
    bot.send_message(message.chat.id, "📢 Ab message bhejo.")

@bot.message_handler(func=lambda m: m.from_user.id in broadcast_mode)
def send_broadcast(message):
    sent = 0
    for u in users_col.find({}):
        try:
            bot.copy_message(int(u["user_id"]), message.chat.id, message.message_id)
            sent += 1
        except:
            pass
    broadcast_mode.discard(message.from_user.id)
    bot.send_message(message.chat.id, f"✅ Sent: <code>{sent}</code>")

def kick_loop():
    while True:
        for u in users_col.find({"channel_id": {"$exists": True}, "expiry": {"$lte": now_ts()}, "active": True}):
            try:
                bot.ban_chat_member(int(u["channel_id"]), int(u["user_id"]))
                time.sleep(1)
                bot.unban_chat_member(int(u["channel_id"]), int(u["user_id"]))
                users_col.update_one({"_id": u["_id"]}, {"$set": {"active": False}})
            except Exception as e:
                print("kick error", e)
        time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=web_run, daemon=True).start()
    threading.Thread(target=kick_loop, daemon=True).start()

    bot.set_my_commands([
        BotCommand("short", "Create Link"),
        BotCommand("stats", "Check Users"),
        BotCommand("broadcast", "Message All"),
        BotCommand("approve", "ID Days"),
        BotCommand("deactivate", "ID"),
    ])

    bot.remove_webhook()
    time.sleep(1)
    print("Bot running:", BOT_USERNAME)
    bot.infinity_polling(timeout=20, long_polling_timeout=10, skip_pending=True)
