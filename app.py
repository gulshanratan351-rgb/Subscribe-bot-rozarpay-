import os, time, uuid, re
from datetime import datetime, timedelta
from flask import Flask, request
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
import razorpay

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

bot = telebot.TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URI)
db = client["sub_management"]

users_col = db["users"]
links_col = db["short_links"]
payments_col = db["payments"]

rzp = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

PLANS = {
    "2880": {"label": "2 Days", "amount": 50},
    "10080": {"label": "7 Days", "amount": 100},
    "43200": {"label": "1 Month", "amount": 250},
    "129600": {"label": "3 Months", "amount": 800},
}

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running ✅"

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") == "application/json":
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
        return "OK", 200
    return "Forbidden", 403

@app.route("/pay/<order_id>")
def pay_page(order_id):
    pay = payments_col.find_one({"order_id": order_id})
    if not pay:
        return "Invalid payment link", 404

    amount = pay["amount"]
    plan_label = pay["plan_label"]

    return f"""
<!DOCTYPE html>
<html>
<head>
<title>Payment</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<style>
body{{font-family:Arial;background:#f4f7ff;text-align:center;padding:30px}}
.card{{background:white;padding:25px;border-radius:18px;max-width:420px;margin:auto;box-shadow:0 10px 30px #0002}}
button{{background:#2563eb;color:white;border:0;padding:14px 24px;border-radius:12px;font-size:18px;font-weight:bold}}
</style>
</head>
<body>
<div class="card">
<h2>Complete Payment</h2>
<p>Plan: <b>{plan_label}</b></p>
<p>Amount: <b>₹{amount}</b></p>
<button id="payBtn">Pay Now</button>
</div>

<script>
var options = {{
    "key": "{RAZORPAY_KEY_ID}",
    "amount": "{amount * 100}",
    "currency": "INR",
    "name": "Merawala Digital Services",
    "description": "{plan_label} Membership",
    "order_id": "{order_id}",
    "callback_url": "{WEBHOOK_URL}/payment-callback",
    "redirect": true,
    "prefill": {{
        "name": "Customer"
    }},
    "theme": {{
        "color": "#2563eb"
    }}
}};
var rzp1 = new Razorpay(options);
document.getElementById("payBtn").onclick = function(e){{
    rzp1.open();
    e.preventDefault();
}}
</script>
</body>
</html>
"""

@app.route("/payment-callback", methods=["GET", "POST"])
def payment_callback():
    razorpay_payment_id = request.values.get("razorpay_payment_id")
    razorpay_order_id = request.values.get("razorpay_order_id")
    razorpay_signature = request.values.get("razorpay_signature")

    if not razorpay_payment_id or not razorpay_order_id or not razorpay_signature:
        return "Payment failed or cancelled."

    pay = payments_col.find_one({"order_id": razorpay_order_id})
    if not pay:
        return "Payment record not found."

    if pay.get("status") == "paid":
        return "Payment already verified ✅"

    try:
        rzp.utility.verify_payment_signature({
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature": razorpay_signature
        })
    except Exception:
        payments_col.update_one(
            {"order_id": razorpay_order_id},
            {"$set": {"status": "failed"}}
        )
        return "Payment verification failed ❌"

    uid = pay["user_id"]
    mins = int(pay["mins"])
    fid = pay.get("fid")

    expiry = int((datetime.now() + timedelta(minutes=mins)).timestamp())

    users_col.update_one(
        {"user_id": uid},
        {"$set": {"expiry": expiry}},
        upsert=True
    )

    payments_col.update_one(
        {"order_id": razorpay_order_id},
        {"$set": {
            "status": "paid",
            "payment_id": razorpay_payment_id,
            "paid_at": datetime.now()
        }}
    )

    msg = f"✅ Payment successful!\n\n👑 Your membership is active.\n📅 Expiry: {get_expiry_date(expiry)}"

    if fid:
        link_data = links_col.find_one({"file_id": fid})
        if link_data:
            msg += f"\n\n🔗 Your Content:\n{link_data['url']}"

    try:
        bot.send_message(uid, msg, disable_web_page_preview=True)
    except:
        pass

    return """
<h2>Payment Successful ✅</h2>
<p>Your membership has been activated. Please return to Telegram.</p>
"""

def is_prime(uid):
    user = users_col.find_one({"user_id": uid})
    return bool(user and user.get("expiry", 0) > datetime.now().timestamp())

def get_expiry_date(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%d %b %Y, %I:%M %p")

def create_payment(uid, fid, mins):
    plan = PLANS[str(mins)]
    amount = plan["amount"]

    receipt = f"rcpt_{uid}_{int(time.time())}"

    order = rzp.order.create({
        "amount": amount * 100,
        "currency": "INR",
        "receipt": receipt,
        "payment_capture": 1
    })

    payments_col.insert_one({
        "user_id": uid,
        "fid": fid,
        "mins": str(mins),
        "amount": amount,
        "plan_label": plan["label"],
        "order_id": order["id"],
        "receipt": receipt,
        "status": "created",
        "created_at": datetime.now()
    })

    return order["id"]

@bot.message_handler(commands=["start"])
def start(message):
    uid = message.from_user.id

    users_col.update_one(
        {"user_id": uid},
        {"$setOnInsert": {"joined": datetime.now()}},
        upsert=True
    )

    match = re.search(r"vid_([a-zA-Z0-9]+)", message.text)

    if match:
        fid = match.group(1)

        if is_prime(uid):
            link_data = links_col.find_one({"file_id": fid})
            if link_data:
                bot.send_message(uid, f"✅ Your content:\n\n{link_data['url']}", disable_web_page_preview=True)
            else:
                bot.send_message(uid, "❌ Link expired or removed.")
            return

        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("💳 2 Days - ₹50", callback_data=f"pay_{fid}_2880"))
        markup.row(InlineKeyboardButton("💳 7 Days - ₹100", callback_data=f"pay_{fid}_10080"))
        markup.row(InlineKeyboardButton("💳 1 Month - ₹250", callback_data=f"pay_{fid}_43200"))
        markup.row(InlineKeyboardButton("💳 3 Months - ₹800", callback_data=f"pay_{fid}_129600"))

        bot.send_message(uid, "🔒 Membership required.\n\nSelect a plan:", reply_markup=markup)
        return

    text = "👋 Welcome!\n\n"
    if is_prime(uid):
        user = users_col.find_one({"user_id": uid})
        text += f"👑 Status: Prime\n📅 Expiry: {get_expiry_date(user['expiry'])}"
    else:
        text += "👤 Status: Free\n\nUse a premium link and choose a plan."
    bot.send_message(uid, text)

@bot.callback_query_handler(func=lambda call: call.data.startswith("pay_"))
def pay_callback(call):
    bot.answer_callback_query(call.id)

    try:
        _, fid, mins = call.data.split("_")
        uid = call.from_user.id

        order_id = create_payment(uid, fid, mins)
        pay_url = f"{WEBHOOK_URL}/pay/{order_id}"

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("Pay Now ✅", url=pay_url))

        amount = PLANS[mins]["amount"]
        label = PLANS[mins]["label"]

        bot.send_message(
            uid,
            f"💳 Payment Details\n\nPlan: {label}\nAmount: ₹{amount}\n\nClick below to pay securely.",
            reply_markup=markup
        )

    except Exception as e:
        bot.send_message(call.message.chat.id, f"Error: {e}")

@bot.message_handler(commands=["stats"], func=lambda m: m.from_user.id == ADMIN_ID)
def stats(message):
    total_users = users_col.count_documents({})
    active_prime = users_col.count_documents({"expiry": {"$gt": datetime.now().timestamp()}})
    total_links = links_col.count_documents({})
    total_payments = payments_col.count_documents({"status": "paid"})

    bot.reply_to(
        message,
        f"📊 Bot Stats\n\nUsers: {total_users}\nPrime: {active_prime}\nLinks: {total_links}\nPaid Payments: {total_payments}"
    )

@bot.message_handler(commands=["approve"], func=lambda m: m.from_user.id == ADMIN_ID)
def approve(message):
    try:
        args = message.text.split()
        if len(args) < 3:
            return bot.reply_to(message, "Format: /approve USER_ID DAYS")

        target_id = int(args[1])
        days = int(args[2])
        expiry = int((datetime.now() + timedelta(days=days)).timestamp())

        users_col.update_one(
            {"user_id": target_id},
            {"$set": {"expiry": expiry}},
            upsert=True
        )

        bot.send_message(target_id, f"✅ Admin activated your membership for {days} days.")
        bot.reply_to(message, "Approved ✅")
    except Exception as e:
        bot.reply_to(message, str(e))

@bot.message_handler(commands=["unapprove", "deactivate"], func=lambda m: m.from_user.id == ADMIN_ID)
def unapprove(message):
    try:
        args = message.text.split()
        if len(args) < 2:
            return bot.reply_to(message, "Format: /unapprove USER_ID")

        target_id = int(args[1])

        users_col.update_one(
            {"user_id": target_id},
            {"$set": {"expiry": 0}},
            upsert=True
        )

        bot.send_message(target_id, "❌ Your membership has been deactivated.")
        bot.reply_to(message, "User deactivated ✅")
    except Exception as e:
        bot.reply_to(message, str(e))

@bot.message_handler(commands=["broadcast"], func=lambda m: m.from_user.id == ADMIN_ID)
def broadcast(message):
    msg = bot.send_message(ADMIN_ID, "Send broadcast message:")
    bot.register_next_step_handler(msg, send_broadcast)

def send_broadcast(message):
    count = 0
    for user in users_col.find({}):
        try:
            bot.copy_message(user["user_id"], ADMIN_ID, message.message_id)
            count += 1
            time.sleep(0.05)
        except:
            pass

    bot.send_message(ADMIN_ID, f"Broadcast sent to {count} users ✅")

@bot.message_handler(commands=["short"], func=lambda m: m.from_user.id == ADMIN_ID)
def short(message):
    msg = bot.reply_to(message, "Send original link:")
    bot.register_next_step_handler(msg, save_link)

def save_link(message):
    file_id = str(uuid.uuid4())[:8].lower()

    links_col.insert_one({
        "file_id": file_id,
        "url": message.text,
        "created_at": datetime.now()
    })

    username = bot.get_me().username
    bot.send_message(
        ADMIN_ID,
        f"✅ Link created:\n\nhttps://t.me/{username}?start=vid_{file_id}"
    )

if __name__ == "__main__":
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
