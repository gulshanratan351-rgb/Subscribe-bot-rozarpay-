import os, telebot, urllib.parse, uuid, datetime, re, threading, random, time, hashlib, hmac
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import razorpay

# ================= CONFIGURATION =================
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
ADMIN_ID = int(os.getenv('ADMIN_ID'))
WEBHOOK_URL = os.getenv('WEBHOOK_URL')

# Razorpay Credentials
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET')

bot = telebot.TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URI)
db = client['sub_management']
users_col = db['users']
links_col = db['short_links']
temp_orders_col = db['temp_orders']

# Initialize Razorpay Client
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# Plans Mapping (Duration in minutes : Price in INR)
PLANS = {
    "2880": 50,      # 2 Days
    "10080": 100,    # 7 Days
    "43200": 200,    # 1 Month
    "129600": 400    # 3 Months
}

app = Flask(__name__)

# ================= FLASK SERVER =================
@app.route('/')
def home():
    return "🚀 Bot is Online with Razorpay Auto-Payment!"

@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "OK", 200
    return "Forbidden", 403

# ================= RAZORPAY WEBHOOK =================
@app.route('/razorpay-webhook', methods=['POST'])
def razorpay_webhook():
    """Handle Razorpay payment success webhook"""
    
    # Verify webhook signature
    razorpay_signature = request.headers.get('X-Razorpay-Signature')
    webhook_secret = os.getenv('RAZORPAY_WEBHOOK_SECRET', RAZORPAY_KEY_SECRET)
    
    payload_body = request.get_data().decode('utf-8')
    expected_signature = hmac.new(
        webhook_secret.encode('utf-8'),
        payload_body.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    if not hmac.compare_digest(expected_signature, razorpay_signature):
        return jsonify({"error": "Invalid signature"}), 403
    
    data = request.json
    event = data.get('event')
    
    if event == 'payment.captured':
        payment = data['payload']['payment']['entity']
        order_id = payment['order_id']
        payment_id = payment['id']
        
        order_data = temp_orders_col.find_one({"order_id": order_id})
        
        if order_data:
            uid = order_data['user_id']
            fid = order_data['fid']
            mins = order_data['mins']
            
            # Activate subscription
            expiry = int((datetime.now() + timedelta(minutes=int(mins))).timestamp())
            users_col.update_one(
                {"user_id": uid}, 
                {"$set": {"expiry": expiry, "payment_id": payment_id}}, 
                upsert=True
            )
            
            # Send success message
            try:
                link_data = links_col.find_one({"file_id": fid})
                if link_data:
                    bot.send_message(uid, f"✅ **Payment Successful!**\n\n🎬 **Your Content:**\n{link_data['url']}")
                else:
                    bot.send_message(uid, f"✅ **Payment Successful!**\n\nYour subscription is now active!")
                
                bot.send_message(ADMIN_ID, f"✅ Auto-Approved!\nUser: `{uid}`\nPayment ID: `{payment_id}`")
            except:
                pass
            
            temp_orders_col.delete_one({"order_id": order_id})
    
    return jsonify({"status": "ok"}), 200

# ================= HELPER FUNCTIONS =================
def is_prime(uid):
    user = users_col.find_one({"user_id": uid})
    if user and user.get('expiry', 0) > datetime.now().timestamp():
        return True
    return False

def get_expiry_date(timestamp):
    return datetime.fromtimestamp(timestamp).strftime('%d %b %Y, %I:%M %p')

# ================= ADMIN COMMANDS =================

@bot.message_handler(commands=['stats'], func=lambda m: m.from_user.id == ADMIN_ID)
def stats_handler(message):
    total_users = users_col.count_documents({})
    active_prime = users_col.count_documents({"expiry": {"$gt": datetime.now().timestamp()}})
    total_links = links_col.count_documents({})
    
    text = (f"📊 **Bot Statistics**\n\n"
            f"👤 Total Users: {total_users}\n"
            f"👑 Active Prime: {active_prime}\n"
            f"🔗 Total Links: {total_links}")
    bot.reply_to(message, text)

@bot.message_handler(commands=['approve'], func=lambda m: m.from_user.id == ADMIN_ID)
def manual_approve(message):
    try:
        args = message.text.split()
        if len(args) < 3:
            return bot.reply_to(message, "❌ Format: `/approve [User_ID] [Days]`")
        
        target_id = int(args[1])
        days = int(args[2])
        expiry = int((datetime.now() + timedelta(days=days)).timestamp())
        
        users_col.update_one({"user_id": target_id}, {"$set": {"expiry": expiry}}, upsert=True)
        bot.send_message(target_id, f"✅ Admin ne {days} days ke liye Prime activate kar diya.")
        bot.reply_to(message, f"✅ User {target_id} approved for {days} days.")
    except Exception as e:
        bot.reply_to(message, f"⚠️ Error: {str(e)}")

@bot.message_handler(commands=['unapprove'], func=lambda m: m.from_user.id == ADMIN_ID)
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
            bot.send_message(target_id, "❌ Your Prime membership has been revoked.")
            bot.reply_to(message, f"✅ User `{target_id}` deactivated.")
        else:
            bot.reply_to(message, "❌ User ID not found.")
    except Exception as e:
        bot.reply_to(message, f"⚠️ Error: {str(e)}")

@bot.message_handler(commands=['broadcast'], func=lambda m: m.from_user.id == ADMIN_ID)
def broadcast_msg(message):
    msg = bot.send_message(ADMIN_ID, "📢 Send message to broadcast:")
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
    links_col.insert_one({"file_id": file_id, "url": message.text})
    bot.send_message(ADMIN_ID, f"✅ **Link Created!**\n\nURL: `https://t.me/{bot.get_me().username}?start=vid_{file_id}`")

# ================= USER LOGIC =================

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
                bot.send_message(uid, f"🍿 **Your Content:**\n\n{link_data['url']}", disable_web_page_preview=True)
            else:
                bot.send_message(uid, "❌ Link expired or removed.")
        else:
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("💳 2 Days - ₹50", callback_data=f"pay_{fid}_2880"))
            markup.row(InlineKeyboardButton("💳 7 Days - ₹100", callback_data=f"pay_{fid}_10080"))
            markup.row(InlineKeyboardButton("💳 1 Month - ₹200", callback_data=f"pay_{fid}_43200"))
            markup.row(InlineKeyboardButton("💳 3 Months - ₹400", callback_data=f"pay_{fid}_129600"))
            
            bot.send_message(uid, "🔒 **Membership Required!**\n\nSelect a plan:", reply_markup=markup)
    else:
        text = "👋 Welcome!\n\n"
        if is_prime(uid):
            u = users_col.find_one({"user_id": uid})
            text += f"👑 Status: **Prime User**\n📅 Expiry: `{get_expiry_date(u['expiry'])}`"
        else:
            text += "👑 Status: **Free User**\nJoin Prime to access premium content."
        bot.send_message(uid, text)

@bot.callback_query_handler(func=lambda call: call.data.startswith('pay_'))
def create_razorpay_order(call):
    """Create Razorpay order"""
    bot.answer_callback_query(call.id)
    
    try:
        _, fid, mins = call.data.split('_')
        amount = PLANS[int(mins)]
        uid = call.from_user.id
        
        # Create Razorpay Order
        order_data = {
            'amount': amount * 100,
            'currency': 'INR',
            'receipt': f'order_{uid}_{int(time.time())}',
            'payment_capture': 1
        }
        
        order = razorpay_client.order.create(data=order_data)
        order_id = order['id']
        
        # Store in database
        temp_orders_col.update_one(
            {"user_id": uid},
            {"$set": {
                "order_id": order_id,
                "fid": fid,
                "mins": mins,
                "amount": amount,
                "created_at": datetime.now()
            }},
            upsert=True
        )
        
        # Payment link
        payment_link = f"https://rzp.io/l/pay-{order_id[:8]}"
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💳 Pay Now", url=payment_link))
        markup.add(InlineKeyboardButton("🔄 Check Payment", callback_data=f"check_{order_id}"))
        
        bot.send_message(
            call.message.chat.id,
            f"💰 **Amount: ₹{amount}**\n\n"
            f"Click below to pay via Razorpay (Card/UPI/Bank)\n\n"
            f"🔗 {payment_link}\n\n"
            f"✅ Payment will be **auto-approved** instantly!\n\n"
            f"🆔 Order: `{order_id[:12]}`",
            reply_markup=markup,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        
    except Exception as e:
        bot.send_message(call.message.chat.id, f"⚠️ Error: {str(e)}\nTry again or contact admin.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('check_'))
def check_payment_status(call):
    """Check payment status manually"""
    bot.answer_callback_query(call.id, "Checking...")
    
    order_id = call.data.split('_')[1]
    order_data = temp_orders_col.find_one({"order_id": order_id})
    
    if not order_data:
        bot.send_message(call.message.chat.id, "❌ Order not found!")
        return
    
    try:
        payments = razorpay_client.order.payments(order_id)
        
        if payments and payments.get('items'):
            for payment in payments['items']:
                if payment['status'] == 'captured':
                    uid = order_data['user_id']
                    mins = order_data['mins']
                    fid = order_data['fid']
                    
                    expiry = int((datetime.now() + timedelta(minutes=int(mins))).timestamp())
                    users_col.update_one(
                        {"user_id": uid},
                        {"$set": {"expiry": expiry, "payment_id": payment['id']}},
                        upsert=True
                    )
                    
                    link_data = links_col.find_one({"file_id": fid})
                    if link_data:
                        bot.send_message(call.message.chat.id, f"✅ **Payment Verified!**\n\n🎬 {link_data['url']}")
                    else:
                        bot.send_message(call.message.chat.id, "✅ **Payment Verified!** Subscription active.")
                    
                    temp_orders_col.delete_one({"order_id": order_id})
                    return
        
        bot.send_message(call.message.chat.id, f"⏳ Payment pending.\nOrder ID: `{order_id[:12]}`\nComplete payment using the link above.")
        
    except Exception as e:
        bot.send_message(call.message.chat.id, f"⚠️ Error: {str(e)}")

# ================= RUNNER =================
if __name__ == '__main__':
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
