"""ReportFix backend. Pure standard library (no installs). Run:  python server.py   ->  http://localhost:8000
All state is in memory (reset = one click). Permissions, priority, voting and trust are enforced here.
"""
import json
import math
import mimetypes
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import triage

LOCK = threading.RLock()
ROOT = os.path.dirname(os.path.abspath(__file__))
FRONT = os.path.abspath(os.path.join(ROOT, "..", "frontend"))
VOTE_WINDOW = int(os.environ.get("VOTE_WINDOW", "120"))   # seconds the confirmation vote stays open
NEW_ACCT_SECS = 600      # accounts younger than this count as "new"
BRIG_N = 4               # this many new accounts acting within BRIG_SECS = brigading
BRIG_SECS = 60
DAILY_GAIN_CAP = 15
MAX_LEN = 1000

PENDING = "Resolved (Pending Confirmation)"
FAKE = "Rejected (Fake)"
CLOSED_SET = ("Closed", FAKE)
BASE_IMPACT = {1: 100, 2: 70, 3: 40, 4: 15}
TRANS = {"New": ["Acknowledged", "In Progress", "Resolved"],
         "Acknowledged": ["In Progress", "Resolved"],
         "In Progress": ["Acknowledged", "Resolved"],
         "Reopened": ["Acknowledged", "In Progress", "Resolved"]}
STUDENT_NAMES = ["Ayesha", "Bilal", "Dua", "Farhan", "Hira", "Imran"]
NEW_NAMES = ["Zara", "Omar", "Sana", "Hamza", "Noor", "Taha", "Mahnoor", "Ali", "Rida", "Usman"]

S = {}


class HttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def now():
    return time.time()


# ------------------------------------------------------------------ state
def init_state():
    global S
    S = {"users": {}, "ledger": {}, "issues": {}, "audit": [], "iseq": 0, "nseq": 0,
         "tokens": {}, "rate": {}, "reveals": [], "flags": []}
    for i, n in enumerate(STUDENT_NAMES, 1):
        add_user("s%d" % i, n, "student", None, old=True)
    for d in triage.DEPARTMENTS:
        add_user("staff_" + d.lower(), "Staff %s" % d, "staff", d, old=True)
    add_user("admin", "Admin (Registrar)", "admin", None, old=True)
    # sample data goes through the real pipeline
    submit_report(S["users"]["s1"], "Library AC kharab hai, bohat garmi hai", "seed1", seed=True)
    submit_report(S["users"]["s2"], "projector not working in lab 2", "seed2", seed=True)


def add_user(uid, name, role, dept, old):
    t = now()
    S["users"][uid] = {"id": uid, "name": name, "role": role, "dept": dept,
                       "created": t - 86400 if old else t}
    if role == "student":
        S["ledger"][uid] = [{"delta": 50, "reason": "Starting score for every student", "issue": None, "ts": t}]


def audit(actor, action, target, detail=""):
    S["audit"].append({"ts": now(), "actor": actor, "action": action, "target": target, "detail": detail})


# ------------------------------------------------------------------ trust
def score(uid):
    return max(0, min(100, sum(e["delta"] for e in S["ledger"].get(uid, []))))


def is_new(uid, at=None):
    return ((at or now()) - S["users"][uid]["created"]) < NEW_ACCT_SECS


def weight(uid):
    if S["users"][uid]["role"] != "student":
        return 0
    w = max(0.2, min(1.2, 0.2 + score(uid) / 100.0))
    return w * (0.5 if is_new(uid) else 1.0)


def add_trust(uid, delta, reason, issue=None):
    if uid not in S["ledger"]:
        return
    cur = score(uid)
    if delta > 0:
        day = now() - 86400
        gained = sum(e["delta"] for e in S["ledger"][uid][1:] if e["delta"] > 0 and e["ts"] > day)
        if gained + delta > DAILY_GAIN_CAP:
            delta2 = max(0, DAILY_GAIN_CAP - gained)
            if delta2 < delta:
                reason += " (daily gain cap reached: +%d of +%d applied)" % (delta2, delta)
            delta = delta2
    new = max(0, min(100, cur + delta))
    S["ledger"][uid].append({"delta": new - cur, "reason": reason, "issue": issue, "ts": now()})


def brigade_set(events):
    new = sorted((ts, u) for u, ts in events if ts - S["users"][u]["created"] < NEW_ACCT_SECS)
    flagged = set()
    for i in range(len(new)):
        grp = {u for t, u in new if new[i][0] <= t <= new[i][0] + BRIG_SECS}
        if len(grp) >= BRIG_N:
            flagged |= grp
    return flagged


def apply_brigade_penalty(issue, flagged, what):
    for u in flagged:
        if u not in issue["brig_pen"]:
            issue["brig_pen"].add(u)
            add_trust(u, -10, "Brigading flag: %d+ new accounts acted together on %s (%s)" % (BRIG_N, issue["id"], what), issue["id"])
            S["flags"].append({"ts": now(), "kind": "brigading", "issue": issue["id"], "detail":
                               "New account %s acted in a burst with others (%s)" % (S["users"][u]["name"], what)})


# ------------------------------------------------------------------ crowd priority
def crowd(issue):
    events = [(u, m["ts"]) for u, m in issue["members"].items()]
    brig = brigade_set(events)
    total, credible = 0.0, 0
    for u in issue["members"]:
        w = weight(u) * (0.3 if u in brig else 1.0)
        c = min(w, 1.0)
        total += c
        if c >= 0.5:
            credible += 1
    return {"sum": round(total, 2), "credible": credible, "brigading": len(brig), "brig": brig}


def prio_score(issue, cr=None):
    cr = cr or crowd(issue)
    p = issue["priority"]
    base = BASE_IMPACT[p]
    factor = 1 + math.log2(1 + cr["sum"])
    sc = base * factor
    note = "base P%d=%d x crowd %.2f (%d distinct students, weighted sum %.2f, each capped at 1.0)" % (
        p, base, factor, len(issue["members"]), cr["sum"])
    if p == 4 and cr["credible"] < 5 and sc > BASE_IMPACT[3]:
        sc = BASE_IMPACT[3]
        note += "; minor-issue ceiling: needs 5 credible students to outrank a normal issue"
    if p == 1:
        note += "; safety hazard always ranks first"
    return round(sc, 1), note


def sort_key(issue):
    sc, _ = prio_score(issue)
    done = 1 if issue["status"] in CLOSED_SET else 0
    return (done, 0 if issue["priority"] == 1 else 1, -sc)


# ------------------------------------------------------------------ issues
def tl(issue, text):
    issue["timeline"].append({"ts": now(), "text": text})
    issue["updated"] = now()


def new_issue(user, item, text, tr, conf):
    S["iseq"] += 1
    iid = "RF-%03d" % S["iseq"]
    issue = {"id": iid, "place": item["place"], "type": item["type"], "dept": item["dept"],
             "priority": item["priority"], "reason": item["reason"], "status": "New", "confidential": conf,
             "created": now(), "updated": now(), "members": {user["id"]: {"kind": "reporter", "ts": now()}},
             "reports": [], "notes": [], "timeline": [], "votes": {}, "cycle": 1, "vote_hist": [],
             "reopen_count": 0, "escalated": False, "flags": [], "explanation": tr["explanation"],
             "validated": False, "corroborated": False, "brig_pen": set(), "deadline": None,
             "recurrence_of": None, "needs_reroute": False, "since_reopen_note": False}
    S["issues"][iid] = issue
    tl(issue, "Issue created (%s)" % ("confidential" if conf else "routed to %s" % item["dept"]))
    return issue


def add_report(issue, user, text, tr):
    issue["reports"].append({"uid": user["id"], "text": text, "ts": now(),
                             "injection": bool(tr["injection"]), "shouting": tr["shouting"]})
    if tr["injection"] and not any(f["kind"] == "injection" for f in issue["flags"]):
        issue["flags"].append({"kind": "injection", "detail": "Report contained instruction-like text; it was ignored. " + "; ".join(sorted(set(tr["injection"])))})


def submit_report(user, text, token, seed=False):
    if user["role"] != "student":
        raise HttpError(403, "Only students can submit reports")
    text = (text or "").strip()
    if not text:
        raise HttpError(400, "Please describe the problem")
    if len(text) > MAX_LEN:
        raise HttpError(400, "Report too long (max %d characters)" % MAX_LEN)
    key = (user["id"], token)
    if token and key in S["tokens"]:
        return S["tokens"][key]
    if not seed:
        recent = [t for t in S["rate"].get(user["id"], []) if t > now() - 60]
        if len(recent) >= 5:
            if not S["rate"].get(user["id"] + "_pen") or S["rate"][user["id"] + "_pen"] < now() - 20:
                add_trust(user["id"], -3, "Spam: more than 5 reports in one minute")
                S["rate"][user["id"] + "_pen"] = now()
            raise HttpError(429, "Too many reports. Limit is 5 per minute. Please wait.")
        recent.append(now())
        S["rate"][user["id"]] = recent
    tr = triage.analyze(text)
    results = []
    if tr["injection"]:
        add_trust(user["id"], -10, "Report contained instructions aimed at the system (prompt injection attempt)")
        audit(user["id"], "injection_flag", "-", "; ".join(tr["injection"]))
    if tr["abusive"]:
        add_trust(user["id"], -5, "Abusive language in a report")
    for c in tr["clarifications"]:
        results.append({"kind": "clarification", "message": c["message"]})
    for item in tr["items"]:
        conf = tr["confidential"]
        if conf:
            issue = new_issue(user, item, text, tr, True)
            add_report(issue, user, text, tr)
            issue["dept"] = "Admin (Welfare)"
            results.append({"kind": "created", "issue": ser(issue, user)})
            continue
        match = next((i for i in S["issues"].values() if not i["confidential"] and i["place"] == item["place"]
                      and i["type"] == item["type"] and i["status"] not in CLOSED_SET and
                      i["status"] != "Closed"), None)
        if match:
            if user["id"] in match["members"]:
                add_report(match, user, text, tr)
                results.append({"kind": "already", "message": "You are already part of %s (%s at %s). No duplicate created." % (
                    match["id"], triage.TYPE_LABEL[match["type"]], match["place"]), "issue": ser(match, user)})
                continue
            join(match, user, "reported the same problem")
            add_report(match, user, text, tr)
            results.append({"kind": "joined", "issue": ser(match, user),
                            "message": "This matches %s, so you were added as 'me too' (%d students now)." % (match["id"], len(match["members"]))})
            continue
        issue = new_issue(user, item, text, tr, False)
        add_report(issue, user, text, tr)
        prev = next((i for i in S["issues"].values() if not i["confidential"] and i["place"] == item["place"]
                     and i["type"] == item["type"] and i["status"] == "Closed" and now() - i["updated"] < 86400), None)
        if prev:
            issue["recurrence_of"] = prev["id"]
            tl(issue, "Possible recurrence of closed issue %s" % prev["id"])
        results.append({"kind": "created", "issue": ser(issue, user)})
    if not results:
        results.append({"kind": "clarification", "message": "I could not find a campus problem in that text."})
    out = {"results": results, "explanation": tr["explanation"], "shouting": tr["shouting"],
           "injection": bool(tr["injection"]), "confidential": tr["confidential"]}
    if token:
        S["tokens"][key] = out
    return out


def join(issue, user, how):
    issue["members"][user["id"]] = {"kind": "joiner", "ts": now()}
    tl(issue, "A student joined (me too). %d affected students." % len(issue["members"]))
    if (len(issue["members"]) >= 2 and not issue["corroborated"] and weight(user["id"]) >= 0.5):
        issue["corroborated"] = True
        rep = next((u for u, m in issue["members"].items() if m["kind"] == "reporter"), None)
        if rep:
            add_trust(rep, 3, "Report corroborated by another credible student (%s)" % issue["id"], issue["id"])
    apply_brigade_penalty(issue, crowd(issue)["brig"], "join")


# ------------------------------------------------------------------ voting
def tick():
    t = now()
    for issue in list(S["issues"].values()):
        if issue["status"] == PENDING and issue["deadline"] and issue["deadline"] <= t:
            evaluate(issue, force=True, why="vote window ended")


def evaluate(issue, force=False, why=""):
    votes = issue["votes"]
    members = list(issue["members"])
    brig = brigade_set([(u, v["ts"]) for u, v in votes.items()])
    apply_brigade_penalty(issue, brig, "vote")

    def vw(u):
        return weight(u) * (0.3 if u in brig else 1.0)
    tw = sum(vw(u) for u in members) or 1e-9
    D = sum(vw(u) for u, v in votes.items() if v["choice"] == "dispute")
    C = sum(vw(u) for u, v in votes.items() if v["choice"] == "confirm")
    nd = sum(1 for v in votes.values() if v["choice"] == "dispute")
    need = min(2, len(members))
    outcome = None
    if nd >= need and D > C and D >= 0.5 * tw:
        outcome = "reopen"
    elif C > D and C >= 0.5 * tw:
        outcome = "close"
    elif len(votes) >= len(members) or force:
        outcome = "reopen" if (nd >= need and D > C) else "close"
    if not outcome:
        return
    voted = C + D
    if voted > 0:
        win = "dispute" if outcome == "reopen" else "confirm"
        clear = len(votes) >= 2 and max(C, D) / voted >= 0.7
        for u, v in votes.items():
            if u in brig:
                continue
            if v["choice"] == win:
                add_trust(u, 2, "Vote matched the final outcome on %s" % issue["id"], issue["id"])
            elif clear:
                add_trust(u, -2, "Vote went against a clear outcome on %s" % issue["id"], issue["id"])
    issue["vote_hist"].append({"cycle": issue["cycle"], "outcome": outcome, "confirm": round(C, 2), "dispute": round(D, 2)})
    if outcome == "reopen":
        issue["status"] = "Reopened"
        issue["reopen_count"] += 1
        issue["cycle"] += 1
        issue["votes"] = {}
        issue["deadline"] = None
        issue["since_reopen_note"] = False
        tl(issue, "Reopened by affected students (weighted disputes %.2f vs confirms %.2f). Staff must add a note before resolving again." % (D, C))
        if issue["reopen_count"] >= 3 and not issue["escalated"]:
            issue["escalated"] = True
            tl(issue, "Escalated to admin after %d reopenings" % issue["reopen_count"])
    else:
        issue["status"] = "Closed"
        issue["deadline"] = None
        tl(issue, "Closed: fix confirmed (%s)" % (why or "confirmations %.2f vs disputes %.2f" % (C, D)))


# ------------------------------------------------------------------ visibility + serialisation
def can_see(user, issue):
    r = user["role"]
    if r == "admin":
        return True
    if issue["confidential"]:
        return user["id"] in issue["members"]
    if r == "student":
        return issue["status"] != FAKE or user["id"] in issue["members"]
    if r == "staff":
        return issue["dept"] == user["dept"]
    return False


def get_issue(user, iid):
    issue = S["issues"].get(iid)
    if not issue or not can_see(user, issue):
        raise HttpError(404, "Issue not found")
    return issue


def ser(issue, user):
    r, uid = user["role"], user["id"]
    cr = crowd(issue)
    sc, why = prio_score(issue, cr)
    member = uid in issue["members"]
    conf = issue["confidential"]
    d = {"id": issue["id"], "place": issue["place"], "type": issue["type"],
         "type_label": triage.TYPE_LABEL.get(issue["type"], issue["type"]), "dept": issue["dept"],
         "priority": issue["priority"], "status": issue["status"], "reason": issue["reason"],
         "confidential": conf, "created": issue["created"], "updated": issue["updated"],
         "me_too": len(issue["members"]), "score": sc, "rank_why": why, "crowd": {k: v for k, v in cr.items() if k != "brig"},
         "reopen_count": issue["reopen_count"], "escalated": issue["escalated"],
         "recurrence_of": issue["recurrence_of"], "needs_reroute": issue["needs_reroute"],
         "member": member, "deadline": issue["deadline"], "cycle": issue["cycle"]}
    if conf:
        d["me_too"] = 1
        d["rank_why"] = "confidential: never ranked on public boards"
    if member or r in ("staff", "admin"):
        d["timeline"] = issue["timeline"]
        d["notes"] = issue["notes"]
    if issue["status"] == PENDING:
        vs = issue["votes"].values()
        d["vote"] = {"confirms": sum(1 for v in vs if v["choice"] == "confirm"),
                     "disputes": sum(1 for v in vs if v["choice"] == "dispute"), "affected": len(issue["members"])}
    d["can_vote"] = bool(r == "student" and member and issue["status"] == PENDING)
    d["my_vote"] = issue["votes"].get(uid, {}).get("choice") if member else None
    d["can_join"] = bool(r == "student" and not member and not conf and issue["status"] not in CLOSED_SET)
    if r == "student" and member:
        d["my_reports"] = [x["text"] for x in issue["reports"] if x["uid"] == uid]
    if r == "staff" or r == "admin":
        d["reports"] = [{"text": x["text"], "ts": x["ts"], "injection": x["injection"], "shouting": x["shouting"]}
                        for x in issue["reports"]]
        d["explanation"] = issue["explanation"]
        d["flags"] = issue["flags"]
        d["vote_hist"] = issue["vote_hist"]
    if r == "admin":
        if conf:
            d["reporters"] = "Anonymous reporter (identity hidden; reveal is logged)"
        else:
            d["reporters"] = [{"name": S["users"][u]["name"], "kind": m["kind"], "trust": score(u),
                               "weight": round(weight(u), 2)} for u, m in issue["members"].items()]
    return d


def ser_list(issues, user):
    return [ser(i, user) for i in sorted(issues, key=sort_key)]


# ------------------------------------------------------------------ actions
def set_status(user, issue, new, note):
    r = user["role"]
    if r == "staff":
        if issue["confidential"] or issue["dept"] != user["dept"]:
            raise HttpError(404, "Issue not found")
    elif r != "admin":
        raise HttpError(403, "Only staff or admin can update status")
    cur = issue["status"]
    if new not in TRANS.get(cur, []):
        raise HttpError(400, "Invalid transition: %s -> %s" % (cur, new))
    note = (note or "").strip()[:300]
    if note:
        issue["notes"].append({"ts": now(), "who": "Staff" if r == "staff" else "Admin", "text": note})
        issue["since_reopen_note"] = True
    if new == "Resolved":
        if issue["reopen_count"] > 0 and not issue["since_reopen_note"]:
            raise HttpError(400, "This issue was reopened. Add a note explaining the new fix before resolving.")
        issue["status"] = PENDING
        issue["deadline"] = now() + VOTE_WINDOW
        issue["votes"] = {}
        tl(issue, "Marked resolved by %s. Affected students have %ds to confirm or dispute." % (
            "department staff" if r == "staff" else "admin", VOTE_WINDOW))
    else:
        issue["status"] = new
        tl(issue, "Status changed to %s" % new)
        if not issue["validated"]:
            issue["validated"] = True
            rep = next((u for u, m in issue["members"].items() if m["kind"] == "reporter"), None)
            if rep:
                add_trust(rep, 2, "Report validated by %s (%s)" % ("staff" if r == "staff" else "admin", issue["id"]), issue["id"])
    audit(user["id"], "status", issue["id"], "%s -> %s %s" % (cur, issue["status"], note))


# ------------------------------------------------------------------ API
def uinfo(u):
    d = {"id": u["id"], "name": u["name"], "role": u["role"], "dept": u["dept"]}
    if u["role"] == "student":
        d.update({"trust": score(u["id"]), "weight": round(weight(u["id"]), 2), "new": is_new(u["id"])})
    return d


def need(user, *roles):
    if user["role"] not in roles:
        raise HttpError(403, "Not allowed for your role")


def api(method, path, body, user_id):
    tick()
    p = path.rstrip("/")
    # ---- judge tools (no auth)
    if method == "GET" and p == "/api/users":
        return {"users": [uinfo(u) for u in S["users"].values()]}
    if method == "POST" and p == "/api/judge/new-student":
        S["nseq"] += 1
        uid = "n%d" % S["nseq"]
        name = "%s (new #%d)" % (NEW_NAMES[(S["nseq"] - 1) % len(NEW_NAMES)], S["nseq"])
        add_user(uid, name, "student", None, old=False)
        return {"user": uinfo(S["users"][uid])}
    if method == "POST" and p == "/api/judge/reset":
        init_state()
        return {"ok": True}
    if method == "GET" and p == "/api/places":
        return {"places": triage.PLACES, "departments": triage.DEPARTMENTS}
    if method == "POST" and p == "/api/triage-preview":
        t = (body.get("text") or "")[:MAX_LEN]
        r = triage.analyze(t)
        return {"items": r["items"], "clarifications": r["clarifications"], "confidential": r["confidential"],
                "injection": r["injection"], "shouting": r["shouting"], "explanation": r["explanation"]}

    user = S["users"].get(user_id)
    if not user:
        raise HttpError(401, "Unknown user. Pick a user from the top bar.")

    if method == "GET" and p == "/api/me":
        return {"user": uinfo(user)}
    if method == "POST" and p == "/api/reports":
        return submit_report(user, body.get("text"), body.get("token"))
    if method == "GET" and p == "/api/board":
        need(user, "student")
        return {"issues": ser_list([i for i in S["issues"].values() if not i["confidential"] and i["status"] != FAKE
                                    and i["status"] != "Closed"], user)}
    if method == "GET" and p == "/api/my/issues":
        need(user, "student")
        return {"issues": ser_list([i for i in S["issues"].values() if user["id"] in i["members"]], user)}
    if method == "GET" and p == "/api/trending":
        need(user, "student", "admin")
        out = []
        for i in S["issues"].values():
            if i["confidential"] or i["status"] in CLOSED_SET:
                continue
            brig = crowd(i)["brig"]
            tr = sum(min(weight(u) * (0.3 if u in brig else 1), 1.0) for u, m in i["members"].items() if m["ts"] > now() - 600)
            if tr > 0 and len(i["members"]) >= 2:
                out.append((tr, i))
        out.sort(key=lambda x: -x[0])
        return {"issues": [dict(ser(i, user), trend=round(t, 2)) for t, i in out[:5]]}
    if method == "GET" and p == "/api/staff/queue":
        need(user, "staff")
        return {"issues": ser_list([i for i in S["issues"].values() if not i["confidential"] and i["dept"] == user["dept"]
                                    and i["status"] != FAKE], user)}
    if method == "GET" and p == "/api/admin/dashboard":
        need(user, "admin")
        allv = list(S["issues"].values())
        pub = [i for i in allv if not i["confidential"]]
        return {"issues": ser_list(pub, user), "welfare": ser_list([i for i in allv if i["confidential"]], user),
                "users": [uinfo(u) for u in S["users"].values() if u["role"] == "student"],
                "flags": S["flags"][-30:][::-1], "summary": summary()}
    if method == "GET" and p == "/api/audit":
        need(user, "admin")
        return {"audit": S["audit"][-100:][::-1]}
    m = re.fullmatch(r"/api/trust/([\w]+)", p)
    if method == "GET" and m:
        uid = m.group(1)
        if uid not in S["ledger"]:
            raise HttpError(404, "No such student")
        if user["role"] != "admin" and user["id"] != uid:
            raise HttpError(403, "You can only view your own trust ledger")
        led = S["ledger"][uid]
        return {"user": uinfo(S["users"][uid]), "ledger": led[::-1], "sum": sum(e["delta"] for e in led),
                "formula": "score = sum of ledger entries (clamped 0-100). Weight = clamp(0.2 + score/100, 0.2, 1.2), halved for accounts under 10 minutes old, x0.3 if flagged as brigading."}
    m = re.fullmatch(r"/api/issues/([\w-]+)(?:/(\w[\w-]*))?", p)
    if m:
        iid, act = m.group(1), m.group(2)
        issue = get_issue(user, iid)
        if method == "GET" and not act:
            return {"issue": ser(issue, user)}
        if method == "POST" and act == "join":
            need(user, "student")
            if issue["confidential"] or issue["status"] in CLOSED_SET:
                raise HttpError(400, "This issue cannot be joined")
            if user["id"] in issue["members"]:
                raise HttpError(400, "You already joined this issue")
            join(issue, user, "me too")
            return {"issue": ser(issue, user)}
        if method == "POST" and act == "vote":
            need(user, "student")
            if user["id"] not in issue["members"]:
                raise HttpError(403, "Only students affected by this issue can vote")
            if issue["status"] != PENDING:
                raise HttpError(400, "Voting is not open for this issue")
            ch = body.get("choice")
            if ch not in ("confirm", "dispute"):
                raise HttpError(400, "choice must be confirm or dispute")
            issue["votes"][user["id"]] = {"choice": ch, "ts": now(), "comment": (body.get("comment") or "")[:200]}
            tl(issue, "A vote was cast (%d of %d affected students have voted)" % (len(issue["votes"]), len(issue["members"])))
            evaluate(issue)
            return {"issue": ser(issue, user)}
        if method == "POST" and act == "status":
            set_status(user, issue, body.get("status"), body.get("note"))
            return {"issue": ser(issue, user)}
        if method == "POST" and act == "note":
            need(user, "staff", "admin")
            if user["role"] == "staff" and (issue["confidential"] or issue["dept"] != user["dept"]):
                raise HttpError(404, "Issue not found")
            txt = (body.get("text") or "").strip()[:300]
            if not txt:
                raise HttpError(400, "Empty note")
            issue["notes"].append({"ts": now(), "who": user["name"], "text": txt})
            issue["since_reopen_note"] = True
            tl(issue, "Note added")
            return {"issue": ser(issue, user)}
        if method == "POST" and act == "reject-dept":
            need(user, "staff")
            if issue["confidential"] or issue["dept"] != user["dept"]:
                raise HttpError(404, "Issue not found")
            reason = (body.get("reason") or "").strip()
            if len(reason) < 3:
                raise HttpError(400, "Give a reason")
            audit(user["id"], "reject_dept", issue["id"], reason)
            issue["dept"] = "Admin Review"
            issue["needs_reroute"] = True
            tl(issue, "Staff said wrong department; sent to admin for rerouting")
            return {"ok": True}
        if method == "POST" and act == "close-vote":
            need(user, "admin")
            if issue["status"] != PENDING:
                raise HttpError(400, "No open vote")
            evaluate(issue, force=True, why="vote closed by admin")
            audit(user["id"], "close_vote", issue["id"])
            return {"issue": ser(issue, user)}
        if method == "POST" and act == "override":
            need(user, "admin")
            reason = (body.get("reason") or "").strip()
            if len(reason) < 3:
                raise HttpError(400, "A reason is required for overrides")
            ch = []
            if body.get("dept"):
                if body["dept"] not in triage.DEPARTMENTS:
                    raise HttpError(400, "Unknown department")
                issue["dept"], issue["needs_reroute"] = body["dept"], False
                ch.append("dept=%s" % body["dept"])
            if body.get("priority"):
                if int(body["priority"]) not in (1, 2, 3, 4):
                    raise HttpError(400, "Priority must be 1-4")
                issue["priority"] = int(body["priority"])
                ch.append("priority=P%s" % body["priority"])
            if body.get("fake"):
                issue["status"] = FAKE
                rep = next((u for u, m2 in issue["members"].items() if m2["kind"] == "reporter"), None)
                if rep:
                    add_trust(rep, -15, "Report marked fake/spam by admin (%s)" % issue["id"], issue["id"])
                ch.append("marked fake")
            tl(issue, "Admin override: %s" % ", ".join(ch))
            audit(user["id"], "override", issue["id"], "%s | %s" % (", ".join(ch), reason))
            return {"issue": ser(issue, user)}
        if method == "POST" and act == "reveal":
            need(user, "admin")
            reason = (body.get("reason") or "").strip()
            if not issue["confidential"]:
                raise HttpError(400, "Only needed for confidential issues")
            if len(reason) < 10:
                raise HttpError(400, "Type a reason of at least 10 characters")
            S["reveals"] = [t for t in S["reveals"] if t > now() - 60]
            if len(S["reveals"]) >= 5:
                raise HttpError(429, "Reveal rate limit reached")
            S["reveals"].append(now())
            rep = next(u for u, m2 in issue["members"].items() if m2["kind"] == "reporter")
            audit(user["id"], "REVEAL_IDENTITY", issue["id"], reason)
            return {"reporter": S["users"][rep]["name"]}
    raise HttpError(404, "Unknown endpoint")


def summary():
    allv = list(S["issues"].values())
    pub = [i for i in allv if not i["confidential"]]
    openi = [i for i in pub if i["status"] not in CLOSED_SET]
    by = {}
    for i in openi:
        by[i["dept"]] = by.get(i["dept"], 0) + 1
    top = sorted(openi, key=sort_key)[:3]
    lines = ["%d open issues across departments (%s)." % (len(openi), ", ".join("%s %d" % kv for kv in sorted(by.items())) or "none"),
             "%d safety (P1) issues open." % sum(1 for i in openi if i["priority"] == 1)]
    if top:
        lines.append("Top items: " + "; ".join("%s %s at %s (%d students)" % (i["id"], triage.TYPE_LABEL[i["type"]], i["place"], len(i["members"])) for i in top) + ".")
    lines.append("%d confidential welfare cases (counts only; details restricted)." % sum(1 for i in allv if i["confidential"]))
    lines.append("%d reopened, %d awaiting student confirmation, %d flags raised." % (
        sum(1 for i in pub if i["status"] == "Reopened"), sum(1 for i in pub if i["status"] == PENDING), len(S["flags"])))
    return " ".join(lines)


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            if method != "GET":
                return self._send(405, {"error": "method not allowed"})
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            fp = os.path.abspath(os.path.join(FRONT, rel))
            if not fp.startswith(FRONT) or not os.path.isfile(fp):
                return self._send(404, {"error": "not found"})
            with open(fp, "rb") as f:
                return self._send(200, f.read(), mimetypes.guess_type(fp)[0] or "application/octet-stream")
        body = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if n > 20000:
                return self._send(413, {"error": "payload too large"})
            raw = self.rfile.read(n) if n else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError
            except Exception:
                return self._send(400, {"error": "invalid JSON"})
        try:
            with LOCK:
                out = api(method, path, body, self.headers.get("X-User-Id", ""))
            self._send(200, out)
        except HttpError as e:
            self._send(e.code, {"error": e.msg})
        except Exception as e:  # never crash the server on odd input
            import traceback
            traceback.print_exc()
            self._send(500, {"error": "Internal error: %s" % e})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


if __name__ == "__main__":
    init_state()
    port = int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print("ReportFix running on http://localhost:%d  (LLM assist: %s)" % (port, triage.LLM_MODE or "off, rules only"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
