import os, telebot, uuid, re, time, hashlib, hmac
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import razorpay

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

# Plans: minutes = price
PLANS = {
    "2880": 50,     # 2 days
    "10080": 100,   # 7 days
    "43200": 250,   # 1 month
    "129600": 650   # 3 months
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
    return "Bot is Running!"

@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode())])
        return "OK", 200
    return "Forbidden", 403

@app.route('/razorpay-webhook', methods=['POST'])
def razorpay_webhook():
    # Verify signature
    signature = request.headers.get('X-Razorpay-Signature')
    payload = request.get_data().decode()
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    
    if not hmac.compare_digest(expected, signature):
        return jsonify({"error": "Invalid"}), 403
    
    data = request.json
    if data.get('event') == 'payment.captured':
        payment = data['payload']['payment']['entity']
        order_id = payment['order_id']
        
        order = orders_col.find_one({"order_id": order_id})
        if order:
            # Activate subscription
            expiry = int((datetime.now() + timedelta(minutes=int(order['mins']))).timestamp())
            users_col.update_one({"user_id": order['user_id']}, {"$set": {"expiry": expiry}}, upsert=True)
            
            # Send content
            link = links_col.find_one({"file_id": order['fid']})
            if link:
                try:
                    bot.send_message(order['user_id'], f"✅ Payment Done!\n\n🎬 {link['url']}")
                except:
                    pass
            
            orders_col.delete_one({"order_id": order_id})
            bot.send_message(ADMIN_ID, f"✅ Auto approved: {order['user_id']}")
    
    return jsonify({"status": "ok"})

# ================= HELPERS =================
def is_prime(uid):
    user = users_col.find_one({"user_id": uid})
    return user and user.get('expiry', 0) > datetime.now().timestamp()

# ================= ADMIN =================
@bot.message_handler(commands=['stats'], func=lambda m: m.from_user.id == ADMIN_ID)
def stats(m):
    total = users_col.count_documents({})
    active = users_col.count_documents({"expiry": {"$gt": datetime.now().timestamp()}})
    links = links_col.count_documents({})
    bot.reply_to(m, f"📊 Stats:\nUsers: {total}\nActive: {active}\nLinks: {links}")

@bot.message_handler(commands=['approve'], func=lambda m: m.from_user.id == ADMIN_ID)
def approve(m):
    try:
        _, uid, days = m.text.split()
        expiry = int((datetime.now() + timedelta(days=int(days))).timestamp())
        users_col.update_one({"user_id": int(uid)}, {"$set": {"expiry": expiry}}, upsert=True)
        bot.send_message(int(uid), f"✅ Prime activated for {days} days")
        bot.reply_to(m, f"✅ Done")
    except:
        bot.reply_to(m, "❌ /approve user_id days")

@bot.message_handler(commands=['unapprove'], func=lambda m: m.from_user.id == ADMIN_ID)
def unapprove(m):
    try:
        uid = int(m.text.split()[1]) if len(m.text.split()) > 1 else m.reply_to_message.from_user.id
        users_col.update_one({"user_id": uid}, {"$set": {"expiry": 0}})
        bot.reply_to(m, f"✅ Deactivated {uid}")
    except:
        bot.reply_to(m, "❌ /unapprove user_id")

@bot.message_handler(commands=['broadcast'], func=lambda m: m.from_user.id == ADMIN_ID)
def broadcast(m):
    msg = bot.send_message(ADMIN_ID, "Send message to broadcast:")
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
    bot.send_message(ADMIN_ID, f"✅ Sent to {count} users")

@bot.message_handler(commands=['short'], func=lambda m: m.from_user.id == ADMIN_ID)
def short(m):
    msg = bot.reply_to(m, "Send link to shorten:")
    bot.register_next_step_handler(msg, save_link)

def save_link(m):
    fid = str(uuid.uuid4())[:6]
    links_col.insert_one({"file_id": fid, "url": m.text})
    bot.send_message(ADMIN_ID, f"✅ Link: https://t.me/{bot.get_me().username}?start={fid}")

# ================= USER =================
@bot.message_handler(commands=['start'])
def start(m):
    uid = m.from_user.id
    users_col.update_one({"user_id": uid}, {"$setOnInsert": {"joined": datetime.now()}}, upsert=True)
    
    # Check if user clicked a short link
    text = m.text.split()
    if len(text) > 1:
        fid = text[1]
        
        if is_prime(uid):
            link = links_col.find_one({"file_id": fid})
            if link:
                bot.send_message(uid, f"🎬 {link['url']}")
            else:
                bot.send_message(uid, "❌ Link not found")
        else:
            # Show payment options
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("2 Days - ₹50", callback_data=f"pay_{fid}_2880"))
            markup.row(InlineKeyboardButton("7 Days - ₹100", callback_data=f"pay_{fid}_10080"))
            markup.row(InlineKeyboardButton("1 Month - ₹250", callback_data=f"pay_{fid}_43200"))
            markup.row(InlineKeyboardButton("3 Months - ₹650", callback_data=f"pay_{fid}_129600"))
            bot.send_message(uid, "🔒 Prime required!\nSelect plan:", reply_markup=markup)
    else:
        # Welcome message
        if is_prime(uid):
            user = users_col.find_one({"user_id": uid})
            expiry = datetime.fromtimestamp(user['expiry']).strftime('%d %b %Y')
            bot.send_message(uid, f"👋 Welcome back!\n✅ Prime active till: {expiry}")
        else:
            bot.send_message(uid, "👋 Welcome!\n💎 Buy Prime to access content")

@bot.callback_query_handler(func=lambda call: call.data.startswith('pay_'))
def create_order(call):
    bot.answer_callback_query(call.id)
    
    try:
        _, fid, mins = call.data.split('_')
        amount = PLANS[mins]
        
        # Create Razorpay order
        order = razorpay_client.order.create({
            'amount': amount * 100,
            'currency': 'INR',
            'payment_capture': 1
        })
        
        # Save order
        orders_col.insert_one({
            "order_id": order['id'],
            "user_id": call.from_user.id,
            "fid": fid,
            "mins": mins,
            "amount": amount,
            "created": datetime.now()
        })
        
        # Send payment link
        payment_url = f"https://rzp.io/l/{order['id'][:8]}"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💳 Pay Now", url=payment_url))
        markup.add(InlineKeyboardButton("🔄 Check Status", callback_data=f"check_{order['id']}"))
        
        bot.send_message(
            call.message.chat.id,
            f"💰 Amount: ₹{amount}\n📦 Plan: {PLAN_NAMES[mins]}\n\n🔗 Click below to pay:\n{payment_url}\n\n✅ Auto-approval after payment",
            reply_markup=markup,
            disable_web_page_preview=True
        )
        
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Error: {str(e)[:100]}")
        bot.send_message(ADMIN_ID, f"Payment error: {str(e)}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('check_'))
def check_status(call):
    bot.answer_callback_query(call.id)
    
    order_id = call.data.split('_')[1]
    order = orders_col.find_one({"order_id": order_id})
    
    if not order:
        bot.send_message(call.message.chat.id, "❌ Order expired")
        return
    
    try:
        payments = razorpay_client.order.payments(order_id)
        for payment in payments.get('items', []):
            if payment['status'] == 'captured':
                # Activate
                expiry = int((datetime.now() + timedelta(minutes=int(order['mins']))).timestamp())
                users_col.update_one({"user_id": order['user_id']}, {"$set": {"expiry": expiry}}, upsert=True)
                
                # Send content
                link = links_col.find_one({"file_id": order['fid']})
                if link:
                    bot.send_message(call.message.chat.id, f"✅ Activated!\n🎬 {link['url']}")
                else:
                    bot.send_message(call.message.chat.id, "✅ Payment confirmed!")
                
                orders_col.delete_one({"order_id": order_id})
                return
        
        bot.send_message(call.message.chat.id, "⏳ Payment pending. Complete payment using the link above.")
        
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Error checking payment")

# ================= RUN =================
if __name__ == '__main__':
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
