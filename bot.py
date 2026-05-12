import os, telebot, urllib.parse, uuid, datetime, re, threading, random, time, json
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from datetime import datetime, timedelta
from flask import Flask, request
import razorpay

# ================= CONFIGURATION =================
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
ADMIN_ID = int(os.getenv('ADMIN_ID'))
WEBHOOK_URL = os.getenv('WEBHOOK_URL')

# Razorpay Keys
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET')

bot = telebot.TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URI)
db = client['sub_management']
users_col = db['users']
links_col = db['short_links']

razor_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
app = Flask(__name__)

# ================= AUTO-APPROVAL WEBHOOK =================
@app.route('/razorpay_webhook', methods=['POST'])
def razorpay_webhook():
    data = request.get_data().decode('utf-8')
    signature = request.headers.get('X-Razorpay-Signature')
    
    try:
        # Verify Payment
        razor_client.utility.verify_webhook_signature(data, signature, RAZORPAY_KEY_SECRET)
        event_data = json.loads(data)
        
        if event_data['event'] == 'payment_link.paid':
            payment = event_data['payload']['payment_link']['entity']
            uid = int(payment['notes']['user_id'])
            mins = int(payment['notes']['mins'])
            fid = payment['notes']['fid']
            
            # Set Expiry & Notify
            expiry = int((datetime.now() + timedelta(minutes=mins)).timestamp())
            users_col.update_one({"user_id": uid}, {"$set": {"expiry": expiry}}, upsert=True)
            
            l_data = links_col.find_one({"file_id": fid})
            msg = f"✅ **Payment Successful!**\n\n🎁 Your Link: {l_data['url']}" if l_data else "✅ Payment Successful! Prime Activated."
            bot.send_message(uid, msg)
            bot.send_message(ADMIN_ID, f"💰 **Auto-Paid:** User `{uid}` bought `{mins}` mins plan.")
            
        return "OK", 200
    except:
        return "Unauthorized", 400

@app.route('/')
def home(): return "🚀 Bot is Live!"

@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "OK", 200
    return "Forbidden", 403

# ================= HELPERS =================
def is_prime(uid):
    user = users_col.find_one({"user_id": uid})
    return user and user.get('expiry', 0) > datetime.now().timestamp()

def get_expiry_date(ts):
    return datetime.fromtimestamp(ts).strftime('%d %b %Y, %I:%M %p')

# ================= ADMIN COMMANDS =================
@bot.message_handler(commands=['stats'], func=lambda m: m.from_user.id == ADMIN_ID)
def stats(message):
    u_count = users_col.count_documents({})
    p_count = users_col.count_documents({"expiry": {"$gt": datetime.now().timestamp()}})
    bot.reply_to(message, f"📊 **Stats:**\nTotal Users: {u_count}\nPrime: {p_count}")

@bot.message_handler(commands=['short'], func=lambda m: m.from_user.id == ADMIN_ID)
def short(message):
    msg = bot.reply_to(message, "🔗 Send link to shorten:")
    bot.register_next_step_handler(msg, lambda m: save_link(m))

def save_link(m):
    fid = str(uuid.uuid4())[:8].lower()
    links_col.insert_one({"file_id": fid, "url": m.text})
    bot.send_message(ADMIN_ID, f"✅ Link: `https://t.me/{bot.get_me().username}?start=vid_{fid}`")

@bot.message_handler(commands=['broadcast'], func=lambda m: m.from_user.id == ADMIN_ID)
def broadcast(message):
    msg = bot.send_message(ADMIN_ID, "📢 Send message to broadcast:")
    bot.register_next_step_handler(msg, lambda m: [bot.copy_message(u['user_id'], ADMIN_ID, m.message_id) for u in users_col.find({})])

# ================= USER LOGIC =================
@bot.message_handler(commands=['start'])
def start(message):
    uid = message.from_user.id
    users_col.update_one({"user_id": uid}, {"$setOnInsert": {"joined": datetime.now()}}, upsert=True)
    
    match = re.search(r'vid_([a-zA-Z0-9]+)', message.text)
    if match:
        fid = match.group(1)
        if is_prime(uid):
            l = links_col.find_one({"file_id": fid})
            bot.send_message(uid, f"🍿 **Content:** {l['url']}") if l else bot.send_message(uid, "❌ Expired")
        else:
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("💳 2 Days - ₹50", callback_data=f"pay_{fid}_2880_50"))
            markup.row(InlineKeyboardButton("💳 7 Days - ₹100", callback_data=f"pay_{fid}_10080_100"))
            markup.row(InlineKeyboardButton("💳 1 Month - ₹250", callback_data=f"pay_{fid}_43200_250"))
            markup.row(InlineKeyboardButton("💳 3 Months - ₹650", callback_data=f"pay_{fid}_129600_650"))
            bot.send_message(uid, "🔒 **Membership Required!**", reply_markup=markup)
    else:
        status = "👑 Prime" if is_prime(uid) else "🆓 Free"
        bot.send_message(uid, f"👋 Welcome!\nStatus: {status}")

@bot.callback_query_handler(func=lambda c: c.data.startswith('pay_'))
def pay(call):
    _, fid, mins, price = call.data.split('_')
    try:
        link = razor_client.payment_link.create({
            "amount": int(price) * 100, "currency": "INR",
            "description": f"Prime {mins}m",
            "notes": {"user_id": str(call.from_user.id), "mins": mins, "fid": fid},
            "callback_url": f"https://t.me/{bot.get_me().username}", "callback_method": "get"
        })
        m = InlineKeyboardMarkup().add(InlineKeyboardButton("💳 Pay Online", url=link['short_url']))
        bot.edit_message_text("💰 Payment karein:", call.message.chat.id, call.message.message_id, reply_markup=m)
    except: bot.answer_callback_query(call.id, "⚠️ Gateway Error")

if __name__ == '__main__':
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
        
