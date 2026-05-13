import os, telebot, uuid, re, time, json, random, string
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from datetime import datetime, timedelta
from flask import Flask, request
import razorpay
import requests

# Config
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
    "2days": {"price": 50, "minutes": 2880, "name": "2 Days"},
    "7days": {"price": 100, "minutes": 10080, "name": "7 Days"},
    "1month": {"price": 250, "minutes": 43200, "name": "1 Month"},
    "3months": {"price": 650, "minutes": 129600, "name": "3 Months"}
}

def generate_short_id(length=8):
    """Generate random short ID like B2r2UlWl"""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=length))

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot Running"

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode())
        bot.process_new_updates([update])
        return 'OK', 200
    return 'Forbidden', 403

@app.route('/razorpay-webhook', methods=['POST'])
def payment_webhook():
    try:
        data = json.loads(request.get_data().decode())
        if data.get('event') == 'payment.captured':
            payment = data['payload']['payment']['entity']
            razorpay_order_id = payment['order_id']
            
            # Find order by razorpay_order_id
            order = orders_col.find_one({"razorpay_order_id": razorpay_order_id})
            if order:
                # Activate user
                expiry_time = int((datetime.now() + timedelta(minutes=order['minutes'])).timestamp())
                users_col.update_one(
                    {"user_id": order['user_id']},
                    {"$set": {"expiry": expiry_time}},
                    upsert=True
                )
                
                # Send content
                link = links_col.find_one({"file_id": order['file_id']})
                if link:
                    bot.send_message(order['user_id'], f"✅ Payment Successful!\n\n🎬 Your Content:\n{link['url']}")
                
                # Cleanup
                orders_col.delete_one({"_id": order['_id']})
                bot.send_message(ADMIN_ID, f"✅ Auto-Approved: User {order['user_id']}")
    except Exception as e:
        print(f"Webhook error: {e}")
    return 'OK', 200

def is_prime(user_id):
    user = users_col.find_one({"user_id": user_id})
    return user and user.get('expiry', 0) > datetime.now().timestamp()

# Admin Commands
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
        bot.reply_to(m, f"✅ User {uid} approved")
    except:
        bot.reply_to(m, "Use: /approve user_id days")

@bot.message_handler(commands=['unapprove'], func=lambda m: m.from_user.id == ADMIN_ID)
def unapprove(m):
    try:
        uid = int(m.text.split()[1]) if len(m.text.split()) > 1 else m.reply_to_message.from_user.id
        users_col.update_one({"user_id": uid}, {"$set": {"expiry": 0}})
        bot.reply_to(m, f"✅ User {uid} deactivated")
    except:
        bot.reply_to(m, "Use: /unapprove user_id")

@bot.message_handler(commands=['short'], func=lambda m: m.from_user.id == ADMIN_ID)
def short(m):
    msg = bot.reply_to(m, "Send link to shorten:")
    bot.register_next_step_handler(msg, save_link)

def save_link(m):
    file_id = str(uuid.uuid4())[:8]
    links_col.insert_one({"file_id": file_id, "url": m.text})
    bot.send_message(ADMIN_ID, f"✅ Link: https://t.me/{bot.get_me().username}?start={file_id}")

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
            time.sleep(0.1)
        except:
            pass
    bot.send_message(ADMIN_ID, f"✅ Sent to {count} users")

# User Commands
@bot.message_handler(commands=['start'])
def start(m):
    user_id = m.from_user.id
    users_col.update_one({"user_id": user_id}, {"$setOnInsert": {"joined": datetime.now()}}, upsert=True)
    
    parts = m.text.split()
    if len(parts) > 1:
        file_id = parts[1]
        
        if is_prime(user_id):
            link = links_col.find_one({"file_id": file_id})
            if link:
                bot.send_message(user_id, link['url'])
            else:
                bot.send_message(user_id, "❌ Link not found")
        else:
            # Show payment options
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("2 Days - ₹50", callback_data=f"pay_{file_id}_2days"),
                InlineKeyboardButton("7 Days - ₹100", callback_data=f"pay_{file_id}_7days"),
                InlineKeyboardButton("1 Month - ₹250", callback_data=f"pay_{file_id}_1month"),
                InlineKeyboardButton("3 Months - ₹650", callback_data=f"pay_{file_id}_3months")
            )
            bot.send_message(user_id, "🔒 Prime Required!\nSelect a plan:", reply_markup=markup)
    else:
        if is_prime(user_id):
            user = users_col.find_one({"user_id": user_id})
            expiry_date = datetime.fromtimestamp(user['expiry']).strftime('%d %b %Y')
            bot.send_message(user_id, f"✅ Prime Active till {expiry_date}")
        else:
            bot.send_message(user_id, "👋 Welcome!\nBuy Prime to access content.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('pay_'))
def handle_payment(call):
    bot.answer_callback_query(call.id)
    
    try:
        _, file_id, plan_key = call.data.split('_')
        plan = PLANS[plan_key]
        
        # Create Razorpay Order
        razorpay_order = razorpay_client.order.create({
            'amount': plan['price'] * 100,
            'currency': 'INR',
            'receipt': f"{call.from_user.id}_{int(time.time())}",
            'payment_capture': 1
        })
        
        # Generate short ID like B2r2UlWl
        short_id = generate_short_id()
        
        # Save to database
        orders_col.insert_one({
            "short_id": short_id,  # Store short ID
            "razorpay_order_id": razorpay_order['id'],  # Store original order ID too
            "user_id": call.from_user.id,
            "file_id": file_id,
            "minutes": plan['minutes'],
            "amount": plan['price'],
            "plan_name": plan['name'],
            "created_at": datetime.now()
        })
        
        # CREATE WORKING LINK LIKE https://rzp.io/rzp/B2r2UlWl
        payment_link = f"https://rzp.io/rzp/{short_id}"
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💳 Pay Now", url=payment_link))
        markup.add(InlineKeyboardButton("✓ Check Payment", callback_data=f"check_{short_id}"))
        
        bot.send_message(
            call.message.chat.id,
            f"💰 Plan: {plan['name']}\n"
            f"💵 Amount: ₹{plan['price']}\n\n"
            f"🔗 Payment Link:\n{payment_link}\n\n"
            f"✅ Click Pay Now, complete payment, then click 'Check Payment'",
            reply_markup=markup,
            disable_web_page_preview=True
        )
        
        # Also send admin notification
        bot.send_message(ADMIN_ID, f"New order: {short_id} - ₹{plan['price']} - User: {call.from_user.id}")
        
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Error: {str(e)[:100]}")
        bot.send_message(ADMIN_ID, f"Payment error: {str(e)}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('check_'))
def check_payment(call):
    bot.answer_callback_query(call.id)
    
    short_id = call.data.split('_')[1]
    order = orders_col.find_one({"short_id": short_id})
    
    if not order:
        bot.send_message(call.message.chat.id, "❌ Order not found!")
        return
    
    try:
        # Get payments for this order using razorpay_order_id
        payments = razorpay_client.order.payments(order['razorpay_order_id'])
        
        for payment in payments.get('items', []):
            if payment['status'] == 'captured':
                # Activate user
                expiry = int((datetime.now() + timedelta(minutes=order['minutes'])).timestamp())
                users_col.update_one(
                    {"user_id": order['user_id']},
                    {"$set": {"expiry": expiry, "payment_id": payment['id']}},
                    upsert=True
                )
                
                # Send content
                link = links_col.find_one({"file_id": order['file_id']})
                if link:
                    bot.send_message(call.message.chat.id, f"✅ Payment Verified!\n\n🎬 {link['url']}")
                else:
                    bot.send_message(call.message.chat.id, "✅ Payment Verified!\nSubscription activated.")
                
                orders_col.delete_one({"_id": order['_id']})
                bot.send_message(ADMIN_ID, f"✅ Manual verify: {order['user_id']} - {order['plan_name']}")
                return
        
        bot.send_message(call.message.chat.id, f"⏳ Payment pending.\nComplete payment first.\nLink: https://rzp.io/rzp/{short_id}")
        
    except Exception as e:
        bot.send_message(call.message.chat.id, f"❌ Check failed: {str(e)[:100]}")

# Keep alive (for Render)
import threading
def keep_alive():
    while True:
        time.sleep(300)
        try:
            requests.get(WEBHOOK_URL)
        except:
            pass

if __name__ == '__main__':
    # Start keep alive thread
    threading.Thread(target=keep_alive, daemon=True).start()
    
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
