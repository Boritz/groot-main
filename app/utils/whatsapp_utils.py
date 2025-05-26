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
app.config["ADMIN_NUMBER"] = os.getenv("ADMIN_NUMBER")  # Admin's WhatsApp number

# In-memory storage (replace with database in production)
user_pins = {}
active_codes = {}  # {code: {wa_id, name, date, expiry, used, verified_at}}

# In-memory session (replace with Redis/db in prod)
session_context = {}

def notify_admin(message):
    """Send notification to admin WhatsApp number"""
    if not app.config["ADMIN_NUMBER"]:
        logging.warning("No ADMIN_NUMBER configured, skipping admin notification")
        return
    
    headers = {
        "Content-type": "application/json",
        "Authorization": f"Bearer {app.config['ACCESS_TOKEN']}",
    }

    url = f"https://graph.facebook.com/{app.config['VERSION']}/{app.config['PHONE_NUMBER_ID']}/messages"
    
    data = {
        "messaging_product": "whatsapp",
        "to": app.config["ADMIN_NUMBER"],
        "type": "text",
        "text": {"body": message}
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        logging.info(f"Admin notification sent: {response.status_code}")
    except Exception as e:
        logging.error(f"Failed to send admin notification: {e}")

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

@app.route('/verify_code', methods=["POST"])
def verify_code():
    """Endpoint for security to verify codes"""
    data = request.json
    code = data.get("code", "").strip().upper()
    
    if not code:
        return jsonify({"valid": False, "message": "No code provided"}), 400
    
    if code not in active_codes:
        notify_admin(f"❌ Invalid code attempt: {code}")
        return jsonify({"valid": False, "message": "Invalid code"}), 404
    
    code_data = active_codes[code]
    now = datetime.now()
    
    if code_data["used"]:
        notify_admin(f"⚠️ Already used code: {code}\nVisitor: {code_data['name']}\nDate: {code_data['date']}")
        return jsonify({"valid": False, "message": "Code already used"}), 403
    
    if now > code_data["expiry"]:
        notify_admin(f"⌛ Expired code: {code}\nVisitor: {code_data['name']}\nDate: {code_data['date']}")
        return jsonify({"valid": False, "message": "Code expired"}), 403
    
    # Mark code as used and record verification time
    active_codes[code]["used"] = True
    active_codes[code]["verified_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    
    # Notify admin of successful verification
    notify_admin(
        f"✅ Access granted\n"
        f"Code: {code}\n"
        f"Visitor: {code_data['name']}\n"
        f"Date: {code_data['date']}\n"
        f"Verified at: {now.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    
    return jsonify({
        "valid": True,
        "message": "Access granted",
        "visitor": {
            "name": code_data["name"],
            "date": code_data["date"],
            "code": code,
            "expiry": code_data["expiry"].strftime("%Y-%m-%d %H:%M")
        }
    })

def validate_pin(pin):
    """Validate that PIN is 4 digits"""
    return pin.isdigit() and len(pin) == 4

def generate_response(message_body, wa_id=None, name=None):
    global session_context, user_pins, active_codes

    # Clean up expired codes first
    current_time = datetime.now()
    for code in list(active_codes.keys()):
        if active_codes[code]["expiry"] <= current_time:
            del active_codes[code]

    if wa_id not in session_context:
        if wa_id in user_pins:
            session_context[wa_id] = {"step": "ask_name", "visitor_info": {}}
            return "Welcome back to Groot Estate Management!\nPlease enter visitor name:"
        else:
            session_context[wa_id] = {"step": "set_pin", "visitor_info": {}}
            return (
                "Welcome to Groot Estate Management!\n"
                "Please set a 4-digit PIN for your bookings:"
            )

    user_session = session_context[wa_id]
    message_body = message_body.strip()
    step = user_session["step"]

    if step == "set_pin":
        if validate_pin(message_body):
            user_pins[wa_id] = message_body
            user_session["step"] = "confirm_pin"
            session_context[wa_id] = user_session
            return "Please confirm your 4-digit PIN:"
        else:
            return "Invalid PIN. Please enter exactly 4 digits."

    elif step == "confirm_pin":
        if message_body == user_pins.get(wa_id):
            user_session["step"] = "ask_name"
            session_context[wa_id] = user_session
            return "PIN set successfully!\nPlease enter your name (for future reference):"
        else:
            return "PINs don't match. Please enter a new 4-digit PIN:"

    elif step == "ask_name":
        user_session["visitor_info"]["resident_name"] = message_body
        user_session["step"] = "ask_house_number"
        session_context[wa_id] = user_session
        return "Please enter your house number (for future reference):"

    elif step == "ask_house_number":
        if not message_body:
            return "House number cannot be empty. Please enter your house number:"
        user_session["visitor_info"]["house_number"] = message_body
        user_session["step"] = "ask_street_name"
        session_context[wa_id] = user_session
        return "Please enter your street name (for future reference):"

    elif step == "ask_street_name":
        if not message_body:
            return "Street name cannot be empty. Please enter your street name:"
        user_session["visitor_info"]["street_name"] = message_body
        user_session["step"] = "ask_visitor_name"
        session_context[wa_id] = user_session
        return "Now, please enter the visitor's name:"

    elif step == "ask_visitor_name":
        user_session["visitor_info"]["name"] = message_body
        user_session["step"] = "ask_date"
        session_context[wa_id] = user_session
        return (
            "Select visit date:\n"
            "1. Today\n"
            "2. Tomorrow\n"
            "3. Specify date (YYYY-MM-DD)"
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
                    return "Date cannot be in past. Enter valid date (YYYY-MM-DD)."
                selected_date = input_date.strftime("%Y-%m-%d")
            except ValueError:
                return "Invalid date format. Use YYYY-MM-DD."
        else:
            return (
                "Invalid input. Select date:\n"
                "1. Today\n"
                "2. Tomorrow\n"
                "3. Specify date (YYYY-MM-DD)"
            )

        user_session["visitor_info"]["date"] = selected_date
        user_session["step"] = "verify_pin"
        session_context[wa_id] = user_session
        return "Enter your 4-digit PIN to confirm booking:"

    elif step == "verify_pin":
        if message_body == user_pins.get(wa_id):
            visitor_info = user_session["visitor_info"]
            random_code = generate_random_code()
            expiry_time = datetime.combine(
                datetime.now().date() + timedelta(days=1),
                datetime.min.time()
            )
            
            active_codes[random_code] = {
                "wa_id": wa_id,
                "name": visitor_info["name"],
                "date": visitor_info["date"],
                "expiry": expiry_time,
                "used": False,
                "verified_at": None
            }
            
            qr_data = f"Groot Estate Pass\nName: {visitor_info['name']}\nDate: {visitor_info['date']}\nCode: {random_code}\nExpires: {expiry_time.strftime('%Y-%m-%d %H:%M')}"
            qr_image_b64, _ = generate_qr_code_base64(qr_data, visitor_info['name'])
            
            send_qr_code_to_visitor(wa_id, qr_image_b64)
            
            # Notify admin of new code generation
            notify_admin(
                f"📄 New visitor pass generated\n"
                f"Name: {visitor_info['name']}\n"
                f"Date: {visitor_info['date']}\n"
                f"Code: {random_code}\n"
                f"Expires: {expiry_time.strftime('%Y-%m-%d %H:%M')}"
            )
            
            session_context.pop(wa_id, None)
            
            return (
                f"✅ Booking confirmed!\n"
                f"Name: {visitor_info['name']}\n"
                f"Date: {visitor_info['date']}\n"
                f"Code: {random_code}\n"
                f"Expires: {expiry_time.strftime('%Y-%m-%d %H:%M')}\n\n"
                f"QR code sent. This code expires at midnight."
            )
        else:
            return "❌ Incorrect PIN. Try again or type 'RESET' to start over."

    else:
        session_context[wa_id] = {"step": "ask_name", "visitor_info": {}}
        return "Let's start over. Enter visitor name:"
    
def generate_qr_code_base64(data, visitor_name):
    save_dir = 'qr_codes'
    os.makedirs(save_dir, exist_ok=True)

    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    filename = f"{visitor_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    file_path = os.path.join(save_dir, filename)
    img.save(file_path)
    logging.info(f"QR Code saved locally at: {file_path}")

    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()

    return img_str, file_path


def send_existing_qr_image_to_visitor(visitor_wa_id, filename):
    """
    Send a previously saved QR code image file to the visitor's WhatsApp number.
    """
    file_path = os.path.join("qr_codes", filename)

    if not os.path.exists(file_path):
        logging.error(f"QR image file not found: {file_path}")
        return

    with open(file_path, "rb") as image_file:
        image_data = image_file.read()

    headers = {
        "Authorization": f"Bearer {current_app.config['ACCESS_TOKEN']}"
    }

    upload_url = f"https://graph.facebook.com/{current_app.config['VERSION']}/{current_app.config['PHONE_NUMBER_ID']}/media"
    message_url = f"https://graph.facebook.com/{current_app.config['VERSION']}/{current_app.config['PHONE_NUMBER_ID']}/messages"

    # Upload image
    files = {
        'file': (filename, BytesIO(image_data), 'image/png'),
        'messaging_product': (None, 'whatsapp'),
    }

    try:
        upload_response = requests.post(upload_url, headers=headers, files=files)
        logging.info(f"Media Upload Response: {upload_response.status_code} - {upload_response.text}")

        if upload_response.status_code == 200:
            media_id = upload_response.json().get("id")

            message_data = {
                "messaging_product": "whatsapp",
                "to": visitor_wa_id,
                "type": "image",
                "image": {
                    "id": media_id,
                    "caption": "Your visitor QR code from Groot Estate Management."
                }
            }

            response = requests.post(
                message_url,
                headers={**headers, "Content-Type": "application/json"},
                json=message_data
            )

            logging.info(f"Image Message Send Response: {response.status_code} - {response.text}")
        else:
            logging.error("Failed to upload image.")
    except Exception as e:
        logging.error(f"Error sending existing QR code image: {e}")


def send_qr_code_to_visitor(visitor_wa_id, qr_base64_image):
    headers = {
        "Authorization": f"Bearer {current_app.config['ACCESS_TOKEN']}"
    }

    url = f"https://graph.facebook.com/{current_app.config['VERSION']}/{current_app.config['PHONE_NUMBER_ID']}/messages"
    upload_url = f"https://graph.facebook.com/{current_app.config['VERSION']}/{current_app.config['PHONE_NUMBER_ID']}/media"

    files = {
        'file': ('visitor_qr.png', BytesIO(base64.b64decode(qr_base64_image)), 'image/png'),
        'messaging_product': (None, 'whatsapp')
    }

    try:
        upload_response = requests.post(upload_url, headers=headers, files=files)
        logging.info(f"Media Upload Response: {upload_response.status_code} - {upload_response.text}")

        if upload_response.status_code == 200:
            media_id = upload_response.json().get("id")
            logging.info(f"Uploaded Media ID: {media_id}")

            message_data = {
                "messaging_product": "whatsapp",
                "to": visitor_wa_id,
                "type": "image",
                "image": {
                    "id": media_id,
                    "caption": "Your visitor access QR code."
                }
            }

            response = requests.post(url, headers={**headers, "Content-Type": "application/json"}, json=message_data)
            logging.info(f"Send Message Response: {response.status_code} - {response.text}")
        else:
            logging.error("Image upload failed.")
    except Exception as e:
        logging.error(f"Error sending QR code: {e}")


def send_pass_to_visitor(visitor_wa_id, _):
    """
    Temporary function to test message delivery to visitor's WhatsApp number.
    Sends only a text message to confirm the number is reachable.
    """
    test_message = "Hello! This is a test message from Groot Estate Management. If you're seeing this, messaging works."

    headers = {
        "Content-type": "application/json",
        "Authorization": f"Bearer {current_app.config['ACCESS_TOKEN']}",
    }

    url = f"https://graph.facebook.com/{current_app.config['VERSION']}/{current_app.config['PHONE_NUMBER_ID']}/messages"

    data = {
        "messaging_product": "whatsapp",
        "to": visitor_wa_id,
        "type": "text",
        "text": {
            "body": test_message
        }
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        logging.info(f"Test Text Message Response: {response.status_code} - {response.text}")
    except Exception as e:
        logging.error(f"Failed to send test message: {e}")


def send_test_image_to_visitor(visitor_wa_id):
    """
    Send a static QR code image file from disk to the visitor to isolate media send issues.
    """
    filename = "Ola_20250524_080258.png"
    file_path = os.path.join("qr_codes", filename)

    if not os.path.exists(file_path):
        logging.error(f"QR image file not found: {file_path}")
        return

    headers = {
        "Authorization": f"Bearer {current_app.config['ACCESS_TOKEN']}"
    }

    # Upload image directly
    with open(file_path, "rb") as f:
        files = {
            "file": (filename, f, "image/png"),
            "messaging_product": (None, "whatsapp"),
        }
        upload_url = f"https://graph.facebook.com/{current_app.config['VERSION']}/{current_app.config['PHONE_NUMBER_ID']}/media"
        upload_response = requests.post(upload_url, headers=headers, files=files)

    logging.info(f"Upload Status: {upload_response.status_code}")
    logging.info(f"Upload Response: {upload_response.text}")

    if upload_response.status_code == 200:
        media_id = upload_response.json().get("id")

        message_data = {
            "messaging_product": "whatsapp",
            "to": visitor_wa_id,
            "type": "image",
            "image": {
                "id": media_id,
                "caption": "Test QR code from Groot Estate Management"
            }
        }

        message_url = f"https://graph.facebook.com/{current_app.config['VERSION']}/{current_app.config['PHONE_NUMBER_ID']}/messages"
        response = requests.post(message_url, headers={**headers, "Content-Type": "application/json"},
                                 json=message_data)

        logging.info(f"Send Media Message Status: {response.status_code}")
        logging.info(f"Send Media Message Response: {response.text}")
    else:
        logging.error("Image upload failed.")


def send_message(data):
    headers = {
        "Content-type": "application/json",
        "Authorization": f"Bearer {current_app.config['ACCESS_TOKEN']}",
    }

    url = f"https://graph.facebook.com/{current_app.config['VERSION']}/{current_app.config['PHONE_NUMBER_ID']}/messages"

    try:
        response = requests.post(url, data=data, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.Timeout:
        logging.error("Timeout occurred while sending message")
        return jsonify({"status": "error", "message": "Request timed out"}), 408
    except requests.RequestException as e:
        logging.error(f"Request failed: {e}")
        return jsonify({"status": "error", "message": "Failed to send message"}), 500
    else:
        log_http_response(response)
        return response


def process_text_for_whatsapp(text):
    text = re.sub(r"\【.*?\】", "", text).strip()
    return re.sub(r"\*\*(.*?)\*\*", r"*\1*", text)


def process_whatsapp_message(body):
    wa_id = body["entry"][0]["changes"][0]["value"]["contacts"][0]["wa_id"]
    name = body["entry"][0]["changes"][0]["value"]["contacts"][0]["profile"]["name"]
    message = body["entry"][0]["changes"][0]["value"]["messages"][0]
    message_body = message["text"]["body"]

    # Check if message is from admin
    if wa_id == app.config["ADMIN_NUMBER"]:
        # Admin is requesting to verify a code
        if not message_body.strip().upper().startswith("VERIFY"):
            response = "🔍 Admin: Please send 'VERIFY <code>' to check a visitor pass"
        else:
            # Extract code from message (format: "VERIFY ABC123")
            try:
                code = message_body.strip().upper().split()[1]
                # Simulate a verify_code API call
                verification = verify_code_admin(code)
                response = verification["message"]
            except IndexError:
                response = "❌ Invalid format. Please use: VERIFY <code>"
    else:
        # Normal user interaction
        response = generate_response(message_body, wa_id, name)

    # Send response back to the sender
    data = get_text_message_input(wa_id, response)
    send_message(data)

def verify_code_admin(code):
    """Special verification function for admin with more detailed responses"""
    if not code:
        return {"valid": False, "message": "❌ No code provided"}
    
    if code not in active_codes:
        return {"valid": False, "message": "❌ Invalid code: " + code}
    
    code_data = active_codes[code]
    now = datetime.now()
    
    if code_data["used"]:
        return {
            "valid": False,
            "message": (
                f"⚠️ Code already used\n"
                f"Resident: {code_data['resident_name']}\n"
                f"Address: {code_data['house_number']} {code_data['street_name']}\n"
                f"Visitor: {code_data['name']}\n"
                f"Date: {code_data['date']}\n"
                f"Verified at: {code_data['verified_at'] or 'N/A'}"
            )
        }
    
    if now > code_data["expiry"]:
        return {
            "valid": False,
            "message": (
                f"⌛ Code expired\n"
                f"Resident: {code_data['resident_name']}\n"
                f"Address: {code_data['house_number']} {code_data['street_name']}\n"
                f"Visitor: {code_data['name']}\n"
                f"Date: {code_data['date']}\n"
                f"Expired at: {code_data['expiry'].strftime('%Y-%m-%d %H:%M')}"
            )
        }
    
    # Mark code as used and record verification time
    active_codes[code]["used"] = True
    active_codes[code]["verified_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    
    return {
        "valid": True,
        "message": (
            f"✅ Access granted\n"
            f"Resident: {code_data['resident_name']}\n"
            f"Address: {code_data['house_number']} {code_data['street_name']}\n"
            f"Visitor: {code_data['name']}\n"
            f"Date: {code_data['date']}\n"
            f"Code: {code}\n"
            f"Expires: {code_data['expiry'].strftime('%Y-%m-%d %H:%M')}"
        )
    }


def is_valid_whatsapp_message(body):
    return (
            body.get("object")
            and body.get("entry")
            and body["entry"][0].get("changes")
            and body["entry"][0]["changes"][0].get("value")
            and body["entry"][0]["changes"][0]["value"].get("messages")
            and body["entry"][0]["changes"][0]["value"]["messages"][0]
    )


if __name__ == "__main__":
    app.run(debug=True)
