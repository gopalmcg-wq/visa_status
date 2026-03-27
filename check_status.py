"""
CEAC Visa Status Checker — GitHub Actions version with full debug logging.
"""

import os
import sys
import time
import base64
import logging
from datetime import datetime, timezone, timedelta

import requests as http_requests

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

# IST = UTC + 5:30
IST = timezone(timedelta(hours=5, minutes=30))

# Config from env vars
CASE_NUMBER = os.environ.get("CEAC_CASE_NUMBER", "")
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
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--single-process")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    for chrome_path in [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]:
        if os.path.exists(chrome_path):
            opts.binary_location = chrome_path
            log.info(f"Chrome binary: {chrome_path}")
            break

    for driver_path in [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ]:
        if os.path.exists(driver_path):
            log.info(f"ChromeDriver: {driver_path}")
            service = Service(driver_path)
            d = webdriver.Chrome(service=service, options=opts)
            d.set_page_load_timeout(30)
            d.set_script_timeout(10)
            return d

    log.info("Using default ChromeDriver lookup")
    d = webdriver.Chrome(options=opts)
    d.set_page_load_timeout(30)
    d.set_script_timeout(10)
    return d


def solve_captcha(driver) -> bool:
    if not HAS_2CAPTCHA or not TWO_CAPTCHA_API_KEY:
        log.warning("2Captcha not configured — skipping CAPTCHA")
        return False

    solver = TwoCaptcha(TWO_CAPTCHA_API_KEY)
    try:
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
            log.info("reCAPTCHA solved")
            return True

        imgs = driver.find_elements(By.CSS_SELECTOR, "img[id*='captcha' i], img[id*='Captcha' i]")
        if imgs:
            log.info("Detected image CAPTCHA — solving...")
            b64 = base64.b64encode(imgs[0].screenshot_as_png).decode()
            result = solver.normal(b64)
            inp = driver.find_element(By.CSS_SELECTOR, "input[id*='captcha' i]")
            inp.clear()
            inp.send_keys(result["code"])
            log.info("Image CAPTCHA solved")
            return True

        log.info("No CAPTCHA detected")
        return True
    except Exception as e:
        log.error(f"CAPTCHA error: {e}")
        return False


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
        log.info(f"Phone alert sent: {title}")
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


def check_status() -> str | None:
    driver = None
    try:
        log.info("Creating Chrome driver...")
        driver = create_driver()
        log.info("Chrome launched")

        log.info(f"Loading {CEAC_URL}")
        driver.get(CEAC_URL)
        log.info(f"Page title: {driver.title}")
        time.sleep(5)
        log.info("Initial wait done")

        # ── DEBUG: dump page info ──
        try:
            log.info(f"URL: {driver.current_url}")
        except Exception as e:
            log.error(f"URL error: {e}")

        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            log.info(f"Iframes: {len(iframes)}")
        except Exception as e:
            log.error(f"Iframe error: {e}")

        try:
            elements = driver.find_elements(By.CSS_SELECTOR, "input, select, button")
            log.info(f"Form elements: {len(elements)}")
            for el in elements:
                eid = el.get_attribute("id") or ""
                etype = el.get_attribute("type") or el.tag_name
                if eid:
                    log.info(f"  [{etype}] {eid}")
        except Exception as e:
            log.error(f"Element scan error: {e}")

        try:
            src = driver.page_source[:2000]
            log.info(f"HTML preview:\n{src}")
        except Exception as e:
            log.error(f"Page source error: {e}")

        # ── STEP 1: Select visa type ──
        log.info("STEP 1: Clicking visa type radio...")
        wait = WebDriverWait(driver, 15)
        rid = ("ctl00_ContentPlaceHolder1_Visa_Application_Type_rbl_0"
               if VISA_TYPE == "NIV" else
               "ctl00_ContentPlaceHolder1_Visa_Application_Type_rbl_1")
        try:
            radio = wait.until(EC.element_to_be_clickable((By.ID, rid)))
            radio.click()
            log.info("Clicked! Waiting for postback...")
            time.sleep(5)
            log.info(f"After postback — title: {driver.title}")
        except Exception as e:
            log.error(f"STEP 1 FAILED: {e}")
            try:
                radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")
                log.info(f"Radio buttons found: {len(radios)}")
                for r in radios:
                    log.info(f"  id='{r.get_attribute('id')}'")
            except:
                pass
            return None

        # Re-scan elements after postback
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, "input, select, button")
            log.info(f"After postback — {len(elements)} elements:")
            for el in elements:
                eid = el.get_attribute("id") or ""
                etype = el.get_attribute("type") or el.tag_name
                if eid:
                    log.info(f"  [{etype}] {eid}")
        except Exception as e:
            log.error(f"Post-postback scan error: {e}")

        # ── STEP 2: Select location ──
        log.info("STEP 2: Selecting location...")
        try:
            dd = wait.until(EC.presence_of_element_located(
                (By.ID, "ctl00_ContentPlaceHolder1_Location_DropDownList")))
            sel = Select(dd)
            log.info(f"Dropdown: {len(sel.options)} options")
            for i, opt in enumerate(sel.options):
                log.info(f"  [{i}] '{opt.text}'")
            matched = False
            for opt in sel.options:
                if LOCATION.upper() in opt.text.upper():
                    sel.select_by_visible_text(opt.text)
                    log.info(f"Matched: '{opt.text}'")
                    matched = True
                    break
            if not matched:
                log.error(f"'{LOCATION}' not in dropdown")
                return None
        except Exception as e:
            log.error(f"STEP 2 FAILED: {e}")
            try:
                selects = driver.find_elements(By.TAG_NAME, "select")
                log.info(f"Selects found: {len(selects)}")
                for s in selects:
                    log.info(f"  id='{s.get_attribute('id')}'")
            except:
                pass
            return None
        time.sleep(2)

        # ── STEP 3: Case number ──
        log.info("STEP 3: Entering case number...")
        try:
            ci = wait.until(EC.presence_of_element_located(
                (By.ID, "ctl00_ContentPlaceHolder1_Ession_Case_Number")))
            ci.clear()
            ci.send_keys(CASE_NUMBER)
            log.info("Case number entered")
        except Exception as e:
            log.error(f"STEP 3 FAILED: {e}")
            return None

        # ── STEP 4: Passport ──
        log.info("STEP 4: Passport...")
        if PASSPORT_NUMBER:
            try:
                pi = driver.find_element(By.CSS_SELECTOR, "input[id*='Passport'], input[id*='passport']")
                pi.clear()
                pi.send_keys(PASSPORT_NUMBER)
                log.info(f"Passport entered (id: {pi.get_attribute('id')})")
            except:
                log.info("No passport field found")

        # ── STEP 5: Surname ──
        log.info("STEP 5: Surname...")
        if SURNAME:
            try:
                si = driver.find_element(By.CSS_SELECTOR, "input[id*='Surname'], input[id*='surname'], input[id*='Last']")
                si.clear()
                si.send_keys(SURNAME)
                log.info(f"Surname entered (id: {si.get_attribute('id')})")
            except:
                log.info("No surname field found")

        time.sleep(1)

        # ── STEP 6: CAPTCHA ──
        log.info("STEP 6: CAPTCHA...")
        if not solve_captcha(driver):
            log.error("CAPTCHA not solved")
            return None

        # ── STEP 7: Submit ──
        log.info("STEP 7: Submit...")
        try:
            btn = wait.until(EC.element_to_be_clickable(
                (By.ID, "ctl00_ContentPlaceHolder1_btnSubmit")))
            btn.click()
            log.info("Submitted!")
        except Exception as e:
            log.error(f"STEP 7 FAILED: {e}")
            return None

        # ── STEP 8: Read status ──
        log.info("STEP 8: Reading status...")
        try:
            el = wait.until(EC.presence_of_element_located(
                (By.ID, "ctl00_ContentPlaceHolder1_ucApplicationStatusView_lblStatus")))
            status = el.text.strip()
            log.info(f"STATUS: {status}")
            return status
        except Exception as e:
            log.error(f"STEP 8 FAILED: {e}")
            try:
                src = driver.page_source[:2000]
                log.info(f"Page after submit:\n{src}")
            except:
                pass
            return None

    except Exception as e:
        log.error(f"Check failed: {e}")
        if driver:
            try:
                src = driver.page_source[:2000]
                log.info(f"Error page source:\n{src}")
            except:
                pass
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass


def main():
    now_ist = datetime.now(IST)
    weekday = now_ist.weekday()
    current_time = now_ist.time()
    open_time = now_ist.replace(hour=7, minute=30, second=0).time()
    close_time = now_ist.replace(hour=17, minute=0, second=0).time()

    if weekday >= 5:
        log.info(f"Weekend ({now_ist.strftime('%A %I:%M %p IST')}). Skipping.")
        sys.exit(0)

    if current_time < open_time or current_time > close_time:
        log.info(f"Outside hours ({now_ist.strftime('%I:%M %p IST')}). Skipping.")
        sys.exit(0)

    log.info(f"{now_ist.strftime('%A %I:%M %p IST')} — checking...")
    log.info(f"Case: {CASE_NUMBER}, Location: {LOCATION}")

    status = check_status()

    if status is None:
        log.error("Could not retrieve status")
        send_phone_alert("CEAC Check Failed", "Could not retrieve status", priority="default")
        sys.exit(1)

    last_status = load_last_status()
    save_status(status)

    if last_status is None:
        log.info(f"First check. Status: {status}")
        send_phone_alert("CEAC Checker Running", f"Status: {status}")
    elif status != last_status:
        log.info(f"CHANGED: '{last_status}' -> '{status}'")
        send_phone_alert("VISA STATUS CHANGED!", f"Old: {last_status}\nNew: {status}", priority="urgent")
    else:
        log.info(f"No change. Status: {status}")


if __name__ == "__main__":
    main()
