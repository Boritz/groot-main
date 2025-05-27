import logging
import json
import requests
import re
import qrcode
from io import BytesIO
import os
import bcrypt
from datetime import datetime, timedelta
import base64
import random
import string
from flask import Flask, request, jsonify, render_template, current_app
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)
app.config["ACCESS_TOKEN"] = os.getenv("ACCESS_TOKEN")
app.config["VERSION"] = "v22.0"
app.config["PHONE_NUMBER_ID"] = os.getenv("PHONE_NUMBER_ID")
app.config["ADMIN_NUMBER"] = os.getenv("ADMIN_NUMBER")  # Admin's WhatsApp number

# Initialize Firestore (add this near the top of your file)
def initialize_firestore():
    try:
        cred_dict = json.loads(os.environ['FIREBASE_CREDENTIALS'])
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        logging.error(f"Error initializing Firestore: {e}")
        raise

db = initialize_firestore()

# Collection names
SESSIONS_COLLECTION = "whatsapp_sessions"
RESIDENTS_COLLECTION = "residents"
CODES_COLLECTION = "active_codes"

# Helper functions for Firestore operations
def get_session(wa_id):
    doc_ref = db.collection(SESSIONS_COLLECTION).document(wa_id)
    doc = doc_ref.get()
    return doc.to_dict() if doc.exists else None

def update_session(wa_id, session_data):
    db.collection(SESSIONS_COLLECTION).document(wa_id).set(session_data, merge=True)

def delete_session(wa_id):
    db.collection(SESSIONS_COLLECTION).document(wa_id).delete()

def get_resident(wa_id):
    doc_ref = db.collection(RESIDENTS_COLLECTION).document(wa_id)
    doc = doc_ref.get()
    return doc.to_dict() if doc.exists else None

def update_resident(wa_id, resident_data):
    db.collection(RESIDENTS_COLLECTION).document(wa_id).set(resident_data)

def get_code(code):
    doc_ref = db.collection(CODES_COLLECTION).document(code)
    doc = doc_ref.get()
    return doc.to_dict() if doc.exists else None

def update_code(code, code_data):
    db.collection(CODES_COLLECTION).document(code).set(code_data)

def delete_code(code):
    db.collection(CODES_COLLECTION).document(code).delete()

def get_expired_codes():
    now = datetime.now()
    expired_codes = db.collection(CODES_COLLECTION) \
        .where("expiry", "<=", now) \
        .stream()
    return [code.id for code in expired_codes]


def hash_pin(pin):
    return bcrypt.hashpw(pin.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


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
    
    code_data = get_code(code)
    if not code_data:
        notify_admin(f"❌ Invalid code attempt: {code}")
        return jsonify({"valid": False, "message": "Invalid code"}), 404
    
    now = datetime.now()
    
    if code_data["used"]:
        notify_admin(f"⚠️ Already used code: {code}\nVisitor: {code_data['name']}\nDate: {code_data['date']}")
        return jsonify({"valid": False, "message": "Code already used"}), 403
    
    if now > code_data["expiry"]:
        notify_admin(f"⌛ Expired code: {code}\nVisitor: {code_data['name']}\nDate: {code_data['date']}")
        return jsonify({"valid": False, "message": "Code expired"}), 403
    
    # Mark code as used and record verification time
    code_data["used"] = True
    code_data["verified_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    update_code(code, code_data)
    
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
    # Clean up expired codes first
    expired_codes = get_expired_codes()
    for code in expired_codes:
        delete_code(code)

    # Get or initialize session
    user_session = get_session(wa_id)
    if not user_session:
        resident = get_resident(wa_id)
        if resident:
            # Returning user - start fresh with visitor name
            user_session = {
                "step": "ask_visitor_name", 
                "visitor_info": {},
                "resident_info": {
                    "pin": resident.get("pin")  # hashed PIN from Firestore
                },
                "is_returning_user": True
            }
            update_session(wa_id, user_session)
            return "Welcome back to Groot Estate Management!\nPlease enter visitor name:"
        else:
            # New user - start with PIN setup
            user_session = {
                "step": "set_pin", 
                "visitor_info": {}, 
                "is_new_user": True
            }
            update_session(wa_id, user_session)
            return "Welcome to Groot Estate Management!\nPlease set a 4-digit PIN for your bookings:"

    message_body = message_body.strip()
    step = user_session["step"]

    if step == "set_pin":
        if validate_pin(message_body):
            user_session["pin"] = message_body
            user_session["step"] = "confirm_pin"
            update_session(wa_id, user_session)
            return "Please confirm your 4-digit PIN:"
        return "Invalid PIN. Please enter exactly 4 digits."

    elif step == "confirm_pin":
        if message_body == user_session.get("pin"):
            if user_session.get("is_new_user"):
                user_session["step"] = "ask_resident_name"
                user_session["resident_info"] = {"pin": user_session["pin"]}
                update_session(wa_id, user_session)
                return "PIN set successfully!\nPlease enter your name (resident):"
            else:
                user_session["step"] = "ask_visitor_name"
                update_session(wa_id, user_session)
                return "PIN verified!\nPlease enter visitor name:"
        return "PINs don't match. Please enter a new 4-digit PIN:"

    elif step == "ask_resident_name":
        user_session["resident_info"] = {"name": message_body}
        user_session["step"] = "ask_house_number"
        update_session(wa_id, user_session)
        return "Please enter your house number:"

    elif step == "ask_house_number":
        user_session["resident_info"]["house_number"] = message_body
        user_session["step"] = "ask_street_name"
        update_session(wa_id, user_session)
        return "Please enter your street name:"

    elif step == "ask_street_name":
        resident_data = {
            "name": user_session["resident_info"]["name"],
            "house_number": user_session["resident_info"]["house_number"],
            "street_name": message_body,
            "wa_id": wa_id,
            "pin": hash_pin(user_session["resident_info"]["pin"]),
            "created_at": datetime.now()
        }
        update_resident(wa_id, resident_data)
        user_session["step"] = "ask_visitor_name"
        update_session(wa_id, user_session)
        return "Resident information saved!\nNow, please enter visitor name:"

    elif step == "ask_visitor_name":
        user_session["visitor_info"] = {"name": message_body}
        user_session["step"] = "ask_date"
        update_session(wa_id, user_session)
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
        update_session(wa_id, user_session)
        return "Enter your 4-digit PIN to confirm booking:"

    elif step == "verify_pin":
        hashed_pin = user_session.get("resident_info", {}).get("pin")
        if hashed_pin and bcrypt.checkpw(message_body.encode('utf-8'), hashed_pin.encode('utf-8')):
            visitor_info = user_session["visitor_info"]
            random_code = generate_random_code()
            expiry_time = datetime.combine(
                datetime.now().date() + timedelta(days=1),
                datetime.min.time()
            )
            
            code_data = {
                "wa_id": wa_id,
                "name": visitor_info["name"],
                "date": visitor_info["date"],
                "expiry": expiry_time,
                "used": False,
                "verified_at": None,
                "created_at": datetime.now()
            }
            update_code(random_code, code_data)
            
            qr_data = f"Groot Estate Pass\nName: {visitor_info['name']}\nDate: {visitor_info['date']}\nCode: {random_code}\nExpires: {expiry_time.strftime('%Y-%m-%d %H:%M')}"
            qr_image_b64, _ = generate_qr_code_base64(qr_data, visitor_info['name'])

            # Save booking to Firestore
            booking_data = {
                "wa_id": wa_id,
                "visitor_name": visitor_info["name"],
                "date": visitor_info["date"],
                "code": random_code,
                "expiry": expiry_time,
                "created_at": datetime.now(),
                # Optional
                "qr_base64": qr_image_b64
            }
            db.collection("bookings").add(booking_data)
            
            # user_session.pop(wa_id, None)
            delete_session(wa_id)
            
            # # Reset session for next booking
            # new_session = {
            #     "step": "ask_visitor_name",
            #     "visitor_info": {},
            #     "is_returning_user": True
            # }
            # update_session(wa_id, new_session)
            
            return (
                f"✅ Booking confirmed!\n"
                f"Name: {visitor_info['name']}\n"
                f"Date: {visitor_info['date']}\n"
                f"Code: {random_code}\n"
                f"Expires: {expiry_time.strftime('%Y-%m-%d %H:%M')}\n\n"
                f"QR code sent. This code expires at midnight."
            )
        return "❌ Incorrect PIN. Try again or type 'RESET' to start over."

    else:
        # Fallback for unexpected states
        new_session = {
            "step": "ask_visitor_name",
            "visitor_info": {},
            "is_returning_user": True
        }
        update_session(wa_id, new_session)
        return "Let's start over. Please enter visitor name:"




def get_recent_bookings(wa_id):
    two_months_ago = datetime.now() - timedelta(days=60)
    bookings_ref = db.collection("bookings") \
        .where("wa_id", "==", wa_id) \
        .where("created_at", ">=", two_months_ago)

    return [doc.to_dict() for doc in bookings_ref.stream()]


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
    try:
        wa_id = body["entry"][0]["changes"][0]["value"]["contacts"][0]["wa_id"]
        name = body["entry"][0]["changes"][0]["value"]["contacts"][0]["profile"]["name"]
        message = body["entry"][0]["changes"][0]["value"]["messages"][0]
        
        # Check if message contains text
        if "text" in message:
            message_body = message["text"]["body"]
            
            # Check if message is from admin
            if wa_id == app.config["ADMIN_NUMBER"]:
                # Admin verification logic
                if not message_body.strip().upper().startswith("VERIFY"):
                    response = "🔍 Admin: Please send 'VERIFY <code>' to check a visitor pass"
                else:
                    try:
                        code = message_body.strip().upper().split()[1]
                        verification = verify_code_admin(code)
                        response = verification["message"]
                    except IndexError:
                        response = "❌ Invalid format. Please use: VERIFY <code>"
            else:
                # Normal user interaction
                response = generate_response(message_body, wa_id, name)
        else:
            # Handle non-text messages
            response = "Please send text messages only for visitor registration."
            
        # Send response back to the sender
        data = get_text_message_input(wa_id, response)
        send_message(data)
        
    except Exception as e:
        logging.error(f"Error processing WhatsApp message: {e}")
        # Send error response if needed
        if "wa_id" in locals():
            error_msg = "Sorry, we encountered an error processing your request. Please try again."
            data = get_text_message_input(wa_id, error_msg)
            send_message(data)

def verify_code_admin(code):
    """Special verification function for admin with more detailed responses"""
    if not code:
        return {"valid": False, "message": "❌ No code provided"}
    
    code_data = get_code(code)
    if not code_data:
        return {"valid": False, "message": "❌ Invalid code: " + code}
    
    resident_data = get_resident(code_data["wa_id"]) or {}
    now = datetime.now()
    
    if code_data["used"]:
        return {
            "valid": False,
            "message": (
                f"⚠️ Code already used\n"
                f"Resident: {resident_data.get('name', 'N/A')}\n"
                f"Address: {resident_data.get('house_number', '')} {resident_data.get('street_name', '')}\n"
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
                f"Resident: {resident_data.get('name', 'N/A')}\n"
                f"Address: {resident_data.get('house_number', '')} {resident_data.get('street_name', '')}\n"
                f"Visitor: {code_data['name']}\n"
                f"Date: {code_data['date']}\n"
                f"Expired at: {code_data['expiry'].strftime('%Y-%m-%d %H:%M')}"
            )
        }
    
    # Mark code as used and record verification time
    code_data["used"] = True
    code_data["verified_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    update_code(code, code_data)
    
    return {
        "valid": True,
        "message": (
            f"✅ Access granted\n"
            f"Resident: {resident_data.get('name', 'N/A')}\n"
            f"Address: {resident_data.get('house_number', '')} {resident_data.get('street_name', '')}\n"
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
