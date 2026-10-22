#!/usr/bin/env python3
"""Watch ICBC road test slots at Burnaby Claims Centre (4399 Wayburne Dr) and book
the best one in the target window, upgrading while changes are still free.

API (from the webdeas-ui bundle, https://onlinebusiness.icbc.com/deas-api/v1):
  PUT  /webLogin/webLogin            login; token comes back in the Authorization header
  PUT  /web/getNearestPos            test centres near a lat/lng
  POST /web/getAvailableAppointments open slots at one centre
  PUT  /web/lock                     hold a slot (empty payload = release)
  POST /web/sendOTP + PUT /web/verifyOTP   one-time code, required for every booking
  PUT  /web/book | /web/rebook | /web/cancel

Every booking needs the emailed one-time code. If GMAIL_ADDRESS + GMAIL_APP_PASSWORD
are set the code is read over IMAP; you can always type it into the terminal too.

ICBC charges $25 to cancel or reschedule within 48 hours of the test, so an existing
booking is only ever upgraded while it is more than 48 h (+ --fee-margin) away.

Slot tiers (higher is better, 0 = never book):
  4  Mon-Fri 10:00-13:30   midday: after rush hour and school drop-off
  3  Mon-Fri 09:30-10:00, 13:30-14:15
  2  Mon-Fri before 09:30  morning rush + school drop-off
  1  Sat/Sun
  0  Mon-Fri after 14:15   school dismissal, then evening rush

Personal info comes from env vars (or .env next to this file):
  ICBC_LAST_NAME, ICBC_LICENCE, ICBC_KEYWORD, optional GMAIL_ADDRESS,
  GMAIL_APP_PASSWORD, NTFY_TOPIC.
"""
import argparse
import datetime as dt
import email
import email.utils
import imaplib
import json
import os
import random
import re
import select
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API = "https://onlinebusiness.icbc.com/deas-api/v1"
TZ = ZoneInfo("America/Vancouver")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HERE = Path(__file__).resolve().parent
DEBUG_DIR = HERE / "icbc_debug"
CENTRE_LATLNG = (49.2622, -123.0005)  # around 4399 Wayburne Dr, Burnaby
UI_ONLY_FIELDS = ("numericalTime", "selected", "apptDate", "apptDateAndTime", "apptTime")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
DONE = 10  # exit code: nothing left to do
T = dt.time


def log(msg):
    print(f"[{dt.datetime.now(TZ):%m-%d %H:%M:%S}] {msg}", flush=True)


def save(name, data):
    DEBUG_DIR.mkdir(exist_ok=True)
    path = DEBUG_DIR / f"{dt.datetime.now(TZ):%m%d-%H%M%S}-{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def notify(title, message, urgent=False):
    try:  # macOS banner + sound
        subprocess.run(["osascript", "-e", f"display notification {json.dumps(message)} "
                        f"with title {json.dumps(title)} sound name \"Glass\""], timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        try:
            requests.post("https://ntfy.sh/", timeout=15, json={
                "topic": topic, "title": title, "message": message,
                "priority": 5 if urgent else 4, "tags": ["rotating_light" if urgent else "car"]})
        except requests.RequestException as e:
            log(f"push failed: {e}")


def tier(when):
    if when.weekday() >= 5:
        return 1
    t = when.time()
    if T(10, 0) <= t < T(13, 30):
        return 4
    if T(9, 30) <= t < T(10, 0) or T(13, 30) <= t <= T(14, 15):
        return 3
    if t < T(9, 30):
        return 2
    return 0


def parse_windows(spec):
    """'23:58-00:06=8,22:00-23:58=20' -> [(start, end, seconds)]; windows may wrap midnight."""
    out = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        span, secs = part.split("=")
        a, b = span.split("-")
        out.append((dt.time.fromisoformat(a), dt.time.fromisoformat(b), float(secs)))
    return out


def poll_seconds(now, windows, default):
    t = now.time()
    for a, b, secs in windows:
        if (a <= t < b) if a <= b else (t >= a or t < b):
            return secs
    return default


class Slot:
    def __init__(self, raw):
        self.raw = raw
        self.when = dt.datetime.combine(dt.date.fromisoformat(raw["appointmentDt"]["date"]),
                                        dt.time.fromisoformat(raw["startTm"]), TZ)
        self.tier = tier(self.when)

    def __repr__(self):
        return f"{self.when:%a %m-%d %H:%M}(t{self.tier})"


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


class Icbc:
    def __init__(self, last_name, licence, keyword, session=None):
        self.creds = {"drvrLastName": last_name, "licenceNumber": licence, "keyword": keyword}
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": UA, "Content-Type": "application/json",
            "Origin": "https://onlinebusiness.icbc.com",
            "Referer": "https://onlinebusiness.icbc.com/webdeas-ui/booking"})
        self.user = None
        self.logged_in_at = 0.0

    def call(self, method, path, body):
        r = self.session.request(method, API + path, json=body, timeout=30)
        if r.status_code >= 400:
            raise ApiError(r.status_code, r.text)
        return r.json() if r.content else None

    def login(self):
        self.session.headers.pop("Authorization", None)
        r = self.session.put(API + "/webLogin/webLogin", json=self.creds, timeout=30,
                             headers={"Cache-control": "no-cache, no-store", "pragma": "no-cache"})
        if r.status_code == 403:
            # ICBC checks the network only after the credentials pass; the web UI shows "cannot be used
            # outside Canada or the United States". Cloud IPs (GitHub Actions) get this. Retrying won't help.
            raise SystemExit("ICBC refused login from this network (HTTP 403). Run from a home connection.")
        if r.status_code == 400:
            raise SystemExit(f"ICBC rejected the login details: {r.content.decode('utf-8', 'replace')[:200]}")
        if r.status_code >= 400:
            raise ApiError(r.status_code, r.text)
        token = r.headers.get("Authorization")
        if not token:
            raise RuntimeError("login response had no Authorization header")
        self.session.headers["Authorization"] = token
        self.user = r.json()
        self.logged_in_at = time.monotonic()
        save("login", self.user)
        return self.user

    @property
    def drvr_id(self):
        return self.user["drvrId"]

    def driver(self, **extra):
        u = self.user
        return {"drvrId": u["drvrId"], "lastName": u.get("lastName"),
                "licenseNumber": u.get("licenseNumber"), "phoneNum": u.get("phoneNum"), **extra}

    def nearest_pos(self, exam_code, start_date):
        lat, lng = CENTRE_LATLNG
        return self.call("PUT", "/web/getNearestPos",
                         {"lng": lng, "lat": lat, "examType": exam_code, "startDate": start_date})

    def slots(self, pos_id, exam_code, exam_date):
        raw = self.call("POST", "/web/getAvailableAppointments", {
            "aPosID": pos_id, "examType": exam_code, "examDate": exam_date.isoformat(),
            "prfDaysOfWeek": "[0,1,2,3,4,5,6]", "prfPartsOfDay": "[0,1]",
            "lastName": self.user["lastName"], "licenseNumber": self.user["licenseNumber"]})
        return [Slot(s) for s in raw or []]

    def lock(self, slot, booked_ts):
        s = slot.raw
        return self.call("PUT", "/web/lock", {
            "appointmentDt": dict(s["appointmentDt"]), "dlExam": dict(s["dlExam"]),
            "drvrDriver": {"drvrId": self.drvr_id}, "drscDrvSchl": {}, "instructorDlNum": None,
            "bookedTs": booked_ts, "startTm": s["startTm"], "endTm": s["endTm"],
            "posId": s["posId"], "resourceId": s["resourceId"], "signature": s["signature"]})

    def unlock(self):
        try:
            self.call("PUT", "/web/lock", {"appointmentDt": {}, "dlExam": {},
                                           "drvrDriver": {"drvrId": self.drvr_id}, "drscDrvSchl": {}})
        except Exception as e:
            log(f"unlock failed (harmless): {e}")

    def send_otp(self, booked_ts):
        return self.call("POST", "/web/sendOTP",
                         {"bookedTs": booked_ts, "drvrID": self.drvr_id, "method": "E"})

    def verify_otp(self, booked_ts, code):
        return self.call("PUT", "/web/verifyOTP",
                         {"bookedTs": booked_ts, "drvrID": self.drvr_id, "code": code})

    def book(self):
        return self.call("PUT", "/web/book", {"userId": f"WEBD:{self.drvr_id}",
                                              "appointment": {"drvrDriver": {"drvrId": self.drvr_id}}})

    def rebook(self, existing):
        appt = {k: v for k, v in existing.items() if k not in UI_ONLY_FIELDS}
        appt["drvrDriver"] = self.driver(firstName=self.user.get("firstName"))
        return self.call("PUT", "/web/rebook", {"userId": f"WEBD:{self.drvr_id}",
                                                "appointment": appt, "action": "RE-BOOKED"})

    def cancel(self, existing):
        appt = {k: v for k, v in existing.items() if k not in UI_ONLY_FIELDS}
        appt["drvrDriver"] = self.driver()
        return self.call("PUT", "/web/cancel", {
            "userId": f"WEBD:{self.drvr_id}", "instDlNum": None, "appointment": appt,
            "action": "CANCELLED", "appointmentDt": existing["appointmentDt"],
            "remark": "Cancelled by user action through WebDEAS", "startTm": existing["startTm"]})


class OtpSource:
    """Waits for the one-time code from Gmail (IMAP) or the terminal, whichever comes first."""

    CODE_RES = [re.compile(r"(?i)(?:code|passcode|verification)\D{0,60}?(\d{4,8})\b"),
                re.compile(r"\b(\d{6})\b")]

    def __init__(self, gmail_address="", gmail_password="", ignore=()):
        self.gmail = (gmail_address, gmail_password) if gmail_address and gmail_password else None
        self.ignore = set(ignore)  # numbers that are never the code (e.g. the licence number)
        self.stdin_open = sys.stdin is not None and not sys.stdin.closed and sys.stdin.isatty()

    def wait(self, since, timeout=600):
        how = "Gmail or the terminal" if self.gmail else "the terminal"
        print(f"\n>>> ICBC emailed a one-time code. Waiting for it from {how}."
              f"\n>>> Type the code and press Enter: ", end="", flush=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.gmail:
                try:
                    code = self.from_gmail(since)
                except Exception as e:  # keep the terminal path alive
                    log(f"gmail check failed: {e}")
                    code = None
                if code:
                    print()
                    log(f"got code {code} from Gmail")
                    return code
            if self.stdin_open:
                ready, _, _ = select.select([sys.stdin], [], [], 6)
                if ready:
                    line = sys.stdin.readline()
                    if not line:
                        self.stdin_open = False
                    elif re.fullmatch(r"\d{4,8}", line.strip()):
                        return line.strip()
                    elif line.strip():
                        print(">>> that does not look like a code, try again: ", end="", flush=True)
            else:
                time.sleep(6)
        return None

    def from_gmail(self, since):
        addr, password = self.gmail
        day = since - dt.timedelta(days=1)
        with imaplib.IMAP4_SSL("imap.gmail.com") as m:
            m.login(addr, password)
            for box in ("INBOX", '"[Gmail]/Spam"'):
                if m.select(box, readonly=True)[0] != "OK":
                    continue
                _, data = m.search(None, f'(FROM "icbc" SINCE {day.day}-{MONTHS[day.month - 1]}-{day.year})')
                for num in reversed(data[0].split()[-10:]):
                    _, parts = m.fetch(num, "(RFC822)")
                    msg = email.message_from_bytes(parts[0][1])
                    sent = email.utils.parsedate_to_datetime(msg["Date"])
                    if sent < since - dt.timedelta(seconds=90):
                        continue
                    code = self.extract(f"{msg.get('Subject', '')}\n{body_text(msg)}")
                    if code:
                        return code
        return None

    def check(self):
        """Fail fast on a bad Gmail app password instead of at booking time."""
        with imaplib.IMAP4_SSL("imap.gmail.com") as m:
            m.login(*self.gmail)

    def extract(self, text):
        for rx in self.CODE_RES:
            for m in rx.finditer(text):
                if m.group(1) not in self.ignore:
                    return m.group(1)
        return None


def body_text(msg):
    out = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            out.append(re.sub(r"<[^>]+>", " ", text))
    return " ".join(" ".join(out).split())


def appt_when(appt):
    return dt.datetime.combine(dt.date.fromisoformat(appt["appointmentDt"]["date"]),
                               dt.time.fromisoformat(appt["startTm"]), TZ)


class Grabber:
    def __init__(self, args, icbc, otp):
        self.args, self.icbc, self.otp = args, icbc, otp
        self.exam_code = None
        self.pos_id = args.pos_id

    # ---------- setup ----------
    def setup(self):
        u = self.icbc.login()
        log("logged in")  # no name/ids: Actions logs of a public repo are world-readable
        exams = u.get("eligibleExams") or []
        for e in exams:
            log(f"eligible: {e.get('code')} {e.get('description')} earliest {e.get('eed', {}).get('date')}")
        for a in u.get("webAappointments") or []:
            log(f"existing appointment: {a['dlExam'].get('description')} {appt_when(a):%a %m-%d %H:%M} pos {a.get('posId')}")
        booked_codes = [a["dlExam"]["code"] for a in u.get("webAappointments") or []]
        codes = [e["code"] for e in exams] + booked_codes
        if self.args.exam_code:
            self.exam_code = self.args.exam_code
        elif codes:
            self.exam_code = codes[0]
        else:
            sys.exit("no eligible road test on this licence (eligibleExams is empty)")
        if u.get("cancellationFeeTotal"):
            log(f"WARNING: outstanding cancellation fees {u['cancellationFeeTotal']} may block booking")
        eed = next((e.get("eed", {}).get("date") for e in exams if e["code"] == self.exam_code), None)
        if eed and dt.date.fromisoformat(eed) >= self.args.end.date():
            sys.exit(f"earliest eligible date {eed} is after the window; nothing can be booked")
        if self.pos_id is None:
            self.pos_id = self.find_pos()
        log(f"exam {self.exam_code}, centre posId {self.pos_id}")

    def find_pos(self):
        start = max(dt.date.today() + dt.timedelta(days=1), self.args.start.date()).isoformat()
        found = self.icbc.nearest_pos(self.exam_code, start)
        save("nearest-pos", found)
        for item in found:
            p = item.get("pos", item)
            log(f"centre {p.get('posId')}: {p.get('agency')} | {p.get('address')}")
        for item in found:
            p = item.get("pos", item)
            if "wayburne" in (p.get("address") or "").lower() or (
                    "burnaby" in (p.get("agency") or "").lower() and "claim" in (p.get("agency") or "").lower()):
                return p["posId"]
        sys.exit("Burnaby Claims Centre not found in nearby centres; pass --pos-id from the list above")

    # ---------- state ----------
    def existing(self):
        for a in self.icbc.user.get("webAappointments") or []:
            if a["dlExam"]["code"] == self.exam_code:
                return a
        return None

    def free_to_change(self, appt):
        limit = dt.timedelta(hours=48, minutes=self.args.fee_margin)
        return appt_when(appt) - dt.datetime.now(TZ) > limit

    def refresh_login(self, force=False):
        age = time.monotonic() - self.icbc.logged_in_at
        # fresh token going into the midnight release, so the first poll after 00:00 can book at once
        hot = self.interval() <= 10 and age > 5 * 60
        if force or hot or age > self.args.relogin_minutes * 60:
            self.icbc.login()

    def interval(self):
        return poll_seconds(dt.datetime.now(TZ), parse_windows(self.args.busy), self.args.interval)

    def search(self, from_date):
        try:
            return self.icbc.slots(self.pos_id, self.exam_code, from_date)
        except ApiError as e:
            if e.status in (401, 403):
                log("token expired; logging in again")
                self.refresh_login(force=True)
                return self.icbc.slots(self.pos_id, self.exam_code, from_date)
            raise

    def wanted(self, slots):
        earliest = dt.datetime.now(TZ) + dt.timedelta(minutes=self.args.lead_minutes)
        out = [s for s in slots if self.args.start <= s.when < self.args.end
               and s.when >= earliest and s.tier > 0]
        return sorted(out, key=lambda s: (-s.tier, s.when))

    # ---------- booking ----------
    def take(self, slot, existing):
        """Lock `slot`, pass the emailed code, then book (or rebook over `existing`)."""
        booked_ts = dt.datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            save("lock", self.icbc.lock(slot, booked_ts))
        except ApiError as e:
            log(f"{slot}: could not lock ({e})")
            return False
        log(f"locked {slot}; sending one-time code by email")
        try:
            sent_at = dt.datetime.now(dt.timezone.utc)
            save("send-otp", self.icbc.send_otp(booked_ts))
            notify("ICBC: 输入验证码", f"已锁定 {slot.when:%m-%d %a %H:%M}，请查邮件把验证码输进终端", urgent=True)
            for attempt in range(3):
                code = self.otp.wait(sent_at, timeout=self.args.otp_timeout)
                if not code:
                    log("no code arrived in time; releasing the slot")
                    return False
                result = self.icbc.verify_otp(booked_ts, code)
                save("verify-otp", result)
                if (result or {}).get("status") == "VERIFIED":
                    break
                self.otp.ignore.add(code)
                log(f"code {code} rejected ({(result or {}).get('status')}); try again")
            else:
                return False
            result = self.icbc.rebook(existing) if existing else self.icbc.book()
            save("book", result)
        finally:
            self.icbc.unlock()
        self.refresh_login(force=True)
        now_have = self.existing()
        if now_have and appt_when(now_have) == slot.when:
            log(f"BOOKED {slot}")
            notify("ICBC: 预约成功", f"Burnaby 路考 {slot.when:%m-%d %a %H:%M} 已预约（邮件确认）")
            return True
        raise RuntimeError(f"booking call returned but account shows {now_have and appt_when(now_have)}")

    # ---------- modes ----------
    def tick(self):
        self.refresh_login()
        existing = self.existing()
        if existing:
            cur = Slot(existing)
            if cur.tier >= 4:
                log(f"holding {cur}, already the best tier; done")
                return "done"
            if not self.free_to_change(existing):
                log(f"holding {cur}; changing it now would cost $25; done")
                return "done"
        if dt.datetime.now(TZ) >= self.args.end:
            log("window has passed; done")
            return "done"
        from_date = max(dt.date.today(), self.args.start.date())
        slots = self.search(from_date)
        wanted = self.wanted(slots)
        current_tier = Slot(existing).tier if existing else 0
        better = [s for s in wanted if s.tier > current_tier]
        log(f"{len(slots)} open slot(s), {len(wanted)} in window {wanted[:6]}"
            + (f"; holding {Slot(existing)}" if existing else ""))
        for slot in better:
            if self.args.dry_run:
                log(f"[dry-run] would book {slot}")
                return None
            if self.take(slot, existing):
                return None
        return None

    def hunt(self):
        deadline = time.monotonic() + self.args.run_minutes * 60
        while time.monotonic() < deadline:
            if self.should_hand_off(deadline):
                log("this run would end around midnight; handing off early so 00:00 is covered")
                return 0
            try:
                if self.tick() == "done":
                    return DONE
            except ApiError as e:
                log(f"API error: {e}")
            except requests.RequestException as e:
                log(f"network error: {e}")
            if self.args.once:
                return 0
            secs = self.interval()
            time.sleep(max(5, secs * random.uniform(0.8, 1.2)))
        return 0

    @staticmethod
    def should_hand_off(deadline, now=None):
        """True from 23:40 on when this run's deadline lands before 00:10 (CI runs are time-boxed)."""
        now = now or dt.datetime.now(TZ)
        if now.time() < T(23, 40):
            return False
        ends = now + dt.timedelta(seconds=deadline - time.monotonic())
        return ends < dt.datetime.combine(now.date() + dt.timedelta(days=1), T(0, 10), TZ)

    def list_slots(self):
        slots = self.search(max(dt.date.today(), self.args.start.date()))
        wanted = set(map(id, self.wanted(slots)))
        log(f"{len(slots)} open slot(s) at posId {self.pos_id}:")
        for s in sorted(slots, key=lambda s: s.when):
            print(f"   {s.when:%a %Y-%m-%d %H:%M}  tier {s.tier}  {'<= in window' if id(s) in wanted else ''}")
        return 0

    def trial(self):
        """Book a far-away slot (free to cancel), confirm it, then cancel it."""
        if self.existing():
            sys.exit("you already have a road test booked; the trial only runs with none")
        safe = dt.datetime.now(TZ) + dt.timedelta(hours=72)
        slots = self.search(max(dt.date.today(), self.args.start.date()))
        pick = [s for s in sorted(slots, key=lambda s: s.when)
                if s.when > safe and not (self.args.start <= s.when < self.args.end)]
        if not pick:
            sys.exit("no slot outside the window and >72 h away to test with")
        slot = pick[0]
        log(f"trial: booking {slot} at posId {self.pos_id}, then cancelling it right away")
        if not self.args.yes and input("Go ahead? [y/N] ").strip().lower() != "y":
            return 0
        if not self.take(slot, None):
            sys.exit("trial booking failed; see log above and icbc_debug/")
        appt = self.existing()
        save("cancel", self.icbc.cancel(appt))
        self.refresh_login(force=True)
        if self.existing():
            sys.exit("cancel call returned but the appointment is still there; cancel it on the website")
        log("trial OK: booked and cancelled; check your email for both confirmations")
        return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", type=lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=TZ),
                   default=dt.datetime(2026, 10, 24, 12, 0, tzinfo=TZ), help="window start (local)")
    p.add_argument("--end", type=lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=TZ),
                   default=dt.datetime(2026, 10, 30, 0, 0, tzinfo=TZ), help="window end, exclusive")
    p.add_argument("--pos-id", type=int, help="skip the centre lookup")
    p.add_argument("--exam-code", help="e.g. 5-R-1; default: first eligible exam")
    p.add_argument("--interval", type=int, default=60, help="seconds between checks outside --busy")
    p.add_argument("--busy", default="23:58-00:06=8,22:00-23:58=20,05:55-07:10=20",
                   help="release windows START-END=SECONDS; ICBC drops most slots at 00:00")
    p.add_argument("--run-minutes", type=float, default=60 * 24 * 7)
    p.add_argument("--relogin-minutes", type=float, default=20)
    p.add_argument("--lead-minutes", type=int, default=180, help="ignore slots starting sooner than this")
    p.add_argument("--fee-margin", type=int, default=60,
                   help="minutes of slack on top of 48 h before a change is considered free")
    p.add_argument("--otp-timeout", type=int, default=600, help="seconds to wait for the emailed code")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="log in, list open slots, exit")
    mode.add_argument("--trial", action="store_true", help="book a far slot then cancel it")
    mode.add_argument("--cancel", action="store_true", help="cancel the current road test booking")
    p.add_argument("--manual-otp", action="store_true",
                   help="allow hunting without Gmail; you must type the code yourself")
    p.add_argument("--dry-run", action="store_true", help="hunt but never lock or book")
    p.add_argument("--once", action="store_true", help="check once and exit")
    p.add_argument("--yes", action="store_true", help="no confirmation prompt for --trial (CI)")
    args = p.parse_args()

    load_env(HERE / ".env")
    need = {k: os.environ.get(k, "").strip() for k in ("ICBC_LAST_NAME", "ICBC_LICENCE", "ICBC_KEYWORD")}
    missing = [k for k, v in need.items() if not v]
    if missing:
        sys.exit(f"missing env vars: {', '.join(missing)}")
    icbc = Icbc(need["ICBC_LAST_NAME"], need["ICBC_LICENCE"], need["ICBC_KEYWORD"])
    otp = OtpSource(os.environ.get("GMAIL_ADDRESS", "").strip(),
                    os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip(),
                    ignore={need["ICBC_LICENCE"]})
    books = not (args.list or args.cancel or args.dry_run)
    if otp.gmail:
        try:
            otp.check()
            log("Gmail IMAP login OK; one-time codes will be read automatically")
        except imaplib.IMAP4.error as e:
            sys.exit(f"Gmail login failed ({e}); check GMAIL_ADDRESS / GMAIL_APP_PASSWORD")
    elif books and not args.manual_otp:
        sys.exit("set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env so codes are read automatically "
                 "(or pass --manual-otp to type them yourself)")
    g = Grabber(args, icbc, otp)
    g.setup()
    if args.list:
        return g.list_slots()
    if args.trial:
        return g.trial()
    if args.cancel:
        appt = g.existing()
        if not appt:
            sys.exit("no road test booking to cancel")
        if not g.free_to_change(appt) and input("Within 48 h: ICBC charges $25. Cancel anyway? [y/N] ").lower() != "y":
            return 0
        save("cancel", icbc.cancel(appt))
        log(f"cancelled {Slot(appt)}")
        return 0
    return g.hunt()


if __name__ == "__main__":
    sys.exit(main())
