import os, telebot, uuid, re, time, hashlib, hmac, json
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import razorpay
import requests

# ================= CONFIG =================
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
links_col = db['links']
orders_col = db['orders']

razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# Plans
PLANS = {
    "2880": 50,
    "10080": 100,
    "43200": 250,
    "129600": 650
}

PLAN_NAMES = {
    "2880": "2 Days",
    "10080": "7 Days",
    "43200": "1 Month",
    "129600": "3 Months"
}

app = Flask(__name__)

# ================= WEBHOOKS =================
@app.route('/')
def home():
    return "Bot Running"

@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode())])
        return "OK", 200
    return "Forbidden", 403

@app.route('/razorpay-webhook', methods=['POST'])
def razorpay_webhook():
    try:
        data = json.loads(request.get_data().decode())
        
        if data.get('event') == 'payment.captured':
            payment = data['payload']['payment']['entity']
            order_id = payment['order_id']
            
            order = orders_col.find_one({"order_id": order_id})
            if order:
                expiry = int((datetime.now() + timedelta(minutes=int(order['mins']))).timestamp())
                users_col.update_one({"user_id": order['user_id']}, {"$set": {"expiry": expiry}}, upsert=True)
                
                link = links_col.find_one({"file_id": order['fid']})
                if link:
                    try:
                        bot.send_message(order['user_id'], f"✅ Payment Successful!\n\n🎬 Your Content:\n{link['url']}")
                    except:
                        pass
                
                orders_col.delete_one({"order_id": order_id})
                bot.send_message(ADMIN_ID, f"✅ Auto-Approved!\nUser: {order['user_id']}\nAmount: ₹{order['amount']}")
        
        return jsonify({"status": "ok"})
    except Exception as e:
        print(f"Webhook error: {str(e)}")
        return jsonify({"status": "error"}), 200

def is_prime(uid):
    user = users_col.find_one({"user_id": uid})
    return user and user.get('expiry', 0) > datetime.now().timestamp()

# ================= ADMIN =================
@bot.message_handler(commands=['stats'], func=lambda m: m.from_user.id == ADMIN_ID)
def stats(m):
    total = users_col.count_documents({})
    active = users_col.count_documents({"expiry": {"$gt": datetime.now().timestamp()}})
    links = links_col.count_documents({})
    bot.reply_to(m, f"📊 Bot Statistics\n\n👤 Total Users: {total}\n👑 Active Prime: {active}\n🔗 Total Links: {links}")

@bot.message_handler(commands=['approve'], func=lambda m: m.from_user.id == ADMIN_ID)
def approve(m):
    try:
        parts = m.text.split()
        if len(parts) < 3:
            bot.reply_to(m, "❌ Use: /approve user_id days")
            return
        uid = int(parts[1])
        days = int(parts[2])
        expiry = int((datetime.now() + timedelta(days=days)).timestamp())
        users_col.update_one({"user_id": uid}, {"$set": {"expiry": expiry}}, upsert=True)
        bot.send_message(uid, f"✅ Prime activated for {days} days by Admin!")
        bot.reply_to(m, f"✅ User {uid} approved for {days} days")
    except Exception as e:
        bot.reply_to(m, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['unapprove'], func=lambda m: m.from_user.id == ADMIN_ID)
def unapprove(m):
    try:
        uid = None
        if m.reply_to_message:
            uid = m.reply_to_message.from_user.id
        else:
            uid = int(m.text.split()[1])
        users_col.update_one({"user_id": uid}, {"$set": {"expiry": 0}})
        bot.reply_to(m, f"✅ User {uid} deactivated")
    except:
        bot.reply_to(m, "❌ Use: /unapprove user_id")

@bot.message_handler(commands=['broadcast'], func=lambda m: m.from_user.id == ADMIN_ID)
def broadcast(m):
    msg = bot.send_message(ADMIN_ID, "📢 Send message to broadcast:")
    bot.register_next_step_handler(msg, do_broadcast)

def do_broadcast(m):
    count = 0
    for user in users_col.find({}):
        try:
            bot.copy_message(user['user_id'], ADMIN_ID, m.message_id)
            count += 1
            time.sleep(0.05)
        except:
            pass
    bot.send_message(ADMIN_ID, f"✅ Broadcast sent to {count} users")

@bot.message_handler(commands=['short'], func=lambda m: m.from_user.id == ADMIN_ID)
def short(m):
    msg = bot.reply_to(m, "🔗 Send the link to shorten:")
    bot.register_next_step_handler(msg, save_link)

def save_link(m):
    fid = str(uuid.uuid4())[:8]
    links_col.insert_one({"file_id": fid, "url": m.text})
    link_url = f"https://t.me/{bot.get_me().username}?start={fid}"
    bot.send_message(ADMIN_ID, f"✅ Link Created!\n\n{link_url}")

# ================= USER =================
@bot.message_handler(commands=['start'])
def start(m):
    uid = m.from_user.id
    users_col.update_one({"user_id": uid}, {"$setOnInsert": {"joined": datetime.now()}}, upsert=True)
    
    text = m.text.split()
    
    if len(text) > 1:
        fid = text[1]
        
        if is_prime(uid):
            link = links_col.find_one({"file_id": fid})
            if link:
                bot.send_message(uid, f"🍿 Your Content:\n\n{link['url']}", disable_web_page_preview=True)
            else:
                bot.send_message(uid, "❌ Link not found")
        else:
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("💳 2 Days - ₹50", callback_data=f"pay_{fid}_2880"))
            markup.row(InlineKeyboardButton("💳 7 Days - ₹100", callback_data=f"pay_{fid}_10080"))
            markup.row(InlineKeyboardButton("💳 1 Month - ₹250", callback_data=f"pay_{fid}_43200"))
            markup.row(InlineKeyboardButton("💳 3 Months - ₹650", callback_data=f"pay_{fid}_129600"))
            
            bot.send_message(uid, "🔒 Membership Required!\n\nSelect your plan to unlock content:", reply_markup=markup)
    else:
        if is_prime(uid):
            user = users_col.find_one({"user_id": uid})
            expiry_date = datetime.fromtimestamp(user['expiry']).strftime('%d %b %Y, %I:%M %p')
            bot.send_message(uid, f"👋 Welcome Back!\n\n✅ Status: Prime Active\n📅 Expiry: {expiry_date}")
        else:
            bot.send_message(uid, "👋 Welcome!\n\n💎 Get Prime membership to access exclusive content.\n\nUse /start [link] to unlock content.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('pay_'))
def create_payment(call):
    bot.answer_callback_query(call.id)
    
    try:
        _, fid, mins = call.data.split('_')
        amount = PLANS[mins]
        
        # Create Razorpay Order
        order_data = {
            'amount': int(amount * 100),
            'currency': 'INR',
            'receipt': f'receipt_{call.from_user.id}_{int(time.time())}',
            'payment_capture': 1
        }
        
        order = razorpay_client.order.create(data=order_data)
        order_id = order['id']
        
        # Save to database
        orders_col.insert_one({
            "order_id": order_id,
            "user_id": call.from_user.id,
            "fid": fid,
            "mins": mins,
            "amount": amount,
            "created_at": datetime.now()
        })
        
        # FIXED: Working payment link format like https://rzp.io/rzp/B2r2UlWl
        payment_link = f"https://rzp.io/rzp/{order_id}"
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💳 Pay with Razorpay", url=payment_link))
        markup.add(InlineKeyboardButton("🔄 Check Payment Status", callback_data=f"check_{order_id}"))
        
        bot.send_message(
            call.message.chat.id,
            f"💰 Plan: {PLAN_NAMES[mins]}\n"
            f"💵 Amount: ₹{amount}\n\n"
            f"🔗 Click below to pay:\n{payment_link}\n\n"
            f"✅ After payment, click Check Payment Status",
            reply_markup=markup,
            disable_web_page_preview=True
        )
        
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Error creating payment!\n\n{str(e)}\n\nPlease try again.")
        bot.send_message(ADMIN_ID, f"Payment Error: {str(e)}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('check_'))
def check_payment(call):
    bot.answer_callback_query(call.id, "Checking payment status...")
    
    order_id = call.data.split('_')[1]
    order = orders_col.find_one({"order_id": order_id})
    
    if not order:
        bot.send_message(call.message.chat.id, "❌ Order not found! Please create a new order.")
        return
    
    try:
        # Fetch payment status
        payments = razorpay_client.order.payments(order_id)
        
        if payments and payments.get('items'):
            for payment in payments['items']:
                if payment['status'] == 'captured':
                    # Activate user
                    expiry = int((datetime.now() + timedelta(minutes=int(order['mins']))).timestamp())
                    users_col.update_one(
                        {"user_id": order['user_id']},
                        {"$set": {"expiry": expiry, "payment_id": payment['id']}},
                        upsert=True
                    )
                    
                    # Send content
                    link = links_col.find_one({"file_id": order['fid']})
                    if link:
                        bot.send_message(call.message.chat.id, f"✅ Payment Verified!\n\n🎬 Your Content:\n{link['url']}")
                    else:
                        bot.send_message(call.message.chat.id, f"✅ Payment Verified!\n\n{PLAN_NAMES[order['mins']]} subscription activated!")
                    
                    orders_col.delete_one({"order_id": order_id})
                    bot.send_message(ADMIN_ID, f"✅ Manual verification!\nUser: {order['user_id']}")
                    return
        
        bot.send_message(
            call.message.chat.id,
            f"⏳ Payment Pending\n\n"
            f"Order ID: {order_id[:12]}\n"
            f"Amount: ₹{order['amount']}\n\n"
            f"Please complete payment using:\nhttps://rzp.io/rzp/{order_id}"
        )
        
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Error checking payment:\n{str(e)}")

# ================= RUN =================
if __name__ == '__main__':
    print("🚀 Bot Starting...")
    bot.remove_webhook()
    time.sleep(2)
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    print(f"✅ Webhook set to: {WEBHOOK_URL}/{BOT_TOKEN}")
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
