"""Tiny database layer shared by the scraper and the viewer.

Production: Cloudflare D1 through its REST API (needs CF_ACCOUNT_ID, CF_D1_DATABASE_ID, CF_API_TOKEN).
Local dev/tests: set LOCAL_SQLITE_PATH=some.db and the same SQL runs against plain SQLite
(D1 *is* SQLite, so behaviour matches).
"""
import os
import sqlite3
import time

import requests


class D1Error(RuntimeError):
    pass


class D1:
    def __init__(self, account_id, database_id, api_token, timeout=60):
        self.url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/d1/database/{database_id}/query"
        )
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {api_token}"})
        self.timeout = timeout

    def _post(self, body):
        for attempt in range(5):
            try:
                r = self.s.post(self.url, json=body, timeout=self.timeout)
            except requests.RequestException as e:
                err = str(e)
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    err = f"HTTP {r.status_code}: {r.text[:300]}"
                else:
                    data = r.json()
                    if not data.get("success"):
                        raise D1Error(str(data.get("errors") or data)[:1000])
                    return data["result"]
            time.sleep(2 ** attempt)
        raise D1Error(f"D1 request failed after retries: {err}")

    def query(self, sql, params=None):
        """Run ONE statement (optionally with ? params) and return rows as dicts."""
        body = {"sql": sql}
        if params:
            body["params"] = list(params)
        result = self._post(body)
        return result[0].get("results", []) if result else []

    def script(self, sql):
        """Run several ;-separated statements with literal values (no params)."""
        self._post({"sql": sql})


class LocalSQLite:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def query(self, sql, params=None):
        cur = self.conn.execute(sql, list(params or []))
        rows = [dict(r) for r in cur.fetchall()]
        self.conn.commit()
        return rows

    def script(self, sql):
        self.conn.executescript(sql)
        self.conn.commit()


def get_db():
    local = os.getenv("LOCAL_SQLITE_PATH")
    if local:
        return LocalSQLite(local)
    missing = [k for k in ("CF_ACCOUNT_ID", "CF_D1_DATABASE_ID", "CF_API_TOKEN") if not os.getenv(k)]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")
    return D1(os.environ["CF_ACCOUNT_ID"], os.environ["CF_D1_DATABASE_ID"], os.environ["CF_API_TOKEN"])


def lit(v):
    """SQLite literal for building batched multi-statement SQL safely."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("\x00", "")
    return "'" + s.replace("'", "''") + "'"
