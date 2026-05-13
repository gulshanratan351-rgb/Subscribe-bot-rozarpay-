import os, telebot, urllib.parse, uuid, datetime, re, threading, random, time, json
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from datetime import datetime, timedelta
from flask import Flask, request
import razorpay

BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
ADMIN_ID = int(os.getenv('ADMIN_ID'))
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET')

bot = telebot.TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URI)
db = client['sub_management']

users_col = db['users']
links_col = db['short_links']
orders_col = db['razorpay_orders']

razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

PLANS = {
    "2880": "50",      # 2 Days
    "10080": "99",     # 7 Days
    "43200": "249",    # 1 Month
    "129600": "649"    # 3 Months
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

@app.route('/razorpay-webhook', methods=['POST'])
def razorpay_webhook():
    try:
        data = json.loads(request.get_data().decode())

        if data.get('event') == 'payment_link.paid':
            payment_link = data['payload']['payment_link']['entity']
            order_id = payment_link['id']

            order = orders_col.find_one({"order_id": order_id})

            if order:
                expiry = int((datetime.now() + timedelta(minutes=int(order['mins']))).timestamp())

                users_col.update_one(
                    {"user_id": order['user_id']},
                    {"$set": {"expiry": expiry}},
                    upsert=True
                )

                link_data = links_col.find_one({"file_id": order['fid']})

                if link_data:
                    bot.send_message(
                        order['user_id'],
                        f"✅ Payment Successful!\n\n🍿 Your Content:\n{link_data['url']}",
                        disable_web_page_preview=True
                    )
                else:
                    bot.send_message(order['user_id'], "✅ Payment Successful! Access granted.")

                orders_col.delete_one({"order_id": order_id})
                bot.send_message(ADMIN_ID, f"✅ Auto-Approved: User {order['user_id']}")

    except Exception as e:
        print("Webhook Error:", e)

    return "OK", 200

def is_prime(uid):
    user = users_col.find_one({"user_id": uid})
    return user and user.get('expiry', 0) > datetime.now().timestamp()

def get_expiry_date(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%d %b %Y, %I:%M %p')

@bot.message_handler(commands=['stats'], func=lambda m: m.from_user.id == ADMIN_ID)
def stats_handler(message):
    total_users = users_col.count_documents({})
    active_prime = users_col.count_documents({"expiry": {"$gt": datetime.now().timestamp()}})
    total_links = links_col.count_documents({})

    text = (
        f"📊 Bot Statistics\n\n"
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
    count = 0

    for user in users_col.find({}):
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

@bot.message_handler(commands=['start'])
def handle_start(message):
    uid = message.from_user.id

    users_col.update_one(
        {"user_id": uid},
        {"$setOnInsert": {"joined": datetime.now()}},
        upsert=True
    )

    match = re.search(r'(?:vid_)?([a-zA-Z0-9]{8})', message.text)

    if match:
        fid = match.group(1)

        if is_prime(uid):
            link_data = links_col.find_one({"file_id": fid})

            if link_data:
                bot.send_message(
                    uid,
                    f"🍿 Your Content is Ready:\n\n{link_data['url']}",
                    disable_web_page_preview=True
                )
            else:
                bot.send_message(uid, "❌ Link expired or removed.")

        else:
            markup = InlineKeyboardMarkup(row_width=2)

            markup.add(
    InlineKeyboardButton("⚡ 2 Days ₹50", callback_data=f"pay_{fid}_2880_50"),
    InlineKeyboardButton("🔥 7 Days ₹99", callback_data=f"pay_{fid}_10080_99")
)

markup.add(
    InlineKeyboardButton("👑 1 Month ₹249", callback_data=f"pay_{fid}_43200_249"),
    InlineKeyboardButton("💎 3 Months ₹649", callback_data=f"pay_{fid}_129600_649")
)

            bot.send_message(
                uid,
                "🔒 Membership Required!\n\n"
                "✨ Upgrade to Prime and unlock premium content instantly.\n\n"
                "💳 Select your plan below:",
                reply_markup=markup
            )

    else:
        text = "👋 Welcome to the Movie Bot!\n\n"

        if is_prime(uid):
            u = users_col.find_one({"user_id": uid})
            text += f"👑 Status: Prime User\n📅 Expiry: {get_expiry_date(u['expiry'])}"
        else:
            text += "👑 Status: Free User\nJoin Prime to access premium links."

        bot.send_message(uid, text)

@bot.callback_query_handler(func=lambda call: call.data.startswith('pay_'))
def create_razorpay_link(call):
    bot.answer_callback_query(call.id)

    try:
        _, fid, mins, price = call.data.split('_')
        uid = call.from_user.id

        payment_link = razorpay_client.payment_link.create({
            "amount": int(price) * 100,
            "currency": "INR",
            "accept_partial": False,
            "description": f"Prime Membership ₹{price}",
            "customer": {
                "name": call.from_user.first_name or "Telegram User"
            },
            "notify": {
                "sms": False,
                "email": False
            },
            "reminder_enable": True,
            "notes": {
                "user_id": str(uid),
                "fid": fid,
                "mins": mins,
                "price": price
            }
        })

        orders_col.insert_one({
            "order_id": payment_link['id'],
            "user_id": uid,
            "fid": fid,
            "mins": mins,
            "price": price,
            "created_at": datetime.now()
        })

        pay_url = payment_link['short_url']

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💳 Pay Now", url=pay_url))
        markup.add(InlineKeyboardButton("✅ Check Payment", callback_data=f"check_{payment_link['id']}"))

        bot.send_message(
            call.message.chat.id,
            f"💰 Plan Amount: ₹{price}\n\n"
            f"Click below to pay:\n{pay_url}\n\n"
            f"Payment ke baad ✅ Check Payment dabao.",
            reply_markup=markup,
            disable_web_page_preview=True
        )

    except Exception as e:
        bot.send_message(call.message.chat.id, f"⚠️ Error: {str(e)}")
        bot.send_message(ADMIN_ID, f"Payment Error: {str(e)}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('check_'))
def check_payment(call):
    bot.answer_callback_query(call.id)

    order_id = call.data.replace('check_', '')
    order = orders_col.find_one({"order_id": order_id})

    if not order:
        bot.send_message(call.message.chat.id, "❌ Order not found.")
        return

    try:
        payment_link = razorpay_client.payment_link.fetch(order_id)

        if payment_link.get('status') == 'paid':
            expiry = int((datetime.now() + timedelta(minutes=int(order['mins']))).timestamp())

            users_col.update_one(
                {"user_id": order['user_id']},
                {"$set": {"expiry": expiry}},
                upsert=True
            )

            link_data = links_col.find_one({"file_id": order['fid']})

            if link_data:
                bot.send_message(
                    call.message.chat.id,
                    f"✅ Payment Successful!\n\n🍿 Your Content:\n{link_data['url']}",
                    disable_web_page_preview=True
                )
            else:
                bot.send_message(call.message.chat.id, "✅ Payment Successful! Access granted.")

            orders_col.delete_one({"order_id": order_id})
            bot.send_message(ADMIN_ID, f"✅ Auto-Approved: User {order['user_id']}")
            return

        bot.send_message(call.message.chat.id, "⏳ Payment pending.\nComplete payment first.")

    except Exception as e:
        bot.send_message(call.message.chat.id, f"⚠️ Error: {str(e)}")

if __name__ == '__main__':
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
