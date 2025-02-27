from flask import Flask, render_template, request, jsonify
import logging
from datetime import datetime, timedelta
import re
import requests
import pytz
import os
from urllib.parse import quote as url_quote
from dotenv import load_dotenv
import time

###############################################################################
# TOGGLE VERBOSE LOGGING
###############################################################################
VERBOSE_LOGS = True  # Set to False to silence extra logs

load_dotenv()

app = Flask(__name__)

# Google Apps Script URL
SCRIPT_URL = os.getenv('GOOGLE_SCRIPT_URL')
if not SCRIPT_URL:
    raise ValueError("GOOGLE_SCRIPT_URL environment variable is not set")

reset = True

def setup_logging():
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger = logging.getLogger()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

setup_logging()
logger = logging.getLogger(__name__)

def current_time_pst():
    tz = pytz.timezone('America/Los_Angeles')
    return datetime.now(tz)

def normalize_phone(phone):
    """
    Normalizes phone by stripping non-digits,
    removing leading '1' if 11 digits, else requiring exactly 10 digits.
    """
    if not phone:
        return None
    stripped = re.sub(r"[^\d]", "", phone)
    if len(stripped) == 10:
        return stripped
    if len(stripped) == 11 and stripped.startswith("1"):
        return stripped[1:]
    return None

def convert_to_12_hour(time_str):
    if not time_str or ":" not in time_str:
        return time_str
    parts = time_str.split(":")
    hours = int(parts[0])
    minutes = int(parts[1]) if len(parts) > 1 else 0
    am_pm = "AM" if hours < 12 else "PM"
    hours = hours % 12 or 12
    return f"{hours}:{minutes:02d} {am_pm}"

def format_date(yyyy_mm_dd):
    """
    e.g. "2025-01-17" => "January 17, 2025"
    """
    try:
        dt = datetime.strptime(yyyy_mm_dd, "%Y-%m-%d")
        return dt.strftime("%B %d, %Y")
    except Exception:
        return yyyy_mm_dd

###############################################################################
# LANDING PAGE
###############################################################################
@app.route("/")
def index():
    try:
        now = current_time_pst()
        logger.info(f"[INDEX] Loading at {now} PST")

        resp = requests.get(f"{SCRIPT_URL}?marketInfo=true")
        if resp.status_code != 200:
            logger.error(f"[INDEX] Market info fetch failed. Status: {resp.status_code}")
            return render_template("index.html", error="Unable to fetch market info.", reset=reset)

        try:
            data = resp.json()
        except Exception as e:
            logger.error(f"[INDEX] JSON parse failed: {resp.text}")
            return render_template("index.html", error="Market info format error.", reset=reset)

        if VERBOSE_LOGS:
            logger.debug(f"[INDEX] market info data: {data}")

        is_open = data.get("isOpen", False)
        current_market = data.get("currentMarket")
        next_market = data.get("nextMarket")

        # If no info at all
        if not current_market and not next_market:
            is_open = False
            current_market = None
            next_market = None

        # next_market_str for display
        next_market_str = ""
        if next_market:
            nm_date = format_date(next_market["date"])
            nm_time = convert_to_12_hour(next_market["startTime"])
            next_market_str = f"{nm_date} at {nm_time} PST"

        return render_template(
            "index.html",
            error=None,
            is_open=is_open,
            next_market_str=next_market_str,
            next_market=next_market,
            current_market=current_market,
            check_in_start=(
                convert_to_12_hour(current_market["checkInStart"].split()[-1])
                if current_market else ""
            ),
            check_in_end=(
                convert_to_12_hour(current_market["checkInEnd"].split()[-1])
                if current_market else ""
            ),
            reset=reset
        )

    except Exception as e:
        logger.exception("[INDEX] Exception:")
        return render_template("index.html", error=str(e), reset=reset)

###############################################################################
# CHECK-IN ROUTE
###############################################################################
@app.route("/checkin", methods=["POST"])
def check_in():
    data = request.get_json() or {}
    raw_phone = data.get("phone", "")
    phone = normalize_phone(raw_phone)
    logger.info(f"[CHECKIN] Attempting check-in with raw phone: %s, normalized: %s", raw_phone, phone)

    if not phone:
        return jsonify({"error": "invalidFormat"})

    now = current_time_pst()

    try:
        # 1) Validate phone in Master List
        val_payload = {"validatePhone": phone}
        val_resp = requests.post(SCRIPT_URL, json=val_payload, timeout=25)
        val_json = val_resp.json()
        if VERBOSE_LOGS:
            logger.debug(f"[CHECKIN] Validation response: %s", val_json)

        if val_json.get("error") == "userNotRegistered":
            return jsonify({"error": "userNotRegistered"})
        elif "error" in val_json:
            return jsonify({"error": val_json["error"]})

        # 2) Market info
        market_resp = requests.get(f"{SCRIPT_URL}?marketInfo=true", timeout=25)
        market_data = market_resp.json()
        if VERBOSE_LOGS:
            logger.debug(f"[CHECKIN] Market info: %s", market_data)

        current_market = market_data.get("currentMarket")
        next_market = market_data.get("nextMarket")

        if not current_market and not next_market:
            nextDateStr = ""
            if next_market:
                nextDateStr = f"The next market is on {format_date(next_market['date'])}"
            else:
                nextDateStr = "No Future Market Date Found"
            return jsonify({"error": "noMarket", "nextMarketString": nextDateStr})

        if not current_market:
            nextDateStr = ""
            if next_market:
                nextDateStr = f"The next market is on {format_date(next_market['date'])}"
            else:
                nextDateStr = "No Future Market Date Found"
            return jsonify({"error": "noMarket", "nextMarketString": nextDateStr})

        cstart_str = current_market["checkInStart"]
        cend_str   = current_market["checkInEnd"]
        dt_fmt = "%Y-%m-%d %H:%M:%S"
        pst_zone = pytz.timezone("America/Los_Angeles")

        check_in_start = pst_zone.localize(datetime.strptime(cstart_str, dt_fmt))
        check_in_end   = pst_zone.localize(datetime.strptime(cend_str, dt_fmt))

        if now < check_in_start:
            friendly_start = convert_to_12_hour(cstart_str.split(" ")[1])
            return jsonify({"error": "checkInNotStarted","startTime": friendly_start})

        if now > check_in_end:
            return jsonify({"error": "marketEnded"})

        # 3) Attempt check-in
        c_payload = {"input": phone}
        checkin_resp = requests.post(SCRIPT_URL, json=c_payload, timeout=25)
        c_data = checkin_resp.json()
        if VERBOSE_LOGS:
            logger.debug(f"[CHECKIN] checkInSheet response: %s", c_data)

        if "error" in c_data:
            if c_data["error"] == "alreadyCheckedIn":
                return jsonify({
                    "success": "You are already checked in. Continuing to wait for your NIL.",
                    "color": current_market["color"],
                    "marketDate": current_market["date"]
                })
            else:
                return jsonify({"error": c_data["error"]})

        return jsonify({
            "success": "Check-in successful. Please wait for your NIL number.",
            "color": current_market["color"],
            "marketDate": current_market["date"]
        })

    except requests.exceptions.Timeout:
        logger.exception("[CHECKIN] Timed out after ~25s in the server.")
        return jsonify({"error": "internalError"})
    except Exception as e:
        logger.exception("[CHECKIN] Exception:")
        return jsonify({"error": "internalError"})

###############################################################################
# VALIDATE ROUTE
###############################################################################
@app.route("/validate", methods=["POST"])
def validate():
    data = request.get_json() or {}
    raw_phone = data.get("phone", "")
    phone = normalize_phone(raw_phone)
    if not phone:
        return jsonify({"error": "invalidFormat"})

    try:
        payload = {"validatePhone": phone}
        v_resp = requests.post(SCRIPT_URL, json=payload, timeout=25)
        v_data = v_resp.json()
        if VERBOSE_LOGS:
            logger.debug(f"[VALIDATE] response from Apps Script: %s", v_data)

        if "error" in v_data:
            return jsonify({"error": v_data["error"]})
        else:
            # If there's no error => phone found in Master
            return jsonify({"success": True})
    except requests.exceptions.Timeout:
        logger.exception("[VALIDATE] Timed out after ~25s in the server.")
        return jsonify({"error": "internalError"})
    except Exception as e:
        logger.exception("[VALIDATE] Exception:")
        return jsonify({"error": "internalError"})

###############################################################################
# FETCH NIL ROUTE
###############################################################################
@app.route("/nil", methods=["POST"])
def get_nil():
    data = request.get_json() or {}
    raw_phone = data.get("phone", "")
    phone = normalize_phone(raw_phone)
    if not phone:
        return jsonify({"error": "invalidFormat"})

    try:
        payload = {"phone": phone}
        n_resp = requests.post(SCRIPT_URL, json=payload, timeout=25)
        if n_resp.status_code != 200:
            return jsonify({"error": "internalError"})

        r_data = n_resp.json()
        if VERBOSE_LOGS:
            logger.debug(f"[NIL] handleNILRetrieval response: %s", r_data)

        if "NIL" in r_data:
            return jsonify({
                "NIL": r_data["NIL"],
                "firstName": r_data.get("firstName", "")
            })
        elif "error" in r_data:
            resp = {"error": r_data["error"]}
            if "firstName" in r_data:
                resp["firstName"] = r_data["firstName"]
            return jsonify(resp)
        else:
            return jsonify({"error": "internalError"})
    except requests.exceptions.Timeout:
        logger.exception("[NIL] Timed out after ~25s in the server.")
        return jsonify({"error": "internalError"})
    except Exception as e:
        logger.exception("[NIL] Exception:")
        return jsonify({"error": "internalError"})

###############################################################################
# SIGNUP PAGE (GET)
###############################################################################
@app.route("/signup", methods=["GET"])
def signup_page():
    return render_template("signup.html")

###############################################################################
# SIGNUP PROCESS (POST)
###############################################################################
@app.route("/signup", methods=["POST"])
def process_signup():
    data = request.get_json() or {}
    fname = data.get("firstName", "").strip()
    lname = data.get("lastName", "").strip()
    email = data.get("email", "").strip()
    primary_phone = data.get("primaryPhone", "").strip()  # new key name
    secondary_phone = data.get("secondaryPhone", "").strip()  # new key name

    phone = normalize_phone(primary_phone)
    if not phone or not fname or not lname or not email:
        return jsonify({"error": "Please provide all required fields."})

    # Check if user already exists
    try:
        add_resp = requests.post(
            SCRIPT_URL,
            json={"addMasterList": {
                "firstName": fname,
                "lastName": lname,
                "email": email,
                "primaryPhone": phone
            }},
            timeout=10
        )
        add_json = add_resp.json()
        if add_json.get("error"):
            return jsonify({"error": add_json["error"]})
    except Exception as e:
        logger.exception("[SIGNUP] existing check error:")
        return jsonify({"error": "Unable to verify registration status."})

    if not add_json.get("success"):
        return jsonify({"error": "Unable to complete registration."})

    # Handle group assignment.
    if not secondary_phone:
        try:
            grp_resp = requests.post(
                SCRIPT_URL,
                json={"updateGroup": {"primaryPhone": phone}},  # no secondaryPhone key sent
                timeout=15
            )
            grp_json = grp_resp.json()
            if grp_json.get("error"):
                rollback_payload = {"removeMasterList": {"primaryPhone": phone}}
                requests.post(SCRIPT_URL, json=rollback_payload, timeout=10)
                return jsonify({"error": grp_json["error"]})
        except Exception as e:
            logger.exception("[SIGNUP] group assignment error:")
            rollback_payload = {"removeMasterList": {"primaryPhone": phone}}
            requests.post(SCRIPT_URL, json=rollback_payload, timeout=10)
            return jsonify({"error": "Unable to complete registration."})
    else:
        success = True
        for sp in [x.strip() for x in secondary_phone.split(",") if x.strip()]:
            sp_norm = normalize_phone(sp)
            if sp_norm:
                try:
                    group_resp = requests.post(
                        SCRIPT_URL,
                        json={"updateGroup": {
                            "primaryPhone": phone,
                            "secondaryPhone": sp_norm
                        }},
                        timeout=15
                    )
                    if group_resp.json().get("error"):
                        success = False
                        break
                except Exception:
                    success = False
                    break
        if not success:
            rollback_payload = {"removeMasterList": {"primaryPhone": phone}}
            requests.post(SCRIPT_URL, json=rollback_payload, timeout=10)
            return jsonify({"error": "Unable to complete group registration."})

    return jsonify({"success": True})


###############################################################################
# MAIN
###############################################################################
if __name__ == "__main__":
    logger.info("Starting Flask application.")
    app.run(debug=True)
