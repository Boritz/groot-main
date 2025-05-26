import logging
import json
import requests
import re
import qrcode
from io import BytesIO
import os
from datetime import datetime, timedelta
import base64
import random
import string
from flask import Flask, request, jsonify, render_template, current_app

app = Flask(__name__)
app.config["ACCESS_TOKEN"] = os.getenv("ACCESS_TOKEN")
app.config["VERSION"] = "v22.0"
app.config["PHONE_NUMBER_ID"] = os.getenv("PHONE_NUMBER_ID")

# In-memory storage for user PINs (replace with database in production)
user_pins = {}

# In-memory session (replace with Redis/db in prod)
session_context = {}

@app.route('/webhook', methods=["POST"])
def webhook():
    body = request.json
    if is_valid_whatsapp_message(body):
        process_whatsapp_message(body)
    return "ok", 200

@app.route('/verify')
def verify_qr():
    return render_template("verify.html")

def log_http_response(response):
    logging.info(f"Status: {response.status_code}")
    logging.info(f"Content-type: {response.headers.get('content-type')}")
    logging.info(f"Body: {response.text}")

def get_text_message_input(recipient, text):
    return json.dumps({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    })

def generate_random_code(length=6):
    """Generate a random alphanumeric code of specified length"""
    characters = string.ascii_uppercase + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

def validate_pin(pin):
    """Validate that PIN is 4 digits"""
    return pin.isdigit() and len(pin) == 4

def generate_response(message_body, wa_id=None, name=None):
    global session_context, user_pins

    if wa_id not in session_context:
        # Check if user has a PIN set
        if wa_id in user_pins:
            session_context[wa_id] = {"step": "ask_name", "visitor_info": {}}
            return "Welcome back to Groot Estate Management!\nPlease enter the visitor's name:"
        else:
            session_context[wa_id] = {"step": "set_pin", "visitor_info": {}}
            return (
                "Welcome to Groot Estate Management!\n"
                "To get started, please set a 4-digit PIN for your bookings.\n"
                "This PIN will be required for future bookings."
            )

    user_session = session_context[wa_id]
    message_body = message_body.strip()
    step = user_session["step"]

    if step == "set_pin":
        if validate_pin(message_body):
            user_pins[wa_id] = message_body
            user_session["step"] = "confirm_pin"
            session_context[wa_id] = user_session
            return "Please confirm your 4-digit PIN by entering it again:"
        else:
            return "Invalid PIN. Please enter exactly 4 digits."

    elif step == "confirm_pin":
        if message_body == user_pins.get(wa_id):
            user_session["step"] = "ask_name"
            session_context[wa_id] = user_session
            return (
                "PIN set successfully!\n\n"
                "Please enter the visitor's name:"
            )
        else:
            return "PINs don't match. Please start over by entering a new 4-digit PIN:"

    elif step == "ask_name":
        user_session["visitor_info"]["name"] = message_body
        user_session["step"] = "ask_date"
        session_context[wa_id] = user_session
        return (
            "Please select the date of visit:\n"
            "1. Today\n"
            "2. Tomorrow\n"
            "3. Specify a date (in the format YYYY-MM-DD)"
        )

    elif step == "ask_date":
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)

        if message_body == "1" or message_body.lower() == "today":
            selected_date = today.strftime("%Y-%m-%d")
        elif message_body == "2" or message_body.lower() == "tomorrow":
            selected_date = tomorrow.strftime("%Y-%m-%d")
        elif message_body.startswith("3") or re.match(r"^\d{4}-\d{2}-\d{2}$", message_body):
            try:
                date_str = message_body.split("3")[-1].strip() if message_body.startswith("3") else message_body
                input_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if input_date < today:
                    return "The date cannot be in the past. Please enter a valid future date (YYYY-MM-DD)."
                selected_date = input_date.strftime("%Y-%m-%d")
            except ValueError:
                return "Invalid date format. Please enter the date in YYYY-MM-DD format."
        else:
            return (
                "Invalid input. Please select the date of visit:\n"
                "1. Today\n"
                "2. Tomorrow\n"
                "3. Specify a date (in the format YYYY-MM-DD)"
            )

        user_session["visitor_info"]["date"] = selected_date
        user_session["step"] = "verify_pin"
        session_context[wa_id] = user_session
        return "Please enter your 4-digit PIN to confirm the booking:"

    elif step == "verify_pin":
        if message_body == user_pins.get(wa_id):
            visitor_info = user_session["visitor_info"]
            
            # Generate random access code
            random_code = generate_random_code()
            visitor_info["code"] = random_code
            
            qr_data = f"Name: {visitor_info['name']}\nDate: {visitor_info['date']}\nAccess Code: {random_code}"
            qr_image_b64, qr_file_path = generate_qr_code_base64(qr_data, visitor_info['name'])
            logging.info(f"QR Code generated and saved at: {qr_file_path}")

            # Send QR code to the user who requested it
            send_qr_code_to_visitor(wa_id, qr_image_b64)
            
            session_context.pop(wa_id, None)

            return (
                f"✅ Booking confirmed!\n\n"
                f"Visitor Name: {visitor_info['name']}\n"
                f"Visit Date: {visitor_info['date']}\n"
                f"Access Code: {random_code}\n\n"
                f"The QR code has been sent to you."
            )
        else:
            return "❌ Incorrect PIN. Please try again or type 'RESET' to start over."

    else:
        session_context[wa_id] = {"step": "ask_name", "visitor_info": {}}
        return "Let's start over. Please enter the visitor's name:"

# ... [keep all the remaining functions unchanged: generate_qr_code_base64, send_qr_code_to_visitor, 
# send_message, process_text_for_whatsapp, process_whatsapp_message, is_valid_whatsapp_message]

if __name__ == "__main__":
    app.run(debug=True)
