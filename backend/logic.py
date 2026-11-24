"""Business logic, scoring algorithms, trust ledger, crowd priority, and lifecycle management."""
import math
import time
import json
from backend.db import get_db

PRIORITY_BASE_IMPACT = {
    1: 100.0,  # P1 Critical (Safety hazard)
    2: 70.0,   # P2 High
    3: 40.0,   # P3 Normal
    4: 15.0,   # P4 Low
}


def clamp(val, min_val, max_val):
    return max(min_val, min(max_val, val))


USER_ALIASES = {
    "s1": "student_ali",
    "s2": "student_bilal",
    "s3": "student_zainab",
    "s4": "student_hamza",
    "s5": "student_sara",
    "admin": "admin_user",
    "student_ali": "s1",
    "student_bilal": "s2",
    "student_zainab": "s3",
    "student_hamza": "s4",
    "student_sara": "s5",
    "admin_user": "admin",
}

def get_user(conn, user_id):
    c = conn.cursor()
    row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row and user_id in USER_ALIASES:
        row = c.execute("SELECT * FROM users WHERE id = ?", (USER_ALIASES[user_id],)).fetchone()
    return dict(row) if row else None


def get_user_trust_weight(conn, user_id, check_brigading=False, issue_id=None):
    """Calculates effective trust weight for a user.
    Formula: clamp(0.2 + score/100, 0.2, 1.2) * new_account_factor * brigading_factor
    """
    user = get_user(conn, user_id)
    if not user:
        return 0.5
    score = user["trust_score"]
    base_weight = clamp(0.2 + (score / 100.0), 0.2, 1.2)

    # New account factor (50% weight for new accounts)
    new_factor = 0.5 if user["is_new"] else 1.0

    # Brigading factor
    brigading_factor = 1.0
    if check_brigading and issue_id:
        c = conn.cursor()
        flag = c.execute("SELECT 1 FROM flags WHERE target_type = 'issue' AND target_id = ? AND kind = 'brigading'",
                         (str(issue_id),)).fetchone()
        if flag:
            brigading_factor = 0.2  # 80% discount on brigaded issues

    return base_weight * new_factor * brigading_factor


def update_user_trust(conn, user_id, delta, reason, issue_id=None):
    """Updates user trust score and logs explainable ledger entry."""
    c = conn.cursor()
    now = time.time()
    c.execute("INSERT INTO trust_ledger (user_id, delta, reason, issue_id, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, delta, reason, issue_id, now))

    # Recalculate trust score as base 50 + sum of deltas
    total_delta = c.execute("SELECT COALESCE(SUM(delta), 0) FROM trust_ledger WHERE user_id = ?",
                            (user_id,)).fetchone()[0]
    new_score = clamp(50.0 + total_delta, 0.0, 100.0)

    # If user has done several positive actions, remove is_new flag
    action_count = c.execute("SELECT COUNT(*) FROM trust_ledger WHERE user_id = ?", (user_id,)).fetchone()[0]
    is_new = 0 if action_count >= 3 else None

    if is_new is not None:
        c.execute("UPDATE users SET trust_score = ?, is_new = ? WHERE id = ?", (new_score, is_new, user_id))
    else:
        c.execute("UPDATE users SET trust_score = ? WHERE id = ?", (new_score, user_id))
    return new_score


def calculate_crowd_priority(conn, issue_id):
    """Calculates crowd priority score for an issue.
    Formula:
      priority_score = base_impact * (1 + log2(1 + sum(min(weight_i, 1.0))))
    Guarantees:
      - Safety issues (P1) remain top priority regardless of crowd size.
      - A single high-trust student cannot push a P4 minor issue past baseline P3 (40.0)
        unless there are at least K=5 distinct credible members.
    """
    c = conn.cursor()
    issue = c.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if not issue:
        return 0.0, ""

    priority = issue["priority"]
    base_impact = PRIORITY_BASE_IMPACT.get(priority, 40.0)

    # Fetch all distinct affected members
    members = c.execute("SELECT user_id FROM memberships WHERE issue_id = ?", (issue_id,)).fetchall()
    distinct_users = [m["user_id"] for m in members]

    capped_weights = []
    weight_details = []
    for uid in distinct_users:
        w = get_user_trust_weight(conn, uid, check_brigading=True, issue_id=issue_id)
        cw = min(w, 1.0)  # Capped at 1.0 per person to prevent single-person dominance
        capped_weights.append(cw)
        weight_details.append(f"{uid[:8]}: raw={w:.2f}, capped={cw:.2f}")

    crowd_sum = sum(capped_weights)
    crowd_factor = 1.0 + math.log2(1.0 + crowd_sum)
    score = base_impact * crowd_factor

    # Minor-issue ceiling (FR-P3):
    # P4 cannot exceed baseline P3 (40.0) unless it has at least 5 distinct members!
    ceiling_applied = False
    if priority == 4:
        if len(distinct_users) < 5 and score > PRIORITY_BASE_IMPACT[3]:
            score = PRIORITY_BASE_IMPACT[3] - 0.1
            ceiling_applied = True

    # P1 Safety floor (FR-P4):
    # Safety issues stay on top (>= 100.0)
    if priority == 1 and score < 100.0:
        score = 100.0

    rank_explanation = {
        "priority_level": f"P{priority}",
        "base_impact": base_impact,
        "distinct_members": len(distinct_users),
        "crowd_sum": round(crowd_sum, 2),
        "crowd_factor": round(crowd_factor, 3),
        "final_score": round(score, 2),
        "ceiling_applied": ceiling_applied,
        "weights": weight_details[:5],
    }

    c.execute("UPDATE issues SET priority_score = ?, base_impact = ? WHERE id = ?",
              (round(score, 2), base_impact, issue_id))
    return round(score, 2), rank_explanation


def check_and_flag_brigading(conn, issue_id):
    """Detects if 4 or more new accounts act within 30 seconds on the same issue."""
    c = conn.cursor()
    now = time.time()
    recent_window = now - 30.0

    # Check recent votes by new users
    recent_new_votes = c.execute("""
        SELECT v.user_id, v.created_at, u.is_new
        FROM votes v
        JOIN users u ON v.user_id = u.id
        WHERE v.issue_id = ? AND v.created_at >= ? AND u.is_new = 1
    """, (issue_id, recent_window)).fetchall()

    if len(recent_new_votes) >= 4:
        # Check if already flagged
        existing = c.execute("SELECT 1 FROM flags WHERE target_type = 'issue' AND target_id = ? AND kind = 'brigading'",
                             (str(issue_id),)).fetchone()
        if not existing:
            detail = f"Brigading detected: {len(recent_new_votes)} new accounts voted within 30 seconds. Vote weights discounted by 80%."
            c.execute("INSERT INTO flags (target_type, target_id, kind, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                      ("issue", str(issue_id), "brigading", detail, now))
            c.execute("INSERT INTO audit_log (actor_id, actor_name, action, target_type, target_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      ("system", "Security Sentinel", "FLAG_BRIGADING", "issue", str(issue_id), detail, now))

            # Penalize brigading accounts
            for r in recent_new_votes:
                update_user_trust(conn, r["user_id"], -10.0, "Brigading detection penalty", issue_id)

            return True
    return False


def evaluate_resolution_voting(conn, issue_id):
    """Evaluates voting results when an issue is in 'Resolved' status.
    Reopen Rule:
      - Distinct disputers >= min(2, affected_count)
      - Disputes sum >= 1.0 (threshold)
      - Disputes sum > Confirms sum
    """
    c = conn.cursor()
    issue = c.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if not issue or issue["status"] != "Resolved":
        return None

    cycle_no = issue["vote_cycle"]
    votes = c.execute("SELECT * FROM votes WHERE issue_id = ? AND cycle_no = ?", (issue_id, cycle_no)).fetchall()
    members = c.execute("SELECT user_id FROM memberships WHERE issue_id = ?", (issue_id,)).fetchall()
    affected_count = len(members)

    check_and_flag_brigading(conn, issue_id)

    dispute_votes = [v for v in votes if v["choice"] == "dispute"]
    confirm_votes = [v for v in votes if v["choice"] == "confirm"]

    # Calculate weighted sums
    disputes_sum = 0.0
    for v in dispute_votes:
        w = get_user_trust_weight(conn, v["user_id"], check_brigading=True, issue_id=issue_id)
        disputes_sum += w

    confirms_sum = 0.0
    for v in confirm_votes:
        w = get_user_trust_weight(conn, v["user_id"], check_brigading=True, issue_id=issue_id)
        confirms_sum += w

    num_disputers = len(dispute_votes)
    min_disputers_needed = min(2, affected_count) if affected_count > 0 else 1

    reopen_triggered = (
        num_disputers >= min_disputers_needed and
        disputes_sum >= 0.8 and
        disputes_sum > confirms_sum
    )

    now = time.time()
    if reopen_triggered:
        reopen_count = issue["reopen_count"] + 1
        new_status = "Reopened"
        c.execute("""
            UPDATE issues SET status = ?, reopen_count = ?, updated_at = ? WHERE id = ?
        """, (new_status, reopen_count, now, issue_id))

        note_text = (f"Resolution disputed and REOPENED by affected students. "
                     f"{num_disputers} disputers (weighted disputes: {disputes_sum:.2f} vs confirms: {confirms_sum:.2f}). "
                     f"Reopen count: {reopen_count}.")
        c.execute("INSERT INTO notes (issue_id, author_id, author_name, text, kind, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (issue_id, "system", "ReportFix Voting System", note_text, "reopen", now))

        # If reopened 3 times, escalate to Admin with SLA flag
        if reopen_count >= 3:
            esc_detail = f"Issue #{issue_id} has been reopened 3 times! Escalated to Administrator for intervention."
            c.execute("INSERT INTO flags (target_type, target_id, kind, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                      ("issue", str(issue_id), "sla_escalation", esc_detail, now))
            c.execute("INSERT INTO audit_log (actor_id, actor_name, action, target_type, target_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      ("system", "Voting Engine", "ESCALATION_ADMIN", "issue", str(issue_id), esc_detail, now))

        calculate_crowd_priority(conn, issue_id)
        return {
            "action": "reopened",
            "disputes_sum": disputes_sum,
            "confirms_sum": confirms_sum,
            "num_disputers": num_disputers,
            "reopen_count": reopen_count
        }

    return {
        "action": "pending_or_confirmed",
        "disputes_sum": disputes_sum,
        "confirms_sum": confirms_sum,
        "num_disputers": num_disputers,
        "min_disputers_needed": min_disputers_needed
    }


def close_issue_vote(conn, issue_id, admin_id="system"):
    """Closes voting and marks issue Closed, rewarding or penalizing voters."""
    c = conn.cursor()
    issue = c.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if not issue or issue["status"] not in ("Resolved", "Reopened"):
        return False

    cycle_no = issue["vote_cycle"]
    votes = c.execute("SELECT * FROM votes WHERE issue_id = ? AND cycle_no = ?", (issue_id, cycle_no)).fetchall()
    now = time.time()

    c.execute("UPDATE issues SET status = 'Closed', closed_at = ?, updated_at = ? WHERE id = ?",
              (now, now, issue_id))

    # Reward confirmers (+2 trust)
    for v in votes:
        if v["choice"] == "confirm":
            update_user_trust(conn, v["user_id"], 2.0, "Vote matched verified resolution", issue_id)
        elif v["choice"] == "dispute":
            update_user_trust(conn, v["user_id"], -2.0, "Dispute not upheld by community", issue_id)

    # Award reporter +3 trust for successful resolved issue
    reporter = c.execute("SELECT user_id FROM memberships WHERE issue_id = ? AND kind = 'reporter'",
                         (issue_id,)).fetchone()
    if reporter:
        update_user_trust(conn, reporter["user_id"], 3.0, "Report successfully resolved and verified", issue_id)

    c.execute("INSERT INTO notes (issue_id, author_id, author_name, text, kind, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (issue_id, admin_id, "System / Admin", "Voting cycle concluded. Issue marked CLOSED.", "status_change", now))
    c.execute("INSERT INTO audit_log (actor_id, actor_name, action, target_type, target_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (admin_id, "Admin/System", "CLOSE_ISSUE", "issue", str(issue_id), "Issue successfully verified and closed", now))
    return True


def get_department_reliability(conn):
    """Computes resolution reliability score per department (confirm vs dispute ratio)."""
    c = conn.cursor()
    depts = ["EE", "CSE", "IS", "Library", "Cafeteria", "Labs"]
    result = []
    for dept in depts:
        total_resolved = c.execute("""
            SELECT COUNT(*) FROM issues WHERE department = ? AND status IN ('Resolved', 'Closed', 'Reopened')
        """, (dept,)).fetchone()[0]

        reopened = c.execute("""
            SELECT COUNT(*) FROM issues WHERE department = ? AND reopen_count > 0
        """, (dept,)).fetchone()[0]

        closed = c.execute("""
            SELECT COUNT(*) FROM issues WHERE department = ? AND status = 'Closed'
        """, (dept,)).fetchone()[0]

        reliability_pct = round(((closed) / total_resolved * 100), 1) if total_resolved > 0 else 100.0
        result.append({
            "department": dept,
            "total_resolved": total_resolved,
            "confirmed_closed": closed,
            "reopened": reopened,
            "reliability_pct": reliability_pct
        })
    return result


def get_admin_summary(conn):
    """Generates structured executive summary of campus health."""
    c = conn.cursor()
    total_open = c.execute("SELECT COUNT(*) FROM issues WHERE status NOT IN ('Closed') AND confidential = 0").fetchone()[0]
    p1_count = c.execute("SELECT COUNT(*) FROM issues WHERE priority = 1 AND status NOT IN ('Closed') AND confidential = 0").fetchone()[0]
    conf_count = c.execute("SELECT COUNT(*) FROM issues WHERE confidential = 1").fetchone()[0]
    flagged_count = c.execute("SELECT COUNT(*) FROM flags").fetchone()[0]
    dept_stats = get_department_reliability(conn)

    # Hotspots: places with multiple issues
    hotspots = c.execute("""
        SELECT place, COUNT(*) as count
        FROM issues
        WHERE confidential = 0
        GROUP BY place
        HAVING count >= 2
        ORDER BY count DESC
        LIMIT 5
    """).fetchall()

    hotspots_list = [{"place": h["place"], "issue_count": h["count"]} for h in hotspots]

    return {
        "total_active_issues": total_open,
        "critical_safety_hazards": p1_count,
        "confidential_welfare_reports": conf_count,
        "security_and_brigading_flags": flagged_count,
        "department_reliability": dept_stats,
        "campus_hotspots": hotspots_list,
        "summary_text": (
            f"Campus Status: {total_open} active non-confidential issues ({p1_count} critical safety hazards). "
            f"{conf_count} confidential welfare reports safely isolated in admin queue. "
            f"{'Warning: Active hazards require immediate attention.' if p1_count > 0 else 'All high-priority systems normal.'}"
        )
    }
