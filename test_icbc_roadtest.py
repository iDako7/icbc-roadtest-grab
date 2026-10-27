"""Offline tests: a fake ICBC API replays the request/response shapes from the webdeas-ui bundle."""
import datetime as dt
import json as jsonlib
import types
import unittest
from unittest import mock

import icbc_roadtest as m

TZ = m.TZ
TODAY = dt.date.today()
MON = TODAY + dt.timedelta(days=4 + (7 - (TODAY + dt.timedelta(days=4)).weekday()) % 7)
SAT = MON - dt.timedelta(days=2)


def slot(day, hhmm, n):
    return {"appointmentDt": {"date": day.isoformat(), "dayOfWeek": day.strftime("%A")},
            "dlExam": {"code": "5-R-1", "description": "Class 5 road test"},
            "startTm": hhmm, "endTm": hhmm, "posId": 274, "resourceId": 9000 + n,
            "signature": f"sig{n}", "lemgMsgId": 1, "checkTm": hhmm}


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.content = b"x" if body is not None else b""
        self.text = str(body)

    def json(self):
        return self._body


class FakeIcbc:
    def __init__(self, slots, appts=()):
        self.headers = {}
        self.slots = slots
        self.appts = list(appts)
        self.calls = []
        self.locked = None

    def user(self):
        return {"drvrId": 777, "firstName": "ALEX", "lastName": "DOE", "licenseNumber": "12345678",
                "phoneNum": "6040000000", "cancellationFeeTotal": 0,
                "eligibleExams": [] if self.appts else [
                    {"code": "5-R-1", "description": "Class 5 road test", "eed": {"date": TODAY.isoformat()}}],
                "webAappointments": [dict(a) for a in self.appts]}

    def put(self, url, data, timeout, headers=None):
        self.calls.append(("PUT", url.split("/v1")[1], jsonlib.loads(data)))
        return Resp(200, self.user(), {"Authorization": "Bearer t"})

    def request(self, method, url, data, timeout):
        assert b", " not in data and b": " not in data, "JSON must be compact"
        json = jsonlib.loads(data)
        path = url.split("/v1")[1]
        self.calls.append((method, path, json))
        if path == "/web/getNearestPos":
            return Resp(200, [{"pos": {"posId": 1, "agency": "Burnaby Driver Licensing", "address": "3880 Lougheed"}},
                              {"pos": {"posId": 274, "agency": "Burnaby Claims Centre (ROAD TESTS ONLY)",
                                       "address": "4399 Wayburne Drive"}}])
        if path == "/web/getAvailableAppointments":
            return Resp(200, self.slots)
        if path == "/web/lock":
            self.locked = json if json.get("startTm") else self.locked
            return Resp(200, json)
        if path == "/web/sendOTP":
            return Resp(200, {"status": "SENT"})
        if path == "/web/verifyOTP":
            return Resp(200, {"status": "VERIFIED" if json["code"] == "424242" else "FAILED"})
        if path in ("/web/book", "/web/rebook"):
            lk = self.locked
            self.appts = [{"appointmentDt": lk["appointmentDt"], "dlExam": lk["dlExam"], "startTm": lk["startTm"],
                           "endTm": lk["endTm"], "posId": lk["posId"], "bookedTs": lk["bookedTs"]}]
            return Resp(200, {"ok": True})
        if path == "/web/cancel":
            self.appts = []
            return Resp(200, {"ok": True})
        return Resp(404, {"error": path})

    def paths(self):
        return [c[1] for c in self.calls]


def make(fake, **over):
    args = types.SimpleNamespace(
        start=dt.datetime.combine(SAT, dt.time(12), TZ), end=dt.datetime.combine(MON + dt.timedelta(days=4), dt.time(0), TZ),
        pos_id=None, exam_code=None, interval=60, busy="", run_minutes=1, relogin_minutes=20, lead_minutes=180,
        anytime_hours=0,
        fee_margin=60, otp_timeout=5, dry_run=False, once=True, yes=False)
    vars(args).update(over)
    otp = mock.Mock()
    otp.wait.return_value = "424242"
    otp.ignore = set()
    g = m.Grabber(args, m.Icbc("DOE", "12345678", "x", session=fake), otp)
    g.setup()
    return g, otp


@mock.patch.object(m, "save", lambda *a: None)
@mock.patch.object(m, "notify", lambda *a, **k: None)
class GrabberTest(unittest.TestCase):
    SLOTS = [slot(SAT, "13:00", 1), slot(MON, "09:20", 2), slot(MON, "15:00", 3),
             slot(MON + dt.timedelta(days=1), "12:00", 4), slot(MON + dt.timedelta(days=10), "11:00", 5)]

    def test_tiers(self):
        at = lambda d, h, mi: m.tier(dt.datetime.combine(d, dt.time(h, mi), TZ))
        self.assertEqual([at(MON, 8, 35), at(MON, 9, 15), at(MON, 9, 55), at(MON, 10, 50), at(MON, 13, 20),
                          at(MON, 14, 0), at(MON, 14, 55), at(SAT, 13, 0)], [0, 1, 2, 2, 2, 1, 0, 0])

    def test_rank_is_time_first_monday_breaks_ties(self):
        tue = MON + dt.timedelta(days=1)
        r = lambda d, h, mi: m.rank(dt.datetime.combine(d, dt.time(h, mi), TZ))
        order = [r(MON, 9, 55), r(MON, 13, 20), r(tue, 11, 30), r(MON, 9, 15), r(MON, 14, 0), r(tue, 9, 15)]
        self.assertEqual(order, [4, 4, 3, 2, 2, 1])
        self.assertEqual([r(MON, 8, 35), r(MON, 14, 55), r(tue, 15, 35)], [0, 0, 0])

    def test_finds_burnaby_claims_centre(self):
        g, _ = make(FakeIcbc(self.SLOTS))
        self.assertEqual(g.pos_id, 274)
        self.assertEqual(g.exam_code, "5-R-1")

    def test_books_best_tier_with_full_payloads(self):
        fake = FakeIcbc(self.SLOTS)
        g, _ = make(fake)
        g.tick()
        self.assertEqual(fake.appts[0]["startTm"], "12:00")  # Tuesday midday beats Monday 09:20
        calls = {p: body for _, p, body in fake.calls}
        search = calls["/web/getAvailableAppointments"]
        self.assertEqual((search["aPosID"], search["examType"], search["examDate"], search["lastName"]),
                         (274, "5-R-1", SAT.isoformat(), "DOE"))
        self.assertEqual(fake.locked["signature"], "sig4")
        self.assertEqual(fake.locked["drvrDriver"], {"drvrId": 777})
        sent = next(b for _, p, b in fake.calls if p == "/web/sendOTP")
        self.assertEqual((sent["method"], sent["drvrID"], sent["bookedTs"]), ("E", 777, fake.locked["bookedTs"]))
        self.assertEqual(calls["/web/book"], {"userId": "WEBD:777", "appointment": {"drvrDriver": {"drvrId": 777}}})
        self.assertEqual(calls["/web/lock"]["appointmentDt"], {}, "slot released after booking")

    def test_upgrades_by_rebook_while_free(self):
        fake = FakeIcbc(self.SLOTS[:2], appts=[slot(SAT, "13:00", 1)])
        g, _ = make(fake)
        g.tick()
        rebook = next(b for _, p, b in fake.calls if p == "/web/rebook")
        self.assertEqual(rebook["action"], "RE-BOOKED")
        self.assertEqual(rebook["appointment"]["startTm"], "13:00")  # the booking being replaced
        self.assertEqual(rebook["appointment"]["drvrDriver"]["licenseNumber"], "12345678")
        self.assertEqual(fake.appts[0]["startTm"], "09:20")

    def test_backup_outside_window_is_replaced_even_if_top_tier(self):
        far = MON + dt.timedelta(days=90)
        fake = FakeIcbc(self.SLOTS[:2], appts=[slot(far, "13:20", 8)])
        g, _ = make(fake)
        self.assertIsNone(g.tick())
        self.assertIn("/web/rebook", fake.paths())
        self.assertEqual(fake.appts[0]["startTm"], "09:20")  # second tier in the window beats best tier in January

    def test_backup_mode_books_earliest_good_slot_and_keeps_it(self):
        fake = FakeIcbc([slot(MON + dt.timedelta(days=90), "08:35", 1), slot(MON + dt.timedelta(days=91), "13:20", 2),
                         slot(MON + dt.timedelta(days=92), "11:30", 3)])
        g, _ = make(fake, yes=True)
        g.backup()
        self.assertEqual(fake.appts[0]["startTm"], "13:20")
        self.assertNotIn("/web/cancel", fake.paths())

    def test_waits_for_a_test_already_taken_to_clear_then_books(self):
        fake = FakeIcbc(self.SLOTS, appts=[slot(TODAY - dt.timedelta(days=1), "08:35", 9)])
        g, _ = make(fake)
        with mock.patch.object(m, "notify") as push:
            self.assertIsNone(g.tick())
            self.assertIsNone(g.tick())
        self.assertNotIn("/web/lock", fake.paths())
        self.assertEqual(push.call_count, 1, "one push, not one per poll")
        with self.assertRaises(SystemExit):
            g.backup()
        fake.appts = []  # ICBC clears it; seen at the next re-login
        g.icbc.login()
        g.tick()
        self.assertIn("/web/book", fake.paths())
        self.assertNotIn("/web/rebook", fake.paths())

    def test_skipped_times_wait_until_anytime_hours_before_the_window(self):
        tue = MON + dt.timedelta(days=1)
        fake = FakeIcbc([slot(tue, "08:35", 1), slot(MON, "15:35", 2), slot(MON, "08:35", 3)])
        g, _ = make(fake)  # anytime_hours=0 and the window is days away
        g.tick()
        self.assertNotIn("/web/lock", fake.paths())

    def test_close_to_the_window_takes_any_slot_then_upgrades_by_rank(self):
        tue = MON + dt.timedelta(days=1)
        far = MON + dt.timedelta(days=90)
        fake = FakeIcbc([slot(tue, "08:35", 1), slot(MON, "15:35", 2), slot(MON, "08:35", 3)],
                        appts=[slot(far, "11:30", 8)])  # an out-of-window backup does not count
        g, _ = make(fake, anytime_hours=24 * 30)
        g.tick()
        self.assertEqual((fake.appts[0]["appointmentDt"]["date"], fake.appts[0]["startTm"]),
                         (MON.isoformat(), "08:35"))  # Monday first, then earliest
        g.tick()
        self.assertEqual(fake.paths().count("/web/rebook"), 1, "holding a fallback, other fallbacks are not upgrades")
        fake.slots.append(slot(tue, "14:00", 4))
        g.tick()
        self.assertEqual(fake.appts[0]["startTm"], "14:00")

    def test_never_touches_booking_inside_48h(self):
        soon = TODAY + dt.timedelta(days=1)
        fake = FakeIcbc(self.SLOTS, appts=[slot(soon, "09:20", 9)])
        g, _ = make(fake, start=dt.datetime.combine(TODAY, dt.time(0), TZ))
        self.assertEqual(g.tick(), "done")
        self.assertNotIn("/web/getAvailableAppointments", fake.paths())

    def test_stops_once_best_tier_held(self):
        fake = FakeIcbc(self.SLOTS, appts=[slot(MON, "12:00", 6)])
        g, _ = make(fake)
        self.assertEqual(g.tick(), "done")

    def test_dry_run_never_locks(self):
        fake = FakeIcbc(self.SLOTS)
        g, _ = make(fake, dry_run=True)
        g.tick()
        self.assertNotIn("/web/lock", fake.paths())

    def test_wrong_code_retries_then_books(self):
        fake = FakeIcbc(self.SLOTS)
        g, otp = make(fake)
        otp.wait.side_effect = ["111111", "424242"]
        g.tick()
        self.assertEqual(len(fake.appts), 1)
        self.assertIn("111111", otp.ignore)

    def test_trial_books_outside_window_then_cancels(self):
        fake = FakeIcbc(self.SLOTS)
        g, _ = make(fake)
        with mock.patch("builtins.input", return_value="y"):
            g.trial()
        self.assertEqual(fake.locked["signature"], "sig5")
        cancel = next(b for _, p, b in fake.calls if p == "/web/cancel")
        self.assertEqual((cancel["action"], cancel["userId"], cancel["startTm"]), ("CANCELLED", "WEBD:777", "11:00"))
        self.assertEqual(fake.appts, [])


class PollTest(unittest.TestCase):
    def test_midnight_window_wraps(self):
        w = m.parse_windows("23:58-00:06=8,22:00-23:58=20,05:55-07:10=20")
        at = lambda h, mi: m.poll_seconds(dt.datetime(2026, 10, 22, h, mi, tzinfo=TZ), w, 60)
        self.assertEqual([at(23, 59), at(0, 0), at(0, 5), at(0, 6), at(22, 30), at(6, 0), at(12, 0)],
                         [8, 8, 8, 60, 20, 20, 60])


    def test_hands_off_before_midnight_only_when_run_would_end_there(self):
        now = dt.datetime(2026, 10, 22, 23, 45, tzinfo=TZ)
        mono = 1000.0
        with mock.patch.object(m.time, "monotonic", return_value=mono):
            self.assertTrue(m.Grabber.should_hand_off(mono + 10 * 60, now))      # ends 23:55
            self.assertFalse(m.Grabber.should_hand_off(mono + 60 * 60, now))     # ends 00:45
            early = now.replace(hour=23, minute=0)
            self.assertFalse(m.Grabber.should_hand_off(mono + 10 * 60, early))   # too early to care


class OtpTest(unittest.TestCase):
    def test_extracts_code_not_licence(self):
        o = m.OtpSource(ignore={"12345678"})
        self.assertEqual(o.extract("Driver licence 12345678. Your verification code is 482913."), "482913")
        self.assertEqual(o.extract("Use passcode: 0571 to book"), "0571")
        self.assertIsNone(o.extract("licence 12345678 booked"))


if __name__ == "__main__":
    unittest.main()
