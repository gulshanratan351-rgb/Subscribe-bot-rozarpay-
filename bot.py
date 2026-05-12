import os
import time
import uuid
import urllib.parse
from datetime import datetime
from threading import Thread

import telebot
from flask import Flask
from pymongo import MongoClient
from apscheduler.schedulers.background import BackgroundScheduler
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand


# ================= ENV =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
UPI_ID = os.getenv("UPI_ID", "").strip()
CONTACT_USERNAME = os.getenv("CONTACT_USERNAME", "").replace("@", "").strip()

if not BOT_TOKEN:
    raise Exception("BOT_TOKEN missing")
if not MONGO_URI:
    raise Exception("MONGO_URI missing")
if not ADMIN_ID:
    raise Exception("ADMIN_ID missing")
if not UPI_ID:
    raise Exception("UPI_ID missing")
if not CONTACT_USERNAME:
    raise Exception("CONTACT_USERNAME missing")


# ================= INIT =================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
client = MongoClient(MONGO_URI)
db = client["sub_management"]

users_col = db["users"]
links_col = db["short_links"]
channels_col = db["channels"]
requests_col = db["payment_requests"]

app = Flask(__name__)
BOT_USERNAME = bot.get_me().username


# ================= PLANS FOR OLD LINKS =================
PLANS = {
    "2880": {"name": "2 Days", "price": 50},
    "43200": {"name": "1 Month", "price": 200},
    "129600": {"name": "3 Months", "price": 650},
}


# ================= WEB =================
@app.route("/")
def home():
    return "Bot Running ✅", 200


@app.route("/health")
def health():
    return "OK ✅", 200


def run_web():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


def keep_alive():
    Thread(target=run_web, daemon=True).start()


# ================= HELPERS =================
def now_ts():
    return int(datetime.now().timestamp())


def fmt(ts):
    return datetime.fromtimestamp(int(ts)).strftime("%d-%m-%Y %I:%M %p")


def is_admin(uid):
    return int(uid) == ADMIN_ID


def save_user(message):
    users_col.update_one(
        {"user_id": message.from_user.id},
        {
            "$set": {
                "user_id": message.from_user.id,
                "username": message.from_user.username,
                "first_name": message.from_user.first_name,
                "last_seen": datetime.now(),
            },
            "$setOnInsert": {
                "joined": datetime.now(),
                "expiry": 0,
                "active": False,
            },
        },
        upsert=True,
    )


def is_prime(uid):
    user = users_col.find_one({"user_id": int(uid)})
    return bool(user and int(user.get("expiry", 0)) > now_ts())


def add_prime(uid, mins):
    user = users_col.find_one({"user_id": int(uid)}) or {}
    old_expiry = int(user.get("expiry", 0))
    base = max(old_expiry, now_ts())
    expiry = base + int(mins) * 60

    users_col.update_one(
        {"user_id": int(uid)},
        {
            "$set": {
                "user_id": int(uid),
                "expiry": expiry,
                "active": True,
                "updated_at": datetime.now(),
            }
        },
        upsert=True,
    )

    return expiry


def make_qr_url(price):
    upi = f"upi://pay?pa={UPI_ID}&pn=Prime Access&am={price}&cu=INR"
    return "https://api.qrserver.com/v1/create-qr-code/?size=350x350&data=" + urllib.parse.quote(upi)


# ================= COMMAND MENU ONLY 5 =================
def set_bot_commands():
    bot.set_my_commands([
        BotCommand("short", "Create Link"),
        BotCommand("stats", "Check Users"),
        BotCommand("broadcast", "Message All"),
        BotCommand("approve", "ID Days"),
        BotCommand("deactivate", "ID"),
    ])


# ================= START: OLD + NEW LINK OPEN =================
@bot.message_handler(commands=["start"])
def start_cmd(message):
    save_user(message)
    uid = message.from_user.id
    parts = message.text.split()

    if len(parts) > 1:
        start_value = parts[1]

        # OLD LINK: ?start=vid_xxxxx
        if start_value.startswith("vid_"):
            fid = start_value.replace("vid_", "")
            link_data = links_col.find_one({"file_id": fid})

            if not link_data:
                bot.send_message(message.chat.id, "❌ Link not found.")
                return

            if is_prime(uid):
                bot.send_message(
                    message.chat.id,
                    f"🍿 <b>Content Unlocked:</b>\n\n{link_data.get('url')}",
                    disable_web_page_preview=True,
                )
                return

            markup = InlineKeyboardMarkup()
            for mins, plan in PLANS.items():
                markup.add(
                    InlineKeyboardButton(
                        f"💳 {plan['name']} - ₹{plan['price']}",
                        callback_data=f"oldpay|{fid}|{mins}",
                    )
                )

            markup.add(
                InlineKeyboardButton(
                    "📞 Contact Admin",
                    url=f"https://t.me/{CONTACT_USERNAME}",
                )
            )

            bot.send_message(
                message.chat.id,
                "🔒 <b>Prime Required</b>\n\nPlan select karo:",
                reply_markup=markup,
            )
            return

        # NEW LINK: ?start=-100xxxxxxxx
        try:
            ch_id = int(start_value)
            ch = channels_col.find_one({"channel_id": ch_id})

            if not ch:
                bot.send_message(message.chat.id, "❌ Channel data not found.")
                return

            active = users_col.find_one({
                "user_id": uid,
                "channel_id": ch_id,
                "expiry": {"$gt": now_ts()},
                "active": True,
            })

            if active:
                bot.send_message(
                    message.chat.id,
                    f"✅ Subscription active hai.\n\nValid till: <code>{fmt(active['expiry'])}</code>",
                )
                return

            markup = InlineKeyboardMarkup()

            for mins, price in ch.get("plans", {}).items():
                markup.add(
                    InlineKeyboardButton(
                        f"💳 {mins} Min - ₹{price}",
                        callback_data=f"newpay|{ch_id}|{mins}",
                    )
                )

            markup.add(
                InlineKeyboardButton(
                    "📞 Contact Admin",
                    url=f"https://t.me/{CONTACT_USERNAME}",
                )
            )

            bot.send_message(
                message.chat.id,
                f"👋 Welcome\n\nChannel: <b>{ch.get('name', 'Private Channel')}</b>\n\nPlan select karo:",
                reply_markup=markup,
            )
            return

        except Exception:
            bot.send_message(message.chat.id, "❌ Invalid link.")
            return

    if is_admin(uid):
        bot.send_message(
            message.chat.id,
            "✅ <b>Admin Panel Active</b>\n\n"
            "/short URL\n"
            "/stats\n"
            "/broadcast\n"
            "/approve USER_ID DAYS\n"
            "/deactivate USER_ID",
        )
    else:
        bot.send_message(message.chat.id, "👋 Welcome! Link open karo.")


# ================= SHORT OLD LINK CREATE =================
@bot.message_handler(commands=["short"])
def short_cmd(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Admin Only")
        return

    parts = message.text.split(" ", 1)

    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "Use:\n<code>/short https://example.com/video</code>",
        )
        return

    url = parts[1].strip()

    if not url.startswith("http"):
        bot.send_message(message.chat.id, "❌ Valid URL do.")
        return

    fid = uuid.uuid4().hex[:8]

    links_col.insert_one({
        "file_id": fid,
        "url": url,
        "created_at": datetime.now(),
        "created_by": message.from_user.id,
    })

    start_link = f"https://t.me/{BOT_USERNAME}?start=vid_{fid}"

    bot.send_message(
        message.chat.id,
        f"✅ <b>Prime Lock Link Created</b>\n\n<code>{start_link}</code>",
        disable_web_page_preview=True,
    )


# ================= OLD LINK PAYMENT =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("oldpay|"))
def old_pay(call):
    _, fid, mins = call.data.split("|")
    plan = PLANS.get(mins)

    if not plan:
        bot.answer_callback_query(call.id, "Invalid plan")
        return

    price = plan["price"]
    qr_url = make_qr_url(price)

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "✅ I Have Paid",
            callback_data=f"oldpaid|{fid}|{mins}",
        )
    )
    markup.add(
        InlineKeyboardButton(
            "📞 Contact Admin",
            url=f"https://t.me/{CONTACT_USERNAME}",
        )
    )

    bot.send_photo(
        call.message.chat.id,
        qr_url,
        caption=(
            f"💳 <b>Prime Payment</b>\n\n"
            f"Plan: <b>{plan['name']}</b>\n"
            f"Price: <b>₹{price}</b>\n"
            f"UPI: <code>{UPI_ID}</code>\n\n"
            f"Payment ke baad <b>I Have Paid</b> dabao."
        ),
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("oldpaid|"))
def old_paid(call):
    _, fid, mins = call.data.split("|")
    plan = PLANS.get(mins)
    user = call.from_user

    req = requests_col.insert_one({
        "type": "old_vid",
        "user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "file_id": fid,
        "mins": int(mins),
        "price": int(plan["price"]),
        "status": "pending",
        "created_at": datetime.now(),
    })

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "✅ Approve",
            callback_data=f"approveold|{req.inserted_id}",
        )
    )
    markup.add(
        InlineKeyboardButton(
            "❌ Reject",
            callback_data=f"reject|{req.inserted_id}",
        )
    )

    bot.send_message(
        ADMIN_ID,
        f"🔔 <b>Old Link Payment Request</b>\n\n"
        f"User: <b>{user.first_name}</b>\n"
        f"ID: <code>{user.id}</code>\n"
        f"File ID: <code>{fid}</code>\n"
        f"Plan: <code>{mins}</code> mins\n"
        f"Price: ₹{plan['price']}",
        reply_markup=markup,
    )

    bot.send_message(call.message.chat.id, "✅ Request admin ko bhej di gayi hai.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("approveold|"))
def approve_old(call):
    if not is_admin(call.from_user.id):
        return

    from bson import ObjectId

    req_id = call.data.split("|")[1]
    req = requests_col.find_one({"_id": ObjectId(req_id)})

    if not req:
        bot.answer_callback_query(call.id, "Request not found")
        return

    expiry = add_prime(req["user_id"], req["mins"])
    link_data = links_col.find_one({"file_id": req["file_id"]})
    content = link_data.get("url") if link_data else "Link not found"

    requests_col.update_one(
        {"_id": ObjectId(req_id)},
        {"$set": {"status": "approved", "approved_at": datetime.now()}},
    )

    bot.send_message(
        int(req["user_id"]),
        f"✅ <b>Prime Approved</b>\n\n"
        f"Valid till: <code>{fmt(expiry)}</code>\n\n"
        f"🍿 Link:\n{content}",
        disable_web_page_preview=True,
    )

    bot.edit_message_text("✅ Approved", call.message.chat.id, call.message.message_id)


# ================= NEW CHANNEL PAYMENT =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("newpay|"))
def new_pay(call):
    _, ch_id, mins = call.data.split("|")
    ch_id = int(ch_id)

    ch = channels_col.find_one({"channel_id": ch_id})

    if not ch:
        bot.answer_callback_query(call.id, "Channel not found")
        return

    price = ch["plans"][mins]
    qr_url = make_qr_url(price)

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "✅ I Have Paid",
            callback_data=f"newpaid|{ch_id}|{mins}",
        )
    )
    markup.add(
        InlineKeyboardButton(
            "📞 Contact Admin",
            url=f"https://t.me/{CONTACT_USERNAME}",
        )
    )

    bot.send_photo(
        call.message.chat.id,
        qr_url,
        caption=(
            f"💳 <b>Payment Details</b>\n\n"
            f"Channel: <b>{ch.get('name', 'Private Channel')}</b>\n"
            f"Plan: <code>{mins}</code> min\n"
            f"Price: <b>₹{price}</b>\n"
            f"UPI: <code>{UPI_ID}</code>\n\n"
            f"Payment ke baad <b>I Have Paid</b> dabao."
        ),
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("newpaid|"))
def new_paid(call):
    _, ch_id, mins = call.data.split("|")
    ch_id = int(ch_id)
    ch = channels_col.find_one({"channel_id": ch_id})
    user = call.from_user

    if not ch:
        bot.answer_callback_query(call.id, "Channel not found")
        return

    price = ch["plans"][mins]

    req = requests_col.insert_one({
        "type": "channel",
        "user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "channel_id": ch_id,
        "channel_name": ch.get("name", "Private Channel"),
        "mins": int(mins),
        "price": int(price),
        "status": "pending",
        "created_at": datetime.now(),
    })

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "✅ Approve",
            callback_data=f"approvenew|{req.inserted_id}",
        )
    )
    markup.add(
        InlineKeyboardButton(
            "❌ Reject",
            callback_data=f"reject|{req.inserted_id}",
        )
    )

    bot.send_message(
        ADMIN_ID,
        f"🔔 <b>Channel Payment Request</b>\n\n"
        f"User: <b>{user.first_name}</b>\n"
        f"ID: <code>{user.id}</code>\n"
        f"Channel: <b>{ch.get('name', 'Private Channel')}</b>\n"
        f"Plan: {mins} mins\n"
        f"Price: ₹{price}",
        reply_markup=markup,
    )

    bot.send_message(call.message.chat.id, "✅ Request admin ko bhej di gayi hai.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("approvenew|"))
def approve_new(call):
    if not is_admin(call.from_user.id):
        return

    from bson import ObjectId

    req_id = call.data.split("|")[1]
    req = requests_col.find_one({"_id": ObjectId(req_id)})

    if not req:
        bot.answer_callback_query(call.id, "Request not found")
        return

    user_id = int(req["user_id"])
    ch_id = int(req["channel_id"])
    mins = int(req["mins"])
    expiry = now_ts() + mins * 60

    try:
        invite = bot.create_chat_invite_link(
            ch_id,
            member_limit=1,
            expire_date=expiry,
            creates_join_request=False,
        )

        users_col.update_one(
            {"user_id": user_id, "channel_id": ch_id},
            {
                "$set": {
                    "user_id": user_id,
                    "channel_id": ch_id,
                    "channel_name": req["channel_name"],
                    "expiry": expiry,
                    "active": True,
                    "updated_at": datetime.now(),
                }
            },
            upsert=True,
        )

        requests_col.update_one(
            {"_id": ObjectId(req_id)},
            {"$set": {"status": "approved", "approved_at": datetime.now()}},
        )

        bot.send_message(
            user_id,
            f"🥳 <b>Payment Approved!</b>\n\n"
            f"Channel: <b>{req['channel_name']}</b>\n"
            f"Valid till: <code>{fmt(expiry)}</code>\n\n"
            f"Join Link:\n{invite.invite_link}",
        )

        bot.edit_message_text("✅ Approved", call.message.chat.id, call.message.message_id)

    except Exception as e:
        bot.send_message(
            ADMIN_ID,
            f"❌ Approve Error:\n<code>{e}</code>\n\nBot ko channel me admin banao with Invite Users + Ban Users permission.",
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("reject|"))
def reject_req(call):
    if not is_admin(call.from_user.id):
        return

    from bson import ObjectId

    req_id = call.data.split("|")[1]
    req = requests_col.find_one({"_id": ObjectId(req_id)})

    if req:
        requests_col.update_one(
            {"_id": ObjectId(req_id)},
            {"$set": {"status": "rejected", "rejected_at": datetime.now()}},
        )

        try:
            bot.send_message(
                int(req["user_id"]),
                "❌ Payment request rejected. Admin se contact karo.",
            )
        except Exception:
            pass

    bot.edit_message_text("❌ Rejected", call.message.chat.id, call.message.message_id)


# ================= ADMIN COMMANDS =================
@bot.message_handler(commands=["approve"])
def manual_approve(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Admin Only")
        return

    parts = message.text.split()

    if len(parts) < 3:
        bot.send_message(message.chat.id, "Use:\n<code>/approve USER_ID DAYS</code>")
        return

    uid = int(parts[1])
    days = int(parts[2])
    expiry = add_prime(uid, days * 1440)

    try:
        bot.send_message(uid, f"✅ Prime Approved\nValid till: <code>{fmt(expiry)}</code>")
    except Exception:
        pass

    bot.send_message(message.chat.id, "✅ Approved")


@bot.message_handler(commands=["deactivate"])
def deactivate(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Admin Only")
        return

    parts = message.text.split()

    if len(parts) < 2:
        bot.send_message(message.chat.id, "Use:\n<code>/deactivate USER_ID</code>")
        return

    uid = int(parts[1])

    users_col.update_many(
        {"user_id": uid},
        {"$set": {"expiry": 0, "active": False}},
    )

    bot.send_message(message.chat.id, "❌ Deactivated")


@bot.message_handler(commands=["stats"])
def stats(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Admin Only")
        return

    total_users = users_col.count_documents({})
    total_links = links_col.count_documents({})
    total_channels = channels_col.count_documents({})
    pending = requests_col.count_documents({"status": "pending"})
    active = users_col.count_documents({"active": True, "expiry": {"$gt": now_ts()}})

    bot.send_message(
        message.chat.id,
        f"📊 <b>Stats</b>\n\n"
        f"Users: <code>{total_users}</code>\n"
        f"Active: <code>{active}</code>\n"
        f"Links: <code>{total_links}</code>\n"
        f"Channels: <code>{total_channels}</code>\n"
        f"Pending: <code>{pending}</code>",
    )


broadcast_mode = {}


@bot.message_handler(commands=["broadcast"])
def broadcast(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "❌ Admin Only")
        return

    broadcast_mode[message.from_user.id] = True
    bot.send_message(message.chat.id, "📢 Ab message bhejo, sab users ko jayega.")


@bot.message_handler(func=lambda m: m.from_user.id in broadcast_mode)
def send_broadcast(message):
    if not is_admin(message.from_user.id):
        return

    sent = 0
    failed = 0

    for u in users_col.find({}):
        try:
            bot.copy_message(
                int(u["user_id"]),
                message.chat.id,
                message.message_id,
            )
            sent += 1
        except Exception:
            failed += 1

    del broadcast_mode[message.from_user.id]

    bot.send_message(
        message.chat.id,
        f"✅ Done\nSent: <code>{sent}</code>\nFailed: <code>{failed}</code>",
    )


# =====
