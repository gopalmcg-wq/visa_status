"""
CEAC Visa Status Checker — Single Run
Designed for GitHub Actions cron. Runs once, checks status,
sends phone alert if changed, then exits.
"""

import os
import sys
import time
import json
import base64
import logging
from datetime import datetime, timezone, timedelta

import requests as http_requests

# IST = UTC + 5:30
IST = timezone(timedelta(hours=5, minutes=30))

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

try:
    from twocaptcha import TwoCaptcha
    HAS_2CAPTCHA = True
except ImportError:
    HAS_2CAPTCHA = False

# ── Config from environment variables (set in GitHub Secrets) ──
CASE_NUMBER = os.environ["CEAC_CASE_NUMBER"]
VISA_TYPE = os.getenv("CEAC_VISA_TYPE", "NIV")
LOCATION = os.getenv("CEAC_LOCATION", "CHENNAI")
PASSPORT_NUMBER = os.environ.get("CEAC_PASSPORT", "")
SURNAME = os.environ.get("CEAC_SURNAME", "")

TWO_CAPTCHA_API_KEY = os.environ.get("TWO_CAPTCHA_API_KEY", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

CEAC_URL = "https://ceac.state.gov/ceacstattracker/status.aspx"
STATUS_FILE = "last_status.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def create_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    # GitHub Actions has Chrome pre-installed
    opts.binary_location = os.getenv("CHROME_BIN", "/usr/bin/google-chrome")
    service = Service(os.getenv("CHROMEDRIVER_BIN", "/usr/bin/chromedriver"))
    return webdriver.Chrome(service=service, options=opts)


def solve_captcha(driver) -> bool:
    if not HAS_2CAPTCHA or not TWO_CAPTCHA_API_KEY:
        log.error("2Captcha not configured — can't solve CAPTCHA in CI")
        return False

    solver = TwoCaptcha(TWO_CAPTCHA_API_KEY)

    try:
        # reCAPTCHA v2
        recaptcha_frames = driver.find_elements(By.CSS_SELECTOR, 'iframe[src*="recaptcha"]')
        if recaptcha_frames:
            log.info("Detected reCAPTCHA — solving via 2Captcha...")
            sitekey = None
            div = driver.find_elements(By.CSS_SELECTOR, '.g-recaptcha')
            if div:
                sitekey = div[0].get_attribute("data-sitekey")
            if not sitekey:
                src = recaptcha_frames[0].get_attribute("src")
                if "k=" in src:
                    sitekey = src.split("k=")[1].split("&")[0]
            if not sitekey:
                log.error("No sitekey found")
                return False

            result = solver.recaptcha(sitekey=sitekey, url=driver.current_url)
            token = result["code"]
            driver.execute_script(
                f'document.getElementById("g-recaptcha-response").innerHTML = "{token}";'
            )
            driver.execute_script(
                "try{___grecaptcha_cfg.clients[0].aa.l.callback(arguments[0])}catch(e){}", token
            )
            log.info("✅ reCAPTCHA solved")
            return True

        # Image CAPTCHA
        imgs = driver.find_elements(By.CSS_SELECTOR, "img[id*='captcha' i], img[id*='Captcha' i]")
        if imgs:
            log.info("Detected image CAPTCHA — solving...")
            b64 = base64.b64encode(imgs[0].screenshot_as_png).decode()
            result = solver.normal(b64)
            inp = driver.find_element(By.CSS_SELECTOR, "input[id*='captcha' i], input[name*='captcha' i]")
            inp.clear()
            inp.send_keys(result["code"])
            log.info("✅ Image CAPTCHA solved")
            return True

        log.info("No CAPTCHA detected")
        return True

    except Exception as e:
        log.error(f"CAPTCHA error: {e}")
        return False


def check_status() -> str | None:
    driver = None
    try:
        driver = create_driver()
        driver.get(CEAC_URL)
        wait = WebDriverWait(driver, 20)

        # Visa type
        rid = ("ctl00_ContentPlaceHolder1_Visa_Application_Type_rbl_0"
               if VISA_TYPE == "NIV" else
               "ctl00_ContentPlaceHolder1_Visa_Application_Type_rbl_1")
        wait.until(EC.element_to_be_clickable((By.ID, rid))).click()
        time.sleep(1)

        # Location
        dd = wait.until(EC.presence_of_element_located(
            (By.ID, "ctl00_ContentPlaceHolder1_Location_DropDownList")))
        sel = Select(dd)
        for opt in sel.options:
            if LOCATION.upper() in opt.text.upper():
                sel.select_by_visible_text(opt.text)
                break
        time.sleep(1)

        # Case number
        ci = wait.until(EC.presence_of_element_located(
            (By.ID, "ctl00_ContentPlaceHolder1_Ession_Case_Number")))
        ci.clear()
        ci.send_keys(CASE_NUMBER)

        # Passport (if field exists)
        if PASSPORT_NUMBER:
            try:
                pi = driver.find_element(By.CSS_SELECTOR, "input[id*='Passport'], input[id*='passport']")
                pi.clear()
                pi.send_keys(PASSPORT_NUMBER)
            except Exception:
                pass

        # Surname (if field exists)
        if SURNAME:
            try:
                si = driver.find_element(By.CSS_SELECTOR, "input[id*='Surname'], input[id*='surname'], input[id*='Last']")
                si.clear()
                si.send_keys(SURNAME)
            except Exception:
                pass

        time.sleep(1)

        # CAPTCHA
        if not solve_captcha(driver):
            return None

        # Submit
        wait.until(EC.element_to_be_clickable(
            (By.ID, "ctl00_ContentPlaceHolder1_btnSubmit"))).click()

        # Get status
        el = wait.until(EC.presence_of_element_located(
            (By.ID, "ctl00_ContentPlaceHolder1_ucApplicationStatusView_lblStatus")))
        return el.text.strip()

    except Exception as e:
        log.error(f"Check failed: {e}")
        return None
    finally:
        if driver:
            driver.quit()


def send_phone_alert(title: str, message: str, priority: str = "high"):
    if not NTFY_TOPIC:
        log.warning("NTFY_TOPIC not set — skipping phone alert")
        return
    try:
        http_requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            headers={"Title": title, "Priority": priority, "Tags": "visa"},
            data=message.encode("utf-8"),
            timeout=10,
        )
        log.info(f"📱 Sent: {title}")
    except Exception as e:
        log.warning(f"Phone alert failed: {e}")


def load_last_status() -> str | None:
    try:
        with open(STATUS_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def save_status(status: str):
    with open(STATUS_FILE, "w") as f:
        f.write(status)


def main():
    # ── Embassy hours check: Mon–Fri, 7:30 AM – 5:00 PM IST ──
    now_ist = datetime.now(IST)
    weekday = now_ist.weekday()  # 0=Mon, 6=Sun
    current_time = now_ist.time()
    open_time = now_ist.replace(hour=7, minute=30, second=0).time()
    close_time = now_ist.replace(hour=17, minute=0, second=0).time()

    if weekday >= 5:  # Saturday or Sunday
        log.info(f"🛑 Weekend in India ({now_ist.strftime('%A %I:%M %p IST')}). Skipping.")
        sys.exit(0)

    if current_time < open_time or current_time > close_time:
        log.info(f"🛑 Outside embassy hours ({now_ist.strftime('%I:%M %p IST')}). Skipping.")
        sys.exit(0)

    log.info(f"⏰ {now_ist.strftime('%A %I:%M %p IST')} — within embassy hours, checking...")
    log.info(f"Checking CEAC status for {CASE_NUMBER} at {LOCATION}...")

    status = check_status()

    if status is None:
        log.error("❌ Could not retrieve status")
        send_phone_alert("CEAC Check Failed", "Could not retrieve status — check logs", priority="default")
        sys.exit(1)

    last_status = load_last_status()
    save_status(status)

    if last_status is None:
        log.info(f"✅ First check. Status: {status}")
        send_phone_alert("CEAC Checker Running", f"Status: {status}\nCase: {CASE_NUMBER}")
    elif status != last_status:
        log.info(f"🚨 CHANGED: '{last_status}' → '{status}'")
        send_phone_alert(
            "🚨 VISA STATUS CHANGED!",
            f"Old: {last_status}\nNew: {status}\nCase: {CASE_NUMBER}",
            priority="urgent",
        )
    else:
        log.info(f"No change. Status: {status}")


if __name__ == "__main__":
    main()
