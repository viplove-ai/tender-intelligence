from __future__ import annotations

import base64
import binascii
import hashlib

from .persistence import IS_WASM

# streamlit-js-eval isn't installable under Pyodide, and this module's WASM-only
# consumers (database.py's restore/validate helpers) never touch streamlit_js_eval, so
# only server mode needs to import it.
if not IS_WASM:
    from streamlit_js_eval import streamlit_js_eval

DB_NAME = "tender_intelligence"
STORE_NAME = "kv"
RECORD_KEY = "db_b64"
SQLITE_HEADER = b"SQLite format 3\x00"
# Base64 adds roughly 33%, keeping this below Streamlit's default 200 MB
# WebSocket message limit while leaving ample room for many imported reports.
MAX_DATABASE_BYTES = 128 * 1024 * 1024
MAX_ENCODED_DATABASE_CHARS = ((MAX_DATABASE_BYTES + 2) // 3) * 4


class BrowserDatabaseError(ValueError):
    """The browser returned a database payload that is unsafe to load."""


def validate_database_bytes(data: bytes) -> bytes:
    """Validate decoded SQLite bytes before writing them to server storage."""
    if not isinstance(data, bytes):
        raise BrowserDatabaseError("The saved browser database has an invalid format.")
    if len(data) > MAX_DATABASE_BYTES:
        raise BrowserDatabaseError("The saved browser database exceeds the 128 MB safety limit.")
    if not data.startswith(SQLITE_HEADER):
        raise BrowserDatabaseError("The saved browser data is not a SQLite database.")
    return data


def decode_database_payload(encoded: str) -> bytes:
    """Strictly decode and validate a browser-supplied SQLite payload."""
    if not isinstance(encoded, str):
        raise BrowserDatabaseError("The saved browser database has an invalid format.")
    if len(encoded) > MAX_ENCODED_DATABASE_CHARS:
        raise BrowserDatabaseError("The saved browser database exceeds the 128 MB safety limit.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BrowserDatabaseError("The saved browser database is not valid base64 data.") from exc
    return validate_database_bytes(data)

_OPEN_STORE_JS = f"""
new Promise((resolve, reject) => {{
  const req = indexedDB.open('{DB_NAME}', 1);
  req.onupgradeneeded = () => {{ if (!req.result.objectStoreNames.contains('{STORE_NAME}')) req.result.createObjectStore('{STORE_NAME}'); }};
  req.onsuccess = () => resolve(req.result);
  req.onerror = () => reject(req.error);
}})
"""

_LOAD_JS = f"""
(async () => {{
  try {{
    const db = await ({_OPEN_STORE_JS});
    const tx = db.transaction('{STORE_NAME}', 'readonly');
    const value = await new Promise((resolve, reject) => {{
      const r = tx.objectStore('{STORE_NAME}').get('{RECORD_KEY}');
      r.onsuccess = () => resolve(r.result || null);
      r.onerror = () => reject(r.error);
    }});
    return value;
  }} catch (e) {{ return null; }}
}})()
"""


def load_db_bytes_from_browser() -> bytes | None:
    """Read the saved SQLite file (base64) from IndexedDB.

    Returns None both when nothing is saved yet and when the async round trip to the
    browser component hasn't completed on this rerun — callers distinguish the two with
    a one-shot "attempted" flag before treating a None as a confirmed-empty database.
    """
    encoded = streamlit_js_eval(js_expressions=_LOAD_JS, key="browser_sync_load", want_output=True)
    if not encoded:
        return None
    return decode_database_payload(encoded)


def save_db_bytes_to_browser(data: bytes) -> None:
    """Persist the current session's SQLite file (base64) into IndexedDB."""
    validate_database_bytes(data)
    encoded = base64.b64encode(data).decode("ascii")
    save_js = f"""
(async () => {{
  try {{
    const db = await ({_OPEN_STORE_JS});
    const tx = db.transaction('{STORE_NAME}', 'readwrite');
    await new Promise((resolve, reject) => {{
      const r = tx.objectStore('{STORE_NAME}').put('{encoded}', '{RECORD_KEY}');
      r.onsuccess = () => resolve(true);
      r.onerror = () => reject(r.error);
    }});
    return true;
  }} catch (e) {{ return false; }}
}})()
"""
    # Vary the component key with the payload hash so each distinct save actually
    # re-triggers the JS evaluation (the component only re-runs when its expression changes).
    digest = hashlib.sha256(data).hexdigest()[:16]
    streamlit_js_eval(js_expressions=save_js, key=f"browser_sync_save_{digest}", want_output=True)
