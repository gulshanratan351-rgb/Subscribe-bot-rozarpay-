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
WEBHOOK_URL = os.getenv('WEBHOOK_URL') # Example: https://your-app.onrender.com

# Razorpay Keys (Render Environment Variables mein add karein)
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET')

bot = telebot.TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URI)
db = client['sub_management']
users_col = db['users']
links_col = db['short_links']

# Razorpay Client Initialization
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# Updated Plans Mapping (As per your request)
PLANS = {
    "2880": 50,     # 2 Days - ₹50
    "10080": 100,   # 7 Days - ₹100
    "43200": 250,   # 1 Month - ₹250
    "129600": 650   # 3 Months - ₹650
}

app = Flask(__name__)

# ================= RAZORPAY WEBHOOK (AUTO-APPROVAL) =================
@app.route('/razorpay_webhook', methods=['POST'])
def razorpay_webhook():
    webhook_signature = request.headers.get('X-Razorpay-Signature')
    webhook_secret = RAZORPAY_KEY_SECRET # Use same secret for validation
    data = request.get_data().decode('utf-8')

    try:
        # Verify the webhook signature
        razorpay_client.utility.verify_webhook_signature(data, webhook_signature, webhook_secret)
        
        event_json = json.loads(data)
        if event_json['event'] == 'payment_link.paid':
            payload = event_json['payload']['payment_link']['entity']
            
            # Extract data from notes
            uid = int(payload['notes']['user_id'])
            mins = int(payload['notes']['mins'])
            fid = payload['notes']['fid']
            
            # Calculate Expiry
            expiry = int((datetime.now() + timedelta(minutes=mins)).timestamp())
            users_col.update_one({"user_id": uid}, {"$set": {"expiry": expiry}}, upsert=True)
            
            # Send Notification & Link
            l_data = links_col.find_one({"file_id": fid})
            msg = f"✅ **Payment Successful!**\n\n🎁 Your Content Link: {l_data['url']}" if l_data else "✅ Payment Successful! Prime Activated."
            bot.send_message(uid, msg)
            bot.send_message(ADMIN_ID, f"💰 **Auto-Payment Alert**\nUser {uid} paid for {mins} mins.")
            
        return "OK", 200
    except Exception as e:
        print(f"Webhook Error: {str(e)}")
        return "Error", 400

@app.route('/')
def home():
    return "🚀 Razorpay Bot is Online!"

@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "OK", 200
    return "Forbidden", 403

# ================= HELPER FUNCTIONS =================
def is_prime(uid):
    user = users_col.find_one({"user_id": uid})
    if user and user.get('expiry', 0) > datetime.now().timestamp():
        return True
    return False

def get_expiry_date(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%d %b %Y, %I:%M %p')

# ================= BOT COMMANDS =================

@bot.message_handler(commands=['start'])
def handle_start(message):
    uid = message.from_user.id
    users_col.update_one({"user_id": uid}, {"$setOnInsert": {"joined": datetime.now()}}, upsert=True)
    
    match = re.search(r'vid_([a-zA-Z0-9]+)', message.text)
    if match:
        fid = match.group(1)
        if is_prime(uid):
            link_data = links_col.find_one({"file_id": fid})
            if link_data:
                bot.send_message(uid, f"🍿 **Your Content:**\n{link_data['url']}")
            else:
                bot.send_message(uid, "❌ Link expired.")
        else:
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("💳 2 Days - ₹50", callback_data=f"rzp_{fid}_2880_50"))
            markup.row(InlineKeyboardButton("💳 7 Days - ₹100", callback_data=f"rzp_{fid}_10080_100"))
            markup.row(InlineKeyboardButton("💳 1 Month - ₹250", callback_data=f"rzp_{fid}_43200_250"))
            markup.row(InlineKeyboardButton("💳 3 Months - ₹650", callback_data=f"rzp_{fid}_129600_650"))
            bot.send_message(uid, "🔒 **Membership Required!**\nSelect a plan to pay via Razorpay:", reply_markup=markup)
    else:
        bot.send_message(uid, "👋 Welcome! Status: " + ("👑 Prime" if is_prime(uid) else "🆓 Free"))

@bot.callback_query_handler(func=lambda call: call.data.startswith('rzp_'))
def create_payment(call):
    bot.answer_callback_query(call.id, "Generating Payment Link...")
    try:
        _, fid, mins, price = call.data.split('_')
        uid = call.from_user.id

        # Create Razorpay Payment Link
        payment_link = razorpay_client.payment_link.create({
            "amount": int(price) * 100, # Amount in paise
            "currency": "INR",
            "accept_partial": False,
            "description": f"Subscription for {mins} minutes",
            "customer": {"name": str(uid)},
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "notes": {
                "user_id": str(uid),
                "mins": mins,
                "fid": fid
            },
            "callback_url": f"https://t.me/{bot.get_me().username}",
            "callback_method": "get"
        })

        pay_url = payment_link['short_url']
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💰 Pay Online Now", url=pay_url))
        
        bot.send_message(uid, f"💎 **Plan:** ₹{price}\n\nNiche button par click karke payment karein. Payment hote hi access mil jayega.", reply_markup=markup)
        
    except Exception as e:
        bot.send_message(call.message.chat.id, f"⚠️ Error in Payment Gateway: {str(e)}")

# (Other Admin commands like /short, /stats, /broadcast remain same as your previous code)
@bot.message_handler(commands=['short'], func=lambda m: m.from_user.id == ADMIN_ID)
def short_link(message):
    msg = bot.reply_to(message, "🔗 Paste original link:")
    bot.register_next_step_handler(msg, save_link)

def save_link(message):
    file_id = str(uuid.uuid4())[:8].lower()
    links_col.insert_one({"file_id": file_id, "url": message.text})
    bot.send_message(ADMIN_ID, f"✅ Link Created: `https://t.me/{bot.get_me().username}?start=vid_{file_id}`")

# ================= RUNNER =================
if __name__ == '__main__':
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
            
