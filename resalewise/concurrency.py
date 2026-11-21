"""Two transactions on two sample bank accounts, run for real.

Alice and Bob start with 200 each (table concurrency_accounts, nothing else uses it).
T1 moves 100 from Alice to Bob and T2 adds 3% interest to both. One after the other, in either
order, they end with 412 between them (103 + 309 or 106 + 306).

Each scenario sends statements on separate database sessions and records what PostgreSQL
actually did: who waited, which job a deadlock killed, what a savepoint kept. Every line of the
timeline is a real statement and every error is the database's own message.
"""
import re
import threading
import time
from decimal import Decimal

import psycopg2

from . import config
from .web import ApiError

START = 200
READ_A  = "SELECT balance FROM concurrency_accounts WHERE name = 'Alice'"
A_MINUS = "UPDATE concurrency_accounts SET balance = balance - 100 WHERE name = 'Alice'"
B_PLUS  = "UPDATE concurrency_accounts SET balance = balance + 100 WHERE name = 'Bob'"
A_INT   = "UPDATE concurrency_accounts SET balance = balance * 1.03 WHERE name = 'Alice'"
B_INT   = "UPDATE concurrency_accounts SET balance = balance * 1.03 WHERE name = 'Bob'"
RESET_SQL = "UPDATE concurrency_accounts SET balance = 200"
SERIAL_OUTCOMES = [(103, 309), (106, 306)]      # T1 then T2, T2 then T1
_RUN_LOCK = 742032                              # advisory lock: one run at a time, across workers


class Trial:
    """One run: its sessions and what happened on them."""

    def __init__(self):
        self.steps = []
        self._lock = threading.Lock()
        self._conns = []
        # three sessions at once: T1 and T2 do the work, the observer resets the accounts,
        # reads balances and checks who is waiting
        opened, errors = {}, []

        def connect(name):
            try:
                opened[name] = self._session()
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=connect, args=(n,), daemon=True) for n in ("t1", "t2", "observer")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self._conns = list(opened.values())
        if errors:
            self.close()
            raise errors[0]
        self.t1, self.t2, self.observer = opened["t1"], opened["t2"], opened["observer"]
        self.t0 = time.perf_counter()

    @staticmethod
    def _session():
        conn = psycopg2.connect(config.database_url(), connect_timeout=10, application_name="resalewise-concurrency")
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 8000")     # no session can hang for long
            cur.execute("SET lock_timeout = 6000")
        return conn

    def close(self):
        for conn in self._conns:
            try:
                conn.close()
            except Exception:
                pass

    # timeline
    def note(self, who, text, status="info", sql="", at=None, issued=None, blocked=False):
        at = time.perf_counter() if at is None else at
        with self._lock:
            self.steps.append({"at": at, "issued": at if issued is None else issued, "ms": round((at - self.t0) * 1000),
                               "who": who, "sql": sql, "note": text, "status": status, "blocked": blocked})

    def timeline(self):
        """Steps in the order they took effect. A statement that ran at once goes where it was sent,
        one that had to wait goes where it finally got through (otherwise a COMMIT could land after
        the statement it unblocked, the two answers arrive in the same millisecond)."""
        return [{k: v for k, v in s.items() if k not in ("at", "issued", "blocked")} for s in sorted(self.steps, key=lambda s: s["at"])]

    def script(self):
        """The statements in the order they were sent, for redoing the run by hand in two terminals.
        `expect` says what each one printed, or that it hung."""
        out = []
        for s in sorted((s for s in self.steps if s["sql"] and s["status"] != "waiting"), key=lambda s: s["issued"]):
            expect = re.sub(r"goes through after waiting [\d,]+ ms", "goes through", s["note"])
            if s["blocked"]:
                expect = ("This terminal hangs, waiting for a lock: carry on in the other terminal. "
                          + ("Within about a second it prints: " if s["status"] == "error" else "Once the lock is released it ") + expect)
            out.append({"session": s["who"], "sql": s["sql"], "expect": expect, "failed": s["status"] == "error"})
        return out

    def run(self, who, conn, sql, say="", blocked=False):
        """Send one statement, record it, return (ok, first value or error text)."""
        began = time.perf_counter()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                value = cur.fetchone()[0] if cur.description else None
        except psycopg2.Error as e:
            diag = getattr(e, "diag", None)
            text = (getattr(diag, "message_primary", None) or str(e).strip().splitlines()[0])
            detail = getattr(diag, "message_detail", None)
            if detail:
                text += " — " + detail.splitlines()[0]
            self.note(who, "ERROR: " + text, "error", sql, at=None if blocked else began, issued=began, blocked=blocked)
            return False, text
        waited = (time.perf_counter() - began) * 1000
        parts = []
        if value is not None:
            parts.append(f"returns {value}")
        if blocked:
            parts.append(f"goes through after waiting {waited:,.0f} ms")
        if say:
            parts.append(say)
        self.note(who, "; ".join(parts), "ok", sql, at=None if blocked else began, issued=began, blocked=blocked)
        return True, value

    def run_blocked(self, who, conn, sql, held_by):
        """Send a statement that should wait for a lock. It runs on its own thread while this one
        asks pg_stat_activity whether the session really is waiting."""
        pid = conn.get_backend_pid()
        out = {}

        def work():
            out["ok"], out["value"] = self.run(who, conn, sql, blocked=True)
        thread = threading.Thread(target=work, daemon=True, name="concurrency")
        thread.start()
        deadline = time.time() + 2
        while thread.is_alive() and time.time() < deadline:
            with self.observer.cursor() as cur:
                cur.execute("SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s", [pid])
                row = cur.fetchone()
            if row and row[0] == "Lock":
                self.note(who, f"BLOCKED: waiting for the row lock {held_by} holds "
                               f"(pg_stat_activity says: {row[0]} / {row[1]})", "waiting", sql)
                break
            time.sleep(0.02)

        def join():
            thread.join(timeout=12)
            return out.get("ok", False)
        return join

    # accounts
    def reset(self, record=False):
        if record:
            self.note("", "Both accounts are set back to 200.", "ok", RESET_SQL)
        with self.observer.cursor() as cur:
            cur.execute("INSERT INTO concurrency_accounts (name, balance) VALUES ('Alice', %s), ('Bob', %s) "
                        "ON CONFLICT (name) DO UPDATE SET balance = EXCLUDED.balance", [START, START])

    def balances(self):
        with self.observer.cursor() as cur:
            cur.execute("SELECT name, balance FROM concurrency_accounts WHERE name IN ('Alice', 'Bob')")
            got = dict(cur.fetchall())
        return {"Alice": float(got.get("Alice", 0)), "Bob": float(got.get("Bob", 0))}

    def whole(self, who, conn, statements, begin="BEGIN"):
        """A whole transaction run with nothing in its way."""
        self.run(who, conn, begin)
        for sql in statements:
            self.run(who, conn, sql)
        self.run(who, conn, "COMMIT")


# the scenarios
def _serial(trial):
    t1, t2 = trial.t1, trial.t2
    trial.whole("T1", t1, [A_MINUS, B_PLUS])
    trial.whole("T2", t2, [A_INT, B_INT])


def _interleaved(trial):
    t1, t2 = trial.t1, trial.t2
    trial.note("", "No BEGIN, no COMMIT: every statement is saved the moment it runs, so the two jobs can mix freely.")
    trial.run("T1", t1, A_MINUS)
    trial.run("T2", t2, A_INT)
    trial.run("T2", t2, B_INT, say="Bob gets interest on 200, before T1's 100 arrives")
    trial.run("T1", t1, B_PLUS)


def _locked(trial):
    t1, t2 = trial.t1, trial.t2
    trial.note("", "The same order of statements is attempted, but each job is now one transaction.")
    trial.run("T1", t1, "BEGIN")
    trial.run("T1", t1, A_MINUS, say="T1 now holds the lock on Alice's row until it commits")
    trial.run("T2", t2, "BEGIN")
    wait = trial.run_blocked("T2", t2, A_INT, held_by="T1")
    trial.run("T1", t1, B_PLUS)
    trial.run("T1", t1, "COMMIT", say="the lock on Alice's row is released")
    wait()
    trial.run("T2", t2, B_INT)
    trial.run("T2", t2, "COMMIT")


def _deadlock(trial):
    t1, t2 = trial.t1, trial.t2
    trial.run("T1", t1, "BEGIN")
    trial.run("T1", t1, A_MINUS, say="T1 locks Alice's row")
    trial.run("T2", t2, "BEGIN")
    trial.run("T2", t2, B_INT, say="T2 locks Bob's row")
    wait = trial.run_blocked("T1", t1, B_PLUS, held_by="T2")
    trial.note("", "T2 now asks for Alice's row, which T1 holds: each waits for the other. "
                 "PostgreSQL checks for this after deadlock_timeout (1 second).")
    ok2, _ = trial.run("T2", t2, A_INT, blocked=True)
    ok1 = wait()
    victim, v_conn, v_job = ("T2", t2, [A_INT, B_INT]) if not ok2 else ("T1", t1, [A_MINUS, B_PLUS])
    survivor, s_conn = ("T1", t1) if victim == "T2" else ("T2", t2)
    if ok1 and ok2:                                         # the database let both through: nothing to undo
        trial.run("T1", t1, "COMMIT")
        trial.run("T2", t2, "COMMIT")
        return
    # the survivor only moved after the victim was aborted, but both answers reach us at the same
    # instant, so put the abort first in the timeline
    with trial._lock:
        failed = next((s for s in reversed(trial.steps) if s["who"] == victim and s["status"] == "error"), None)
        went = next((s for s in reversed(trial.steps) if s["who"] == survivor and s["status"] == "ok"), None)
        if failed and went and failed["at"] >= went["at"]:
            failed["at"] = went["at"] - 1e-6
            failed["ms"] = went["ms"]
    trial.run(victim, v_conn, "ROLLBACK", say=f"{victim} was chosen as the victim: everything it did is undone")
    trial.run(survivor, s_conn, "COMMIT")
    trial.note("", f"The application does what it must after a deadlock: it runs {victim} again.")
    trial.whole(victim, v_conn, v_job)


def _deadlock_by_hand(script):
    """By hand the two lock requests are seconds apart, and then PostgreSQL aborts the other
    session than it did in the fast run above. So don't name a victim, say to follow
    whichever terminal prints the error."""
    first = list(script[:6])                                   # BEGIN, hold, BEGIN, hold, then the two requests
    first[4] = dict(first[4], expect="This terminal hangs, waiting for Terminal 2's lock on Bob's row. Carry on with the next step.")
    first[5] = dict(first[5], expect="Now each terminal waits for the other. Within about a second PostgreSQL stops one of them: "
                                     "it prints ERROR: deadlock detected, and the other one goes through. Which one depends on your timing; either is correct.")
    def again(a, b):
        return "\n".join(["BEGIN;", a + ";", b + ";", "COMMIT"])
    return first + [
        {"session": "", "sql": "ROLLBACK", "failed": False, "label": "In the terminal that printed the error",
         "expect": "Its work is undone. Only one of the two jobs is lost, never both."},
        {"session": "", "sql": "COMMIT", "failed": False, "label": "In the other terminal",
         "expect": "Its last update has gone through; this makes it permanent."},
        {"session": "T1", "sql": again(A_MINUS, B_PLUS), "failed": False, "label": "If Terminal 1 printed the error: run its transfer again there",
         "expect": ""},
        {"session": "T2", "sql": again(A_INT, B_INT), "failed": False, "label": "If Terminal 2 printed the error: run its interest again there",
         "expect": ""},
    ]


def _savepoint(trial):
    t1 = trial.t1
    trial.run("T1", t1, "BEGIN")
    trial.run("T1", t1, A_MINUS)
    trial.run("T1", t1, "SAVEPOINT before_deposit")
    trial.run("T1", t1, "UPDATE concurrency_accounts SET balance = balance + 1000 WHERE name = 'Bob'",
            say="a slip: 1000 instead of 100")
    trial.run("T1", t1, "ROLLBACK TO SAVEPOINT before_deposit", say="only the slip is undone; Alice's withdrawal stays")
    trial.run("T1", t1, B_PLUS)
    trial.run("T1", t1, "COMMIT")


def _read_then_write(trial, isolation):
    """Both jobs read Alice's balance, work out the new figure in the app, then write it."""
    t1, t2 = trial.t1, trial.t2
    begin = f"BEGIN ISOLATION LEVEL {isolation}"
    trial.run("T1", t1, begin)
    _, w1 = trial.run("T1", t1, READ_A)
    trial.run("T2", t2, begin)
    _, w2 = trial.run("T2", t2, READ_A, say="T2 reads the same 200")
    trial.run("T1", t1, f"UPDATE concurrency_accounts SET balance = {Decimal(w1) - 100:.2f} WHERE name = 'Alice'",
            say="200 − 100, worked out by the application")
    trial.run("T1", t1, "COMMIT")
    ok, _ = trial.run("T2", t2, f"UPDATE concurrency_accounts SET balance = {Decimal(w2) * Decimal('1.03'):.2f} WHERE name = 'Alice'",
                    say="200 × 1.03, from the balance T2 read earlier")
    if ok:
        trial.run("T2", t2, "COMMIT")
        return
    trial.run("T2", t2, "ROLLBACK")
    trial.note("", "Optimistic control: nobody waited, the clash was caught at write time. T2 simply tries again.")
    trial.run("T2", t2, begin)
    _, w2 = trial.run("T2", t2, READ_A, say="this time T2 sees T1's result")
    trial.run("T2", t2, f"UPDATE concurrency_accounts SET balance = {Decimal(w2) * Decimal('1.03'):.2f} WHERE name = 'Alice'")
    trial.run("T2", t2, "COMMIT")


def _atomicity(trial):
    t1 = trial.t1
    deposit = "UPDATE concurrency_accounts SET balance = balance + 500 WHERE name = 'Bob'"
    withdraw = "UPDATE concurrency_accounts SET balance = balance - 500 WHERE name = 'Alice'"
    trial.note("", "A transfer of 500, more than Alice has. First without a transaction.")
    trial.run("T1", t1, deposit)
    trial.run("T1", t1, withdraw)
    b = trial.balances()
    trial.note("", f"Alice {b['Alice']:.0f}, Bob {b['Bob']:.0f}: the deposit stayed, the withdrawal was refused. "
                 "500 dollars came from nowhere.", "error")
    trial.reset(record=True)
    trial.note("", "The same two statements again, inside one transaction.")
    trial.run("T1", t1, "BEGIN")
    trial.run("T1", t1, deposit)
    trial.run("T1", t1, withdraw)
    trial.run("T1", t1, "ROLLBACK", say="all or nothing: the deposit is undone too")


SCENARIOS = {
    "serial": {
        "title": "Serial schedule", "idea": "Scheduling", "run": _serial, "valid": SERIAL_OUTCOMES,
        "what": "T1 finishes, then T2 starts. The safest schedule, and the answer every other schedule is judged against."},
    "interleaved": {
        "title": "Interleaving without transactions", "idea": "Why interleaving goes wrong", "run": _interleaved,
        "valid": SERIAL_OUTCOMES,
        "what": "T2's interest on Bob's account lands between T1's two steps. Three dollars go missing."},
    "locked": {
        "title": "The same order, with transactions", "idea": "Isolation by locking", "run": _locked,
        "valid": SERIAL_OUTCOMES,
        "what": "Each job is wrapped in BEGIN … COMMIT. T2 has to wait for T1's row lock, so the bad order cannot happen."},
    "deadlock": {
        "title": "Deadlock", "idea": "Two jobs stuck on each other", "run": _deadlock, "valid": SERIAL_OUTCOMES,
        "by_hand": _deadlock_by_hand,
        "end_note": "Whichever terminal was the victim, the two accounts end at 412 between them: Alice 103 and Bob 309, or Alice 106 and Bob 306.",
        "what": "T1 locks Alice then wants Bob; T2 locks Bob then wants Alice. PostgreSQL detects the cycle and aborts one of them."},
    "lost_update": {
        "title": "Lost update", "idea": "Read, think, write", "run": lambda trial: _read_then_write(trial, "READ COMMITTED"),
        "valid": [(103, 200), (106, 200)],
        "what": "Both jobs read Alice's 200, then each writes its own answer. The second write wipes out the first."},
    "optimistic": {
        "title": "Optimistic control", "idea": "No waiting, retry on a clash", "run": lambda trial: _read_then_write(trial, "REPEATABLE READ"),
        "valid": [(103, 200), (106, 200)],
        "what": "The same clash under REPEATABLE READ: no locks are waited for, the database refuses the stale write, and T2 retries."},
    "savepoint": {
        "title": "Savepoint", "idea": "Undo part of a transaction", "run": _savepoint, "valid": [(100, 300)],
        "what": "A mistake halfway through a transaction is rolled back to a savepoint; the work before it is kept."},
    "atomicity": {
        "title": "Atomicity", "idea": "A transfer that cannot finish", "run": _atomicity, "valid": [(200, 200)],
        "what": "A transfer larger than the balance fails halfway. Without a transaction the half that worked stays; with one, nothing does."},
}


def catalogue():
    return [{"key": k, "title": s["title"], "idea": s["idea"], "what": s["what"]} for k, s in SCENARIOS.items()]


def run(key):
    """Run one scenario from a clean start and return the timeline and final balances."""
    scenario = SCENARIOS.get(key)
    if not scenario:
        raise ApiError("Unknown scenario.", 400, "validation_error")
    trial = Trial()
    try:
        with trial.observer.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", [_RUN_LOCK])
            if not cur.fetchone()[0]:
                raise ApiError("Another run is in progress. Try again in a few seconds.", 409, "busy")
        trial.reset()
        start = trial.balances()
        trial.t0 = time.perf_counter()                 # the clock starts with the first statement
        scenario["run"](trial)
        end = trial.balances()
        took = round((time.perf_counter() - trial.t0) * 1000)
    finally:
        trial.close()                                  # closing the observer releases the advisory lock
    got = (round(end["Alice"], 2), round(end["Bob"], 2))
    return {"key": key, "title": scenario["title"], "idea": scenario["idea"], "what": scenario["what"],
            "start": start, "end": end, "total": round(end["Alice"] + end["Bob"], 2),
            "correct": any(got == (float(a), float(b)) for a, b in scenario["valid"]),
            "valid": [{"Alice": a, "Bob": b} for a, b in scenario["valid"]],
            "steps": trial.timeline(), "took_ms": took,
            "terminal": {"setup": RESET_SQL, "steps": scenario.get("by_hand", list)(trial.script()),
                         "end_note": scenario.get("end_note", "")}}
