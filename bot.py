@app.route('/razorpay-webhook', methods=['POST'])
def payment_webhook():
    try:
        data = json.loads(request.get_data().decode())

        if data.get('event') == 'payment_link.paid':
            plink = data['payload']['payment_link']['entity']
            order_id = plink['id']

            order = orders_col.find_one({"order_id": order_id})
            if order:
                expiry_time = int((datetime.now() + timedelta(minutes=order['minutes'])).timestamp())
                users_col.update_one(
                    {"user_id": order['user_id']},
                    {"$set": {"expiry": expiry_time}},
                    upsert=True
                )

                link = links_col.find_one({"file_id": order['file_id']})
                if link:
                    bot.send_message(order['user_id'], f"✅ Payment Successful!\n\n🎬 Your Content:\n{link['url']}")

                orders_col.delete_one({"order_id": order_id})
                bot.send_message(ADMIN_ID, f"✅ Auto-Approved: User {order['user_id']}")

    except Exception as e:
        print(f"Webhook error: {e}")

    return 'OK', 200
