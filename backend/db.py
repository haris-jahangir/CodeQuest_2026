"""SQLite Database Manager for ReportFix.
Supports WAL mode, schema initialization, seeding, and clean resets.
"""
import os
import sqlite3
import time
import json

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reportfix.db")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        department TEXT,
        trust_score REAL DEFAULT 50.0,
        is_new INTEGER DEFAULT 0,
        created_at REAL NOT NULL
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS places (
        id TEXT PRIMARY KEY,
        canonical_name TEXT NOT NULL,
        owner_dept TEXT NOT NULL
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        place TEXT NOT NULL,
        issue_type TEXT NOT NULL,
        department TEXT NOT NULL,
        priority INTEGER NOT NULL,
        priority_score REAL NOT NULL,
        base_impact REAL NOT NULL,
        reason TEXT NOT NULL,
        status TEXT NOT NULL,
        confidential INTEGER DEFAULT 0,
        injection_flagged INTEGER DEFAULT 0,
        reopen_count INTEGER DEFAULT 0,
        vote_cycle INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        resolved_at REAL,
        closed_at REAL,
        triage_explanation TEXT NOT NULL,
        recurrence_id INTEGER DEFAULT NULL
    )""")

    c.execute("CREATE INDEX IF NOT EXISTS idx_issues_dept ON issues(department);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_issues_place_type ON issues(place, issue_type, status);")

    c.execute("""
    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id INTEGER NOT NULL,
        user_id TEXT NOT NULL,
        raw_text TEXT NOT NULL,
        normalized_text TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY(issue_id) REFERENCES issues(id) ON DELETE CASCADE
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS memberships (
        issue_id INTEGER NOT NULL,
        user_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        joined_at REAL NOT NULL,
        PRIMARY KEY (issue_id, user_id),
        FOREIGN KEY(issue_id) REFERENCES issues(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id INTEGER NOT NULL,
        user_id TEXT NOT NULL,
        cycle_no INTEGER NOT NULL,
        choice TEXT NOT NULL,
        weight REAL NOT NULL,
        comment TEXT,
        created_at REAL NOT NULL,
        UNIQUE (issue_id, user_id, cycle_no),
        FOREIGN KEY(issue_id) REFERENCES issues(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS trust_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        delta REAL NOT NULL,
        reason TEXT NOT NULL,
        issue_id INTEGER,
        created_at REAL NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id INTEGER NOT NULL,
        author_id TEXT NOT NULL,
        author_name TEXT NOT NULL,
        text TEXT NOT NULL,
        kind TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY(issue_id) REFERENCES issues(id) ON DELETE CASCADE
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_id TEXT NOT NULL,
        actor_name TEXT NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_at REAL NOT NULL
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS flags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_at REAL NOT NULL
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS confidential_reveals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_id INTEGER NOT NULL,
        admin_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at REAL NOT NULL,
        FOREIGN KEY(issue_id) REFERENCES issues(id) ON DELETE CASCADE
    )""")

    conn.commit()
    conn.close()


def seed_data(reset=False):
    init_db()
    conn = get_db()
    c = conn.cursor()

    if reset:
        c.execute("DELETE FROM confidential_reveals;")
        c.execute("DELETE FROM flags;")
        c.execute("DELETE FROM audit_log;")
        c.execute("DELETE FROM notes;")
        c.execute("DELETE FROM trust_ledger;")
        c.execute("DELETE FROM votes;")
        c.execute("DELETE FROM memberships;")
        c.execute("DELETE FROM reports;")
        c.execute("DELETE FROM issues;")
        c.execute("DELETE FROM places;")
        c.execute("DELETE FROM users;")

    # Check if users already seeded
    existing = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    now = time.time()

    if existing == 0:
        users = [
            ("s1", "Ali (Student, 50)", "student", None, 50.0, 0),
            ("s2", "Bilal (Student, 50)", "student", None, 50.0, 0),
            ("s3", "Zainab (High Trust, 85)", "student", None, 85.0, 0),
            ("s4", "Hamza (Low Trust, 25)", "student", None, 25.0, 0),
            ("s5", "Sara (New, 50)", "student", None, 50.0, 1),
            ("student_ali", "Ali Khan (Student)", "student", None, 50.0, 0),
            ("student_bilal", "Bilal Ahmed (Student)", "student", None, 50.0, 0),
            ("student_zainab", "Zainab Fatima (High Trust)", "student", None, 85.0, 0),
            ("student_hamza", "Hamza Tariq (Low Trust)", "student", None, 25.0, 0),
            ("student_sara", "Sara Noor (New Account)", "student", None, 50.0, 1),
            ("staff_cse", "Engr. Asad (CSE Staff)", "staff", "CSE", 75.0, 0),
            ("staff_ee", "Engr. Bilal (EE Staff)", "staff", "EE", 75.0, 0),
            ("staff_is", "Engr. Fahad (IS Staff)", "staff", "IS", 75.0, 0),
            ("staff_lib", "Ms. Nabila (Library Staff)", "staff", "Library", 75.0, 0),
            ("staff_cafe", "Chef Rasheed (Cafeteria Staff)", "staff", "Cafeteria", 75.0, 0),
            ("staff_labs", "Tariq Sahab (Labs Staff)", "staff", "Labs", 75.0, 0),
            ("admin_user", "Campus Administrator", "admin", None, 99.0, 0),
            ("admin", "Campus Administrator", "admin", None, 99.0, 0),
        ]
        for uid, name, role, dept, trust, is_new in users:
            c.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (uid, name, role, dept, trust, is_new, now))

        # Seed initial trust ledgers
        for uid, name, role, dept, trust, is_new in users:
            if role == "student":
                c.execute("INSERT INTO trust_ledger (user_id, delta, reason, issue_id, created_at) VALUES (?, ?, ?, ?, ?)",
                          (uid, trust - 50.0 if trust != 50.0 else 0.0, "Initial baseline trust", None, now))

        # Seed places
        places = [
            ("Lab 1", "Lab 1", "Labs"),
            ("Lab 2", "Lab 2", "Labs"),
            ("Lab 3", "Lab 3", "Labs"),
            ("Lab 4", "Lab 4", "Labs"),
            ("Lab 5", "Lab 5", "Labs"),
            ("CSE Block", "CSE Block", "CSE"),
            ("EE Block", "EE Block", "EE"),
            ("IS Block", "IS Block", "IS"),
            ("Library", "Library", "Library"),
            ("Cafeteria", "Cafeteria", "Cafeteria"),
            ("Admin Block", "Admin Block", "IS"),
            ("Main Gate", "Main Gate", "EE"),
            ("Hostel", "Hostel", "EE"),
        ]
        for pid, name, odept in places:
            c.execute("INSERT INTO places VALUES (?, ?, ?)", (pid, name, odept))

        # Places seeded, zero dummy issues
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    seed_data(reset=True)
    print("Database initialized and seeded successfully.")
