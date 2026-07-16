from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.io as pio
import streamlit as st

from src.analysis_store import delete_tender_analysis, get_tender_analysis, list_tender_analyses, save_tender_analysis
from src.analytics import contractor_metrics, dashboard_metrics, filter_tenders, load_tenders, sort_tender_details_newest_first
from src.boq_costing import (
    calculate_boq_costing, category_summary, costing_category_key, pareto_items, prepare_boq_items,
    reconcile_boq_items,
)
from src.browser_sync import (
    BrowserDatabaseError,
    load_db_bytes_from_browser,
    save_db_bytes_to_browser,
)
from src.classifier import CATEGORIES, classify_work_type
from src.cleaning import currency_scale, format_inr, format_inr_compact, format_variance
from src.corrections import delete_tenders_by_filters
from src.database import initialize_database, reset_database, restore_database_bytes, set_session_db_path
from src.exporter import build_database_backup
from src.guarantee import calculate_performance_guarantee
from src.importer import commit_import_batch, prepare_import_batch
from src.nit_parser import NITExtraction, extract_nit_pdf
from src.persistence import IS_WASM, resolve_data_dir
from src.persistence import sync_after_write as _sync_after_write_shim
from src.prediction import estimate_bid
from src.utils import configure_logging


st.set_page_config(page_title="Tender Intelligence System", page_icon="🏛️", layout="wide")
configure_logging()
LOG = logging.getLogger(__name__)


def _render_full_screen_loader(message: str) -> None:
    """A loading indicator centered over the whole viewport, not just inline text."""
    st.html(
        f"""
        <div style="position:fixed;inset:0;z-index:9999;display:flex;flex-direction:column;
                     align-items:center;justify-content:center;gap:16px;background:#f4f6f7ee;">
          <div style="width:48px;height:48px;border-radius:50%;border:5px solid #cfe3e8;
                       border-top-color:#125b6c;animation:ti-center-spin 0.8s linear infinite;"></div>
          <div style="font-size:1.05rem;color:#0e4a58;font-weight:500;">{html.escape(message)}</div>
        </div>
        <style>@keyframes ti-center-spin {{ to {{ transform:rotate(360deg); }} }}</style>
        """
    )


@contextmanager
def centered_loader(message: str):
    """Show a screen-centered loader for the duration of a block, then clear it."""
    placeholder = st.empty()
    with placeholder.container():
        _render_full_screen_loader(message)
    try:
        yield
    finally:
        placeholder.empty()


def _bootstrap_session_database() -> None:
    """Hydrate this browser session's private SQLite file from IndexedDB.

    Each visitor gets an isolated temp file — never shared across sessions/users on the
    same Streamlit server process. The browser round trip is asynchronous, so the first
    script run shows a loading state and stops; the component's reply triggers Streamlit
    to rerun the script automatically, at which point the data (if any) is ready.
    """
    if "session_db_path" not in st.session_state:
        # Keeping the TemporaryDirectory object in Session State ties cleanup to
        # the lifetime of this Streamlit session instead of leaking mkdtemp paths.
        session_dir = tempfile.TemporaryDirectory(prefix="tender_session_", ignore_cleanup_errors=True)
        st.session_state["_session_db_tempdir"] = session_dir
        st.session_state["session_db_path"] = Path(session_dir.name) / "tender_intelligence.db"
    set_session_db_path(st.session_state["session_db_path"])
    if st.session_state.get("session_db_ready"):
        return
    try:
        loaded = load_db_bytes_from_browser()
    except BrowserDatabaseError:
        LOG.warning("Rejected an invalid or oversized browser database", exc_info=True)
        st.session_state["session_db_corrupt_warning"] = True
        st.session_state["session_db_ready"] = True
        # Render page content on a clean run. The zero-content browser component still
        # owns an iframe on this run and would otherwise leave a gap above the page title.
        st.rerun()
    if loaded:
        db_path = st.session_state["session_db_path"]
        db_path.write_bytes(loaded)
        try:
            initialize_database(db_path)
        except sqlite3.DatabaseError:
            # The saved bytes were corrupt (interrupted write, browser storage eviction,
            # a stale schema, etc.) — don't let a bad IndexedDB payload crash the whole
            # app. Discard it and continue with a fresh database instead.
            LOG.exception("Saved browser database was unreadable; starting fresh")
            db_path.unlink(missing_ok=True)
            st.session_state["session_db_corrupt_warning"] = True
        st.session_state["session_db_ready"] = True
        st.rerun()
    if not st.session_state.get("session_db_load_attempted"):
        st.session_state["session_db_load_attempted"] = True
        _render_full_screen_loader("Loading your saved data…")
        st.stop()
    # A second attempt with nothing returned means the browser genuinely has no saved
    # database yet, rather than the component simply not having replied yet.
    st.session_state["session_db_ready"] = True
    st.rerun()


def _bootstrap_wasm_database() -> None:
    """Point the session at the desktop build's persistent host-mounted directory.

    Under stlite (WASM) there is no browser IndexedDB round trip to await: the NODEFS
    mount already puts the SQLite file on the user's real disk, so the database is ready
    on the very first script run.
    """
    db_path = resolve_data_dir() / "tender_intelligence.db"
    st.session_state["session_db_path"] = db_path
    set_session_db_path(db_path)


if IS_WASM:
    _bootstrap_wasm_database()
else:
    _bootstrap_session_database()
initialize_database()
if st.session_state.pop("session_db_corrupt_warning", False):
    st.warning("Your previously saved data in this browser could not be read and was reset. Please re-import your reports.")


def sync_to_browser() -> None:
    """Push the session's current SQLite state back into the browser's IndexedDB.

    Server mode only — see sync_after_write() for the runtime-agnostic call site every
    mutating action actually uses. Uses SQLite's backup API rather than reading the raw
    file: in WAL mode recent commits live in the -wal sidecar and are not yet reflected
    in the main .db file, so a plain read would persist a stale snapshot (e.g. a deletion
    would reappear after a refresh). backup() produces a single consistent file that
    includes all committed changes.
    """
    db_path = st.session_state.get("session_db_path")
    if db_path and Path(db_path).exists():
        save_db_bytes_to_browser(build_database_backup(db_path))


def sync_after_write() -> None:
    """Flush the database after a mutation, then invalidate the cached tender table so
    the next rerun observes fresh rows. Delegates to sync_to_browser() in server mode;
    a no-op in WASM mode, where NODEFS writes are already durable — see src/persistence.py.
    """
    _sync_after_write_shim(sync_to_browser)
    # Never put private per-session data in a global cache.
    st.session_state.pop("_tender_data_cache", None)


def load_session_tenders() -> pd.DataFrame:
    """Load tenders once per session and invalidate after each committed change."""
    cached = st.session_state.get("_tender_data_cache")
    if isinstance(cached, pd.DataFrame):
        return cached
    loaded = load_tenders()
    st.session_state["_tender_data_cache"] = loaded
    return loaded


def restore_session_database(data: bytes) -> None:
    """Integrity-check a downloaded backup and atomically restore this session."""
    db_path = Path(st.session_state["session_db_path"])
    restore_database_bytes(data, db_path)
    sync_after_write()


def reset_entire_app() -> None:
    """Reset this browser's database and discard all page-specific session state."""
    db_path = Path(st.session_state["session_db_path"])
    reset_database(db_path)
    sync_after_write()

    # Keep the live session database plumbing, but discard navigation, filters,
    # previews, calculations, and widget values from every page.
    preserved = {
        "_session_db_tempdir": st.session_state.get("_session_db_tempdir"),
        "session_db_path": db_path,
        "session_db_ready": True,
    }
    st.session_state.clear()
    st.session_state.update(preserved)
    st.session_state["post_import_message"] = "The app was reset. All browser-stored app data has been removed."


def show_reset_confirmation() -> None:
    st.session_state["show_master_reset_confirmation"] = True


def hide_reset_confirmation() -> None:
    st.session_state["show_master_reset_confirmation"] = False


GITHUB_REPO = "viplove-ai/tender-intelligence-desktop"
# electron-builder's artifactName is pinned (see package.json) to these exact, version-free
# filenames specifically so this permalink pattern keeps working across every future
# release without the web app needing to know the current version number.
DESKTOP_DOWNLOAD_MAC = f"https://github.com/{GITHUB_REPO}/releases/latest/download/Tender-Intelligence.dmg"
DESKTOP_DOWNLOAD_WINDOWS = f"https://github.com/{GITHUB_REPO}/releases/latest/download/Tender-Intelligence-Setup.exe"
# scripts/checksum-installer.js (run in CI right after electron-builder) publishes these
# alongside each installer so the desktop app can verify what it downloads.
DESKTOP_CHECKSUM_MAC = DESKTOP_DOWNLOAD_MAC + ".sha256"
DESKTOP_CHECKSUM_WINDOWS = DESKTOP_DOWNLOAD_WINDOWS + ".sha256"
DESKTOP_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
# Real installers run ~150-300MB; this is a sanity ceiling against the WASM heap, not a
# realistic expectation — see download_and_verify_installer's docstring for the caveat.
MAX_INSTALLER_BYTES = 400 * 1024 * 1024


def _detect_os_download() -> tuple[str | None, str]:
    """Best-guess OS from the browser's User-Agent header, and its matching installer URL.

    Web/server mode only. Verified against a real browser UA; does NOT work inside the
    desktop build itself (see _current_desktop_platform for why) — stlite's internal
    websocket bridge exposes only a synthetic {'Host': 'stlite.local', ...}, no real UA.
    """
    user_agent = (st.context.headers or {}).get("User-Agent", "")
    if "Windows" in user_agent:
        return "Windows", DESKTOP_DOWNLOAD_WINDOWS
    if "Macintosh" in user_agent or "Mac OS X" in user_agent:
        return "macOS", DESKTOP_DOWNLOAD_MAC
    return None, DESKTOP_RELEASES_PAGE


def _current_desktop_platform() -> str | None:
    """Which OS this desktop build targets — baked in at build time, not detected live.

    scripts/write-platform-marker.js writes desktop_platform.txt before every
    `npm run dump` (package.json's predump hook), once per platform-specific CI job. This
    is necessary, not just convenient: confirmed against the real Electron build that
    st.context.headers carries no real browser User-Agent to sniff the way _detect_os_download
    does in server mode, so there's no live signal to detect the host OS from in here.
    """
    try:
        return (Path(__file__).parent / "desktop_platform.txt").read_text().strip() or None
    except Exception:
        return None


def _desktop_download_and_checksum() -> tuple[str, str, str] | None:
    """(installer_url, checksum_url, file_name) for this build's own platform, or None."""
    platform_name = _current_desktop_platform()
    if platform_name == "macOS":
        return DESKTOP_DOWNLOAD_MAC, DESKTOP_CHECKSUM_MAC, "Tender-Intelligence.dmg"
    if platform_name == "Windows":
        return DESKTOP_DOWNLOAD_WINDOWS, DESKTOP_CHECKSUM_WINDOWS, "Tender-Intelligence-Setup.exe"
    return None


def _current_app_version() -> str | None:
    """Read this build's version out of the bundled package.json.

    package.json is listed in stlite.desktop.files so it ships next to app.py in the
    desktop build too — one shared source of truth for the version number instead of a
    second copy that can silently drift from the one electron-builder actually stamps.
    """
    try:
        return json.loads((Path(__file__).parent / "package.json").read_text())["version"]
    except Exception:
        return None


def _latest_release_tag() -> str | None:
    """Fetch the newest release's tag from GitHub. Only ever called under Pyodide.

    There are no real sockets in WASM — this goes through Pyodide's browser-fetch bridge,
    which @stlite's Node worker polyfills. Verified end-to-end against the real Electron
    build (got a genuine HTTP response back through pyfetch); the caller still treats any
    exception here as "couldn't check right now" rather than letting it crash the app,
    since real-world failures (offline, rate-limited) are still possible.
    """
    import pyodide.http

    async def _get() -> str | None:
        response = await pyodide.http.pyfetch(LATEST_RELEASE_API, headers={"Accept": "application/vnd.github+json"})
        if response.status != 200:
            return None
        data = await response.json()
        return data.get("tag_name")

    return asyncio.run(_get())


def _pyfetch_bytes(url: str, *, max_bytes: int = MAX_INSTALLER_BYTES) -> bytes:
    """Fetch a URL's full body as bytes via Pyodide's browser-fetch bridge.

    Verified in the real Electron build that pyfetch's response exposes a working
    .bytes() returning a plain Python bytes object. Not re-verified at actual
    installer size (150-300MB) in this environment — only against small JSON payloads —
    so treat the first real-world run of this path as the real test. Aborts before
    reading the body if the server advertises a size over max_bytes; if the server omits
    Content-Length, this check is silently skipped rather than blocking the download.
    """
    import pyodide.http

    async def _get() -> bytes:
        response = await pyodide.http.pyfetch(url)
        response.raise_for_status()
        try:
            content_length = int(response.headers.get("content-length"))
        except Exception:
            content_length = None
        if content_length is not None and content_length > max_bytes:
            raise ValueError(f"Refusing to download {content_length} bytes (limit {max_bytes})")
        return await response.bytes()

    return asyncio.run(_get())


def _pyfetch_text(url: str) -> str | None:
    """Fetch a small text file (the checksum sidecar). None on any failure — a missing or
    unreachable checksum means "can't verify", handled by the caller, not a hard error."""
    import pyodide.http

    async def _get() -> str | None:
        response = await pyodide.http.pyfetch(url)
        if response.status != 200:
            return None
        return await response.string()

    try:
        return asyncio.run(_get())
    except Exception:
        return None


UPDATE_CHECK_INTERVAL_DAYS = 30
UPDATE_STATE_FILENAME = "update_check_state.json"


def _update_state_path() -> Path:
    return resolve_data_dir() / UPDATE_STATE_FILENAME


def _load_update_state() -> dict[str, str]:
    try:
        return json.loads(_update_state_path().read_text())
    except Exception:
        return {}


def _save_update_state(state: dict[str, str]) -> None:
    try:
        _update_state_path().write_text(json.dumps(state))
    except Exception:
        LOG.warning("Couldn't persist update-check state", exc_info=True)


def maybe_check_for_updates_in_background() -> None:
    """Silently check for a new desktop release at most once per
    UPDATE_CHECK_INTERVAL_DAYS, persisted in a small state file next to the database so
    the cadence survives app restarts — session state alone wouldn't, since it resets
    every relaunch. Only ever surfaces UI (an "info" banner + install button, wired up by
    the caller) when an update is actually pending; up-to-date and "couldn't check right
    now" are both silent, so a monthly network hiccup never interrupts normal use.

    Every failure mode here — a missing/corrupt state file, no network, a GitHub API
    hiccup, a malformed response — is caught and swallowed. Worst case is "no update
    surfaced this run, try again next cycle," never a broken app. This function is called
    unconditionally on every page render, so it must never let an exception escape.
    """
    if not IS_WASM or st.session_state.get("_update_check_attempted_this_session"):
        return
    st.session_state["_update_check_attempted_this_session"] = True
    try:
        current = _current_app_version()
        state = _load_update_state()

        # The app may have been updated since this was last written (user downloaded and
        # reinstalled) — reconcile the stale flag before deciding what to show, even if a
        # fresh network check isn't due yet.
        if current and state.get("update_available_version") == current:
            state.pop("update_available_version", None)

        last_checked_raw = state.get("last_checked")
        due = True
        if last_checked_raw:
            try:
                due = (datetime.utcnow() - datetime.fromisoformat(last_checked_raw)) >= timedelta(
                    days=UPDATE_CHECK_INTERVAL_DAYS
                )
            except Exception:
                due = True

        if due:
            st.session_state.pop("_installer_result", None)
            st.session_state.pop("_installer_bytes", None)
            try:
                latest_tag = _latest_release_tag()
            except Exception:
                LOG.warning("Scheduled update check failed", exc_info=True)
                latest_tag = None
            state["last_checked"] = datetime.utcnow().isoformat()
            if latest_tag is not None:
                latest_version = latest_tag.removeprefix("desktop-v")
                if current and latest_version != current:
                    state["update_available_version"] = latest_version
                else:
                    state.pop("update_available_version", None)

        _save_update_state(state)

        pending_version = state.get("update_available_version")
        if pending_version:
            st.session_state["_update_check_result"] = (
                "info",
                f"Version {pending_version} is available (you have {current or 'an earlier version'}).",
            )
    except Exception:
        LOG.warning("Background update check failed unexpectedly", exc_info=True)


def download_and_verify_installer() -> None:
    """Fetch this platform's installer, verify it against the published SHA256, and stage
    it in session state so a real st.download_button can hand it to the user to save.

    The version-check's small-JSON fetch is verified end-to-end against the real Electron
    build (see _latest_release_tag). This function reuses the same pyfetch mechanism for a
    much larger binary payload (150-300MB) — not separately verified at that size here, so
    the first real release is the real test of the download half of this path.
    """
    target = _desktop_download_and_checksum()
    if target is None:
        st.session_state["_installer_result"] = (
            "warning",
            f"Don't know which installer this build is — [download manually]({DESKTOP_RELEASES_PAGE}) instead.",
        )
        return
    download_url, checksum_url, file_name = target

    try:
        installer_bytes = _pyfetch_bytes(download_url)
    except Exception:
        LOG.warning("Installer download failed", exc_info=True)
        st.session_state["_installer_result"] = (
            "error",
            f"Couldn't download the installer. [Try the direct link]({download_url}) in your browser instead.",
        )
        return

    expected_line = _pyfetch_text(checksum_url)
    expected_hash = expected_line.split()[0].lower() if expected_line else None
    actual_hash = hashlib.sha256(installer_bytes).hexdigest()

    if expected_hash and actual_hash != expected_hash:
        st.session_state["_installer_result"] = (
            "error",
            f"Downloaded file failed checksum verification (expected {expected_hash[:12]}…, "
            f"got {actual_hash[:12]}…). Discarded — try again or "
            f"[download manually]({download_url}).",
        )
        return

    st.session_state["_installer_bytes"] = installer_bytes
    st.session_state["_installer_file_name"] = file_name
    st.session_state["_installer_result"] = (
        "success",
        "Downloaded and verified against the published checksum. Save it below, then quit "
        "and run it to install."
        if expected_hash
        else "Downloaded (no published checksum to verify against yet). Save it below, then "
        "quit and run it to install.",
    )


def render_masthead() -> None:
    """CPWD-style gradient title bar with one action slot on the right: a desktop
    download link in server/cloud mode. In the desktop build, the update check itself
    runs silently in the background (see maybe_check_for_updates_in_background) — there's
    no manual button; an install prompt only appears below the masthead when a newer
    release is actually available.
    """
    maybe_check_for_updates_in_background()
    with st.container(key="masthead_row"):
        title_col, action_col = st.columns([5, 1], vertical_alignment="center")
        with title_col:
            st.html(
                """
                <div style="display:flex;align-items:center;gap:15px;">
                  <span style="font-size:1.75rem;line-height:1;">🏛️</span>
                  <div>
                    <div style="color:#ffffff;font-size:1.2rem;font-weight:700;letter-spacing:.3px;">Tender Intelligence</div>
                    <div style="color:#c4e4ec;font-size:.8rem;margin-top:2px;">Historical CPWD tender analysis &amp; transparent bid estimation</div>
                  </div>
                </div>
                """
            )
        with action_col:
            if not IS_WASM:
                os_label, download_url = _detect_os_download()
                st.link_button(
                    f"Download for {os_label}" if os_label else "Get desktop app",
                    download_url,
                    width="stretch",
                )
    # .get(), not .pop(): these need to survive the rerun the "Download & verify" and
    # "Save installer" buttons themselves trigger, so the offer doesn't vanish the moment
    # the user acts on it. maybe_check_for_updates_in_background() clears the installer
    # keys whenever it runs a fresh check, so a stale offer can't outlive it.
    if result := st.session_state.get("_update_check_result"):
        level, message = result
        getattr(st, level)(message)
        if level == "info" and _desktop_download_and_checksum() is not None:
            if st.button("Download & verify installer", key="download_verify_installer_btn"):
                with st.spinner("Downloading and verifying — this can take a while for a large installer…"):
                    download_and_verify_installer()

    if result := st.session_state.get("_installer_result"):
        level, message = result
        getattr(st, level)(message)

    if "_installer_bytes" in st.session_state:
        st.download_button(
            "Save installer",
            data=st.session_state["_installer_bytes"],
            file_name=st.session_state.get("_installer_file_name", "installer"),
            mime="application/octet-stream",
            key="save_installer_btn",
        )


def render_master_reset() -> None:
    """Render the destructive reset control globally in the sidebar."""
    st.sidebar.button(
        "↻ Reset website",
        key="show_master_reset",
        width="stretch",
        on_click=show_reset_confirmation,
    )
    if st.session_state.get("show_master_reset_confirmation"):
        with st.sidebar.container(border=True):
            st.markdown("**Reset website?**")
            st.warning(
                "This permanently removes all imported tenders, saved analyses, BOQ plans, "
                "and import history stored by this browser."
            )
            confirmed = st.checkbox(
                "I understand this cannot be undone",
                key="confirm_master_reset",
            )
            st.button(
                "Permanently reset website",
                key="master_reset_button",
                type="primary",
                disabled=not confirmed,
                on_click=reset_entire_app,
                width="stretch",
            )
            st.button(
                "Cancel",
                key="cancel_master_reset",
                width="stretch",
                on_click=hide_reset_confirmation,
            )


st.markdown(
    """<style>
    /* Custom CSS targets either semantic HTML this app owns, the st-key-<key> hook
       Streamlit adds for a widget's own key=, or a stable data-testid — never the
       auto-generated st-emotion-cache-* class names, which are unstable across Streamlit
       versions (this app's two runtimes bundle two different ones: 1.59 server, 1.57
       desktop) and would silently stop matching on the next upgrade.

       This is deliberately st.markdown(..., unsafe_allow_html=True), not st.html(): when
       an st.html() call's content is *only* <style> tags, Streamlit routes it through a
       separate "event container" path (added for its own issue #9388, to dodge DOMPurify
       stripping bare <style> tags) that empirically does not reliably keep applying in
       this app — confirmed by direct inspection, isolated repros, and comparison against
       this exact st.markdown call, which does not hit that path and works every time. */
    h1,h2,h3{color:#0e4a58;font-weight:700}
    h1{letter-spacing:.2px}
    .muted{color:#5b6b73}.below{color:#15803d}.above{color:#b91c1c}
    a,a:visited{color:#0f6c82}
    details summary{font-weight:600;color:#0e4a58}
    .st-key-show_master_reset button{background:#b91c1c;border-color:#b91c1c;color:#fff}
    .st-key-show_master_reset button:hover{background:#991b1b;border-color:#991b1b;color:#fff}
    .st-key-masthead_row{background:linear-gradient(90deg,#014464 0%,#125b6c 62%,#16788f 100%);
        padding:15px 24px;border-radius:12px;margin-bottom:16px;box-shadow:0 2px 6px rgba(1,68,100,.18);}
    .st-key-masthead_row button,.st-key-masthead_row a[data-testid="stBaseLinkButton-secondary"]{
        background:#ffffff;color:#0e4a58;border-color:#ffffff;font-weight:600;}
    .st-key-masthead_row button:hover,.st-key-masthead_row a[data-testid="stBaseLinkButton-secondary"]:hover{
        background:#e7f1f3;color:#0e4a58;border-color:#e7f1f3;}

    /* Sidebar navigation: reskin the plain st.radio as a vertical tab list. */
    section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] p{
        font-size:.72rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#5b6b73;}
    section[data-testid="stSidebar"] [data-testid="stRadioGroup"]{gap:3px;width:100%;}
    section[data-testid="stSidebar"] [data-testid="stRadioOption"]{
        display:flex;align-items:center;width:100%;box-sizing:border-box;
        padding:10px 14px;border-radius:8px;cursor:pointer;
        border-left:3px solid transparent;
        transition:background-color .15s ease,color .15s ease,border-color .15s ease;}
    section[data-testid="stSidebar"] [data-testid="stRadioOption"] > div{width:100%;}
    section[data-testid="stSidebar"] [data-testid="stRadioOption"] [data-testid="stMarkdownContainer"] p{
        margin:0;font-weight:500;color:#0e4a58;transition:color .15s ease;}
    /* The circle-drawing div is always the first child before the label's markdown
       container — a DOM-order rule, not an emotion-cache class, so it survives a
       Streamlit version bump between the two runtimes. */
    section[data-testid="stSidebar"] [data-testid="stRadioOption"] > div > div > div:first-child{
        display:none;}
    section[data-testid="stSidebar"] [data-testid="stRadioOption"]:hover{
        background:#eaf4f6;border-left-color:#8ec3cf;}
    section[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"]{
        background:#125b6c;border-left-color:#e0a458;}
    section[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] [data-testid="stMarkdownContainer"] p{
        color:#ffffff;font-weight:700;}
    </style>""",
    unsafe_allow_html=True,
)


def begin_action(message: str = "Working…") -> None:
    """Widget callback that enables the native, rerun-safe action overlay."""
    st.session_state["_pending_action_message"] = message


ACTION_OVERLAY = st.empty()
if pending_action := st.session_state.get("_pending_action_message"):
    with ACTION_OVERLAY.container():
        _render_full_screen_loader(str(pending_action))


def clear_action_overlay() -> None:
    st.session_state.pop("_pending_action_message", None)
    ACTION_OVERLAY.empty()


# CPWD-aligned Plotly palette so every chart uses the portal's teal family instead of
# Plotly's default blue/rainbow. Categorical colours lead with the brand teal/navy and
# fall back to the muted tile tones seen on the e-Tendering dashboard.
CPWD_CATEGORICAL = ["#125b6c", "#16788f", "#4f9db0", "#8ec3cf", "#e0a458",
                    "#9ad0c2", "#4f6f52", "#c98b5e", "#8ea7e9", "#caa6a6"]
CPWD_CONTINUOUS = ["#e2eef1", "#8ec3cf", "#4f9db0", "#16788f", "#014464"]
pio.templates["cpwd"] = {"layout": {"colorway": CPWD_CATEGORICAL}}
px.defaults.template = "plotly_white+cpwd"
px.defaults.color_discrete_sequence = CPWD_CATEGORICAL
px.defaults.color_continuous_scale = CPWD_CONTINUOUS


PAGES = ["Tender Analysis & Bid Estimator", "Dashboard", "Contractors"]
# A brand-new browser (empty per-session database) is steered to the Dashboard, which
# renders the import UI itself until the first CPWD award report has been added.
ONBOARDING_PAGES = ["Dashboard"]


def safe_options(series: pd.Series) -> list[str]:
    return sorted(str(value) for value in series.dropna().unique() if str(value).strip())


def counted_multiselect(label: str, series: pd.Series, key: str) -> list[str]:
    clean = series.dropna().astype(str)
    clean = clean[clean.str.strip().ne("")]
    counts = clean.value_counts()
    options = sorted(counts.index.tolist())
    return st.multiselect(
        f"{label} ({len(options)} unique)", options, key=key,
        format_func=lambda value: f"{value} — {int(counts.get(value, 0)):,} tenders",
    )


def show_percent(value):
    return format_variance(value)


def show_table(frame: pd.DataFrame, key: str | None = None, height: int = 420):
    if frame.empty:
        st.info("No records match this view.")
        return
    visible = sort_tender_details_newest_first(frame)
    rename = {
        "tender_id": "Tender ID", "nit_rfp_no": "NIT / RFP No", "work_name": "Work Description",
        "region": "Region", "zone_circle": "Zone / Circle", "division": "Division", "subdivision": "Sub-division",
        "location": "Location", "estimated_cost": "Estimated Cost",
        "quoted_value": "Awarded Value", "variance_percent": "Above / Below", "contractor_name": "Contractor",
        "status": "Status", "work_type": "Work Type", "bid_opening_datetime": "Bid Opening",
        "submission_closing_datetime": "Bid Submission Closing", "award_date": "Award Date",
        "tender_award_date": "Tender Award Date", "awarded_date": "Awarded Date", "date_of_award": "Date of Award",
        "_score": "Match Score", "_description_similarity": "Description Match", "_reason": "Why Matched",
    }
    columns = [column for column in rename if column in visible.columns]
    visible = visible[columns].rename(columns=rename)
    if "Above / Below" in visible:
        visible["Above / Below"] = visible["Above / Below"].map(format_variance)
    if "Description Match" in visible:
        visible["Description Match"] = visible["Description Match"].map(lambda value: f"{float(value):.0%}" if pd.notna(value) else "—")
    if "Match Score" in visible:
        visible["Match Score"] = visible["Match Score"].map(lambda value: f"{float(value):.2f}" if pd.notna(value) else "—")
    for amount_column in ("Estimated Cost", "Awarded Value"):
        if amount_column in visible:
            visible[amount_column] = visible[amount_column].map(format_inr_compact)
    st.dataframe(
        visible, width="stretch", hide_index=True, height=height, key=key,
    )


def chart(frame, **kwargs):
    fig = px.bar(frame, **kwargs)
    fig.update_layout(margin=dict(l=10, r=10, t=50, b=10), legend_title_text="")
    st.plotly_chart(fig, width="stretch")


def money_chart(frame: pd.DataFrame, value_column: str, axis: str = "y", **kwargs):
    plotted = frame.copy()
    divisor, unit = currency_scale(plotted[value_column])
    plotted[value_column] = plotted[value_column] / divisor
    labels = dict(kwargs.pop("labels", {}))
    labels[value_column] = unit
    kwargs[axis] = value_column
    chart(plotted, labels=labels, **kwargs)


def bundled_data_details(df: pd.DataFrame) -> tuple[list[str], pd.Timestamp | None]:
    """Return bundled public-data regions and their latest tender date."""
    if df.empty or "source_file" not in df:
        return [], None
    bundled_mask = pd.Series(False, index=df.index)
    regions = set()
    for source_file in df["source_file"].dropna().astype(str).unique():
        match = BUNDLED_DATA_FILE_PATTERN.fullmatch(Path(source_file).stem)
        if not match:
            continue
        regions.add(re.sub(r"[_-]+", " ", match.group("region")).strip().title())
        bundled_mask |= df["source_file"].eq(source_file)
    if not regions:
        return [], None
    date_values = []
    for column in ("bid_opening_datetime", "submission_closing_datetime"):
        if column in df:
            date_values.append(pd.to_datetime(df.loc[bundled_mask, column], errors="coerce", format="mixed"))
    valid_dates = pd.concat(date_values).dropna() if date_values else pd.Series(dtype="datetime64[ns]")
    latest = valid_dates.max() if not valid_dates.empty else None
    return sorted(regions), latest


def dashboard_page(df: pd.DataFrame):
    st.title("Tender Intelligence Dashboard")
    if message := st.session_state.pop("post_import_message", None):
        st.success(message)
    if df.empty:
        # No data yet: the Dashboard itself acts as the onboarding / import screen.
        render_import_ui(onboarding=True)
        return
    bundled_regions, data_through = bundled_data_details(df)
    if bundled_regions:
        region_text = ", ".join(bundled_regions)
        date_text = (
            f" Data available through {data_through.day} {data_through:%B %Y}, based on the latest tender date."
            if data_through is not None
            else ""
        )
        st.info(f"You are viewing public CPWD tender data for {region_text}.{date_text}")
    st.caption("A consolidated view of local CPWD tender and award records. Filters apply to every metric and chart below.")
    add_col, delete_col = st.columns(2)
    with add_col:
        with st.expander("➕ Add more tenders", expanded=False):
            render_import_ui(onboarding=False)
    with delete_col:
        with st.expander("🗑️ Delete tenders by region / office", expanded=False):
            render_delete_region_ui(df)
    with st.sidebar:
        st.subheader("Dashboard filters")
        filters = {}
        for field, label in (("region", "Region"), ("zone_circle", "Zone / Circle"), ("division", "Division"),
                             ("subdivision", "Sub-division"), ("financial_year", "Financial year"), ("work_type", "Work type"),
                             ("contractor_name", "Contractor"), ("status", "Status"), ("location", "Location")):
            filters[field] = counted_multiselect(label, df[field], key=f"filter_{field}")
        costs = df["estimated_cost"].dropna()
        if not costs.empty:
            minimum, maximum = float(costs.min()), float(costs.max())
            filters["estimated_cost"] = st.slider("Estimated-cost range", minimum, maximum, (minimum, maximum), format="₹%.0f")
    selected = filter_tenders(df, filters)
    metrics = dashboard_metrics(selected)
    cards = st.columns(6)
    labels = [
        ("Unique tenders", metrics["unique_tenders"]), ("Awarded tenders", metrics["awarded_tenders"]),
        ("Contractors", metrics["contractor_count"]), ("Total estimated value", format_inr_compact(metrics["total_estimated_value"])),
        ("Total awarded value", format_inr_compact(metrics["total_awarded_value"])), ("Weighted result", show_percent(metrics["weighted_variance"])),
    ]
    for card, (label, value) in zip(cards, labels):
        card.metric(
            label,
            value,
            help="Weighted result compares the sum of awarded values with the sum of valid estimated costs."
            if label == "Weighted result"
            else None,
            border=True,
        )
    awards = selected[selected["is_awarded"]]
    if awards.empty:
        st.warning("The selected records contain no awarded tenders with both a contractor and positive awarded value.")
        show_table(selected)
        return
    c1, c2 = st.columns(2)
    with c1:
        division = awards.groupby("division", dropna=False).agg(Awards=("id", "count"), Value=("quoted_value", "sum")).reset_index()
        money_chart(division, "Value", x="division", color="Awards", title="Awards by division", labels={"division": "Division"})
    with c2:
        annual = awards.groupby("financial_year", dropna=False).agg(Awards=("id", "count"), Value=("quoted_value", "sum")).reset_index()
        money_chart(annual, "Value", x="financial_year", color="Awards", title="Awards by financial year", labels={"financial_year": "Financial year"})
    c3, c4 = st.columns(2)
    with c3:
        types = awards.groupby("work_type").size().reset_index(name="Awards")
        fig = px.pie(types, names="work_type", values="Awards", title="Work-type distribution", hole=.42)
        st.plotly_chart(fig, width="stretch")
    with c4:
        top_count = awards.groupby("contractor_name").size().nlargest(10).sort_values().reset_index(name="Awards")
        chart(top_count, y="contractor_name", x="Awards", orientation="h", title="Top contractors by award count", labels={"contractor_name": "Contractor"})
    top_value = awards.groupby("contractor_name")["quoted_value"].sum().nlargest(10).sort_values().reset_index()
    money_chart(top_value, "quoted_value", axis="x", y="contractor_name", orientation="h", title="Top contractors by awarded value", labels={"contractor_name": "Contractor"})
    st.subheader("Filtered tender details")
    show_table(selected)


MASTER_DATA_DIR = Path(__file__).resolve().parent / "master_data"
BUNDLED_DATA_FILE_PATTERN = re.compile(r"^region_cpwd_(?P<region>.+)$", re.IGNORECASE)


def available_bundled_data_files() -> list[Path]:
    """Public CPWD Excel reports bundled with the app for ready-made loading."""
    if not MASTER_DATA_DIR.exists():
        return []
    return sorted(p for p in MASTER_DATA_DIR.iterdir() if p.suffix.lower() in {".xls", ".xlsx"})


def available_bundled_data_regions() -> dict[str, Path]:
    """Map region labels to files named ``region_cpwd_<region>.xls[x]``."""
    regions = {}
    for path in available_bundled_data_files():
        match = BUNDLED_DATA_FILE_PATTERN.fullmatch(path.stem)
        if not match:
            continue
        label = re.sub(r"[_-]+", " ", match.group("region")).strip().title()
        if label:
            regions[label] = path
    return dict(sorted(regions.items()))


def load_bundled_dataset(region: str, data_file: Path) -> None:
    """Import one selected region's bundled report into this browser's database."""
    if data_file not in available_bundled_data_files():
        st.session_state["import_error"] = "The selected CPWD dataset is not available."
        st.rerun()
    try:
        totals = {key: 0 for key in ("inserted_rows", "updated_rows", "unchanged_rows", "rejected_rows")}
        with centered_loader(f"Loading public CPWD data for {region}…"):
            items = prepare_import_batch([(data_file.read_bytes(), data_file.name)])
            for preview, _ in items:
                if preview.validation_errors:
                    raise ValueError("; ".join(preview.validation_errors))
            for result in commit_import_batch(items):
                for key in totals:
                    totals[key] += result[key]
            sync_after_write()
        st.session_state.pop("import_error", None)
        st.session_state["post_import_message"] = (
            f"{region} CPWD data loaded: {totals['inserted_rows']} tenders added"
            + (f", {totals['unchanged_rows']} already present." if totals["unchanged_rows"] else ".")
        )
        st.session_state["pending_nav"] = "Dashboard"
        st.rerun()
    except Exception:
        LOG.exception("Bundled CPWD data load failed")
        st.session_state["import_error"] = "The CPWD dataset could not be loaded. See the server log for details."
        st.rerun()


def render_import_ui(onboarding: bool = False):
    """Import controls, reused as the empty-state onboarding screen and the Dashboard's
    'Add more tenders' section. No page title of its own — the host page provides context."""
    if onboarding:
        st.info(
            "Welcome — this browser has no saved data yet. Download the tender report for your region "
            "from the CPWD e-Tendering website and upload it below. Once imported, the dashboard and "
            "contractor tabs unlock."
        )
    bundled_regions = available_bundled_data_regions()
    if bundled_regions:
        with st.container(border=True):
            st.markdown(
                "**Load public CPWD data** Choose a region to use the ready-made tender records. "
                "You can clear or replace them at any time."
            )
            selected_region = st.selectbox(
                "Choose a region",
                options=list(bundled_regions),
                key="bundled_data_region",
            )
            if st.button(
                f"📊 Load {selected_region} CPWD data",
                key="load_bundled_data",
                on_click=begin_action,
                args=(f"Loading public CPWD data for {selected_region}…",),
            ):
                load_bundled_dataset(selected_region, bundled_regions[selected_region])
        st.caption("— or import your own report below —")
    st.write("Upload one or more CPWD `.xls` or `.xlsx` reports. Nothing is saved until you review the preview and press **Confirm Import**.")
    division = st.text_input("Division override (optional)", help="Leave blank to detect it from Tender Publishing Office.")
    files = st.file_uploader(
        "Choose reports",
        type=["xls", "xlsx"],
        accept_multiple_files=True,
        max_upload_size=5,
    )
    signature = tuple((f.name, f.size) for f in files) if files else ()
    if not files:
        # Uploader cleared — drop any stale preview/error state from a previous selection.
        for key in ("import_previews", "import_signature", "import_error"):
            st.session_state.pop(key, None)
    if files and st.session_state.get("import_signature") != signature:
        previews = []
        error_message = None
        try:
            with centered_loader("Previewing the uploaded reports…"):
                previews = prepare_import_batch([(uploaded.getvalue(), uploaded.name) for uploaded in files], division or None)
        except ValueError as exc:
            error_message = f"The selected reports could not be previewed: {exc}"
        except Exception:
            LOG.exception("Import preview failed")
            error_message = "The selected reports could not be previewed. See the server log for details."
        st.session_state["import_previews"] = previews
        st.session_state["import_signature"] = signature
        # Persist any error in session_state so it stays on screen across Streamlit reruns
        # instead of flashing once and vanishing when the next interaction re-runs the script.
        st.session_state["import_error"] = error_message
    if import_error := st.session_state.get("import_error"):
        st.error(import_error)
    previews = st.session_state.get("import_previews", [])
    valid = bool(previews)
    for preview, _ in previews:
        st.subheader(preview.filename)
        if preview.validation_errors:
            valid = False
            for error in preview.validation_errors:
                st.error(error)
            continue
        st.caption(f"Detected division: **{preview.division or 'Not detected'}** · SHA-256: `{preview.file_hash[:12]}…`")
        counts = preview.counts
        cols = st.columns(7)
        for col, (label, key) in zip(cols, [("Rows", "total_rows"), ("New", "new_tenders"), ("Updates", "updated_tenders"),
                                                     ("Unchanged", "unchanged_duplicates"), ("Rejected", "rejected_rows"),
                                                     ("Awarded", "awarded_records"), ("Warnings", "missing_award_warnings")]):
            col.metric(label, counts[key], border=True)
        display = pd.DataFrame(preview.records)
        if not display.empty:
            show_table(display.head(100))
        if preview.rejected:
            with st.expander("Rejected rows"):
                st.dataframe(sort_tender_details_newest_first(pd.DataFrame(preview.rejected)), hide_index=True, width="stretch")
    if valid and st.button(
        "Confirm Import",
        type="primary",
        on_click=begin_action,
        args=("Saving the import…",),
    ):
        try:
            totals = {key: 0 for key in ("inserted_rows", "updated_rows", "unchanged_rows", "rejected_rows")}
            with centered_loader("Saving the import…"):
                results = commit_import_batch(previews)
            for result in results:
                for key in totals:
                    totals[key] += result[key]
            sync_after_write()
            st.session_state.pop("import_previews", None)
            st.session_state.pop("import_signature", None)
            st.session_state.pop("import_error", None)
            st.session_state["post_import_message"] = (
                f"Import complete: {totals['inserted_rows']} new, {totals['updated_rows']} updated, "
                f"{totals['unchanged_rows']} unchanged, {totals['rejected_rows']} rejected."
            )
            # Use a non-widget key: the nav radio is already instantiated in this run, and
            # Streamlit forbids writing a widget's own key after it exists. The pending value
            # is applied to nav_page before the radio is created on the next run.
            st.session_state["pending_nav"] = "Dashboard"
            st.rerun()
        except ValueError as exc:
            st.session_state["import_error"] = f"The import was rejected: {exc}"
            st.rerun()
        except Exception:
            LOG.exception("Confirmed import failed")
            # Keep the previews on screen and persist the reason so it survives the rerun,
            # then rerun to render it at the top of the page rather than only at the bottom.
            st.session_state["import_error"] = (
                "The import could not be completed and no changes were saved. See the server log for details."
            )
            st.rerun()


def render_delete_region_ui(df: pd.DataFrame):
    """Delete a whole office/region slice of tenders, then re-import corrected reports.
    Lives on the Dashboard alongside the import section since the two are used together."""
    st.download_button(
        "⬇️ Download database backup",
        data=lambda: build_database_backup(st.session_state["session_db_path"]),
        file_name=f"tender_intelligence_{date.today():%Y%m%d}.db",
        mime="application/vnd.sqlite3",
        help="Save the current browser-backed database before deleting records.",
    )
    with st.expander("Restore a downloaded database backup"):
        backup = st.file_uploader(
            "Choose SQLite backup",
            type=["db", "sqlite", "sqlite3"],
            key="restore_database_upload",
            max_upload_size=128,
            help="Maximum 128 MB. The backup is integrity-checked before it replaces this session's data.",
        )
        confirm_restore = st.checkbox(
            "I understand this replaces all data currently stored by this browser",
            key="confirm_database_restore",
        )
        if st.button(
            "Restore Database Backup",
            key="restore_database_button",
            type="primary",
            disabled=backup is None or not confirm_restore,
            on_click=begin_action,
            args=("Checking and restoring the database backup…",),
        ):
            try:
                restore_session_database(backup.getvalue())
                st.session_state["post_import_message"] = "Database backup restored successfully."
                st.rerun()
            except BrowserDatabaseError as exc:
                st.error(f"The backup was not restored: {exc}")
            except (sqlite3.DatabaseError, OSError):
                LOG.warning("Database restore rejected", exc_info=True)
                st.error("The backup was not restored. See the server log for details.")
    st.warning(
        "This permanently deletes every matching tender. Download a database backup first "
        "if you may need to undo it. Import-history entries are retained."
    )
    delete_filters = {}
    c1, c2 = st.columns(2)
    with c1:
        delete_filters["region"] = counted_multiselect("Region", df["region"], "delete_region")
        delete_filters["division"] = counted_multiselect("Division", df["division"], "delete_division")
    with c2:
        delete_filters["zone_circle"] = counted_multiselect("Zone / Circle", df["zone_circle"], "delete_zone")
        delete_filters["subdivision"] = counted_multiselect("Sub-division", df["subdivision"], "delete_subdivision")
    delete_preview = filter_tenders(df, delete_filters) if any(delete_filters.values()) else df.iloc[0:0]
    st.metric("Tenders that will be deleted", f"{len(delete_preview):,}", border=True)
    confirm_delete = st.checkbox("I understand this deletion is permanent and I have checked the filters", key="confirm_office_delete")
    if st.button(
        "Delete Matching Tender Data",
        type="primary",
        disabled=delete_preview.empty or not confirm_delete,
        on_click=begin_action,
        args=("Deleting the selected tender data…",),
    ):
        try:
            deleted = delete_tenders_by_filters(delete_filters)
            sync_after_write()
            st.success(f"Deleted {deleted:,} tenders. You can now import the corrected reports again.")
        except ValueError as exc:
            st.error(str(exc))


def contractors_page(df: pd.DataFrame):
    """Single contractor page: an individual profile and a side-by-side comparison,
    merged into tabs so contractor intelligence lives in one place."""
    st.title("Contractors")
    awards = df[df["is_awarded"]] if not df.empty else df
    names = safe_options(awards.get("contractor_name", pd.Series(dtype=str)))
    if not names:
        st.info("No awarded contractor records are available yet.")
        return
    award_counts = awards["contractor_name"].value_counts()
    profile_tab, compare_tab = st.tabs(["Profile", "Compare"])
    with profile_tab:
        _contractor_profile_section(awards, names, award_counts)
    with compare_tab:
        _contractor_comparison_section(awards, names, award_counts)


def _contractor_profile_section(awards: pd.DataFrame, names: list, award_counts: pd.Series):
    query = st.text_input("Search contractor")
    matches = [name for name in names if query.casefold() in name.casefold()]
    name = st.selectbox("Select contractor", matches or names, format_func=lambda value: f"{value} — {int(award_counts.get(value, 0)):,} awards")
    group = awards[awards["contractor_name"] == name].copy()
    metrics = contractor_metrics(group)
    cards = st.columns(6)
    values = [("Awards", metrics["award_count"]), ("Awarded value", format_inr_compact(metrics["total_awarded_value"])),
              ("Average award", format_inr_compact(metrics["average_award_size"])), ("Average result", show_percent(metrics["average_variance"])),
              ("Median result", show_percent(metrics["median_variance"])), ("Weighted result", show_percent(metrics["weighted_variance"]))]
    helps = {"Average result": "Simple mean of valid tender percentages.", "Median result": "Middle valid percentage; less affected by outliers.",
             "Weighted result": "Uses total quoted and estimated values, so larger tenders carry more weight."}
    for col, (label, value) in zip(cards, values):
        col.metric(label, value, help=helps.get(label), border=True)
    st.write(f"**Bid positions:** {metrics['below_count']} Below · {metrics['above_count']} Above · {metrics['at_par_count']} At Par")
    st.write(f"**Range:** {show_percent(metrics['most_below'])} to {show_percent(metrics['most_above'])} · **Tender size:** {format_inr_compact(metrics['minimum_tender_size'])} to {format_inr_compact(metrics['maximum_tender_size'])}")
    st.write(f"**Divisions:** {', '.join(metrics['divisions']) or '—'}  |  **Preferred locations:** {', '.join(metrics['locations']) or '—'}  |  **Primary work types:** {', '.join(metrics['work_types']) or '—'}")
    c1, c2 = st.columns(2)
    with c1:
        annual = group.groupby("financial_year", dropna=False).agg(Awards=("id", "count"), Average_Result=("variance_percent", "mean")).reset_index()
        chart(annual, x="financial_year", y="Average_Result", color="Awards", title="Annual bidding trend", labels={"financial_year": "Financial year", "Average_Result": "Average % (+ above / − below)"})
    with c2:
        work = group.groupby("work_type").size().sort_values().reset_index(name="Awards")
        chart(work, y="work_type", x="Awards", orientation="h", title="Awards by work type", labels={"work_type": "Work type"})
    st.subheader("Complete tender history")
    show_table(group)


def _contractor_comparison_section(awards: pd.DataFrame, names: list, award_counts: pd.Series):
    chosen = st.multiselect("Select up to five contractors", names, max_selections=5,
                            format_func=lambda value: f"{value} — {int(award_counts.get(value, 0)):,} awards")
    if not chosen:
        st.info("Choose contractors to compare.")
        return
    rows = []
    for name in chosen:
        m = contractor_metrics(awards[awards["contractor_name"] == name])
        rows.append({"Contractor": name, "Awards": m["award_count"], "Awarded Value": m["total_awarded_value"],
                     "Average %": m["average_variance"], "Median %": m["median_variance"], "Weighted %": m["weighted_variance"],
                     "Below": m["below_count"], "Above": m["above_count"], "At Par": m["at_par_count"]})
    summary = pd.DataFrame(rows)
    summary_display = summary.copy()
    summary_display["Awarded Value"] = summary_display["Awarded Value"].map(format_inr_compact)
    st.dataframe(summary_display, hide_index=True, width="stretch")
    c1, c2 = st.columns(2)
    with c1:
        money_chart(summary, "Awarded Value", x="Contractor", color="Awards", title="Awarded value comparison")
    with c2:
        chart(summary, x="Contractor", y="Weighted %", color="Awards", title="Weighted above / below comparison")
    trend = awards[awards["contractor_name"].isin(chosen)].groupby(["financial_year", "contractor_name"])["variance_percent"].median().reset_index()
    fig = px.line(trend, x="financial_year", y="variance_percent", color="contractor_name", markers=True, title="Median bidding trend")
    st.plotly_chart(fig, width="stretch")


def _show_nit_extraction(extraction: NITExtraction):
    st.success(f"Extracted {extraction.page_count} pages from **{extraction.filename}**. Review the detected values before estimating.")
    cards = st.columns(6)
    values = [
        ("NIT number", extraction.nit_no or "Not detected"),
        ("Estimated cost", format_inr_compact(extraction.estimated_cost)),
        ("EMD", format_inr_compact(extraction.emd_amount)),
        ("Completion", extraction.completion_period or "Not detected"),
        ("BOQ items", len(extraction.boq_items)),
        ("BOQ total", format_inr_compact(extraction.boq_total)),
    ]
    for card, (label, value) in zip(cards, values):
        card.metric(label, value, border=True)
    if extraction.warnings:
        for warning in extraction.warnings:
            st.warning(warning)
    with st.expander("Important extracted tender conditions", expanded=True):
        details = {
            "Name of work": extraction.work_name,
            "Division / office": extraction.division,
            "Location": extraction.location,
            "Bid type": extraction.bid_type,
            "Submission deadline": extraction.submission_closing,
            "Bid opening": extraction.bid_opening,
            "Performance guarantee": f"{extraction.performance_guarantee_percent:g}%" if extraction.performance_guarantee_percent is not None else None,
            "Security deposit": f"{extraction.security_deposit_percent:g}%" if extraction.security_deposit_percent is not None else None,
            "Civil estimated cost": format_inr(extraction.civil_estimated_cost) if extraction.civil_estimated_cost is not None else None,
            "Electrical estimated cost": format_inr(extraction.electrical_estimated_cost) if extraction.electrical_estimated_cost is not None else None,
            "DSR / cost index (Civil)": (
                f"DSR {extraction.civil_dsr_year}; {extraction.civil_cost_index_percent:g}% cost index"
                if extraction.civil_dsr_year is not None and extraction.civil_cost_index_percent is not None else None
            ),
            "DSR / cost index (Electrical)": (
                f"DSR {extraction.electrical_dsr_year}; {extraction.electrical_cost_index_percent:g}% cost index"
                if extraction.electrical_dsr_year is not None and extraction.electrical_cost_index_percent is not None else None
            ),
            "Contractor eligibility": extraction.contractor_eligibility,
            "Similar-work criteria": extraction.similar_work_criteria,
        }
        for label, value in details.items():
            st.write(f"**{label}:** {value or 'Not detected'}")
    if extraction.boq_items:
        with st.expander(f"Extracted Schedule of Quantities ({len(extraction.boq_items)} priced items)"):
            boq = pd.DataFrame(extraction.boq_items).rename(columns={
                "item_no": "Item", "description": "Description", "quantity": "Quantity",
                "unit": "Unit", "rate": "Rate (₹)", "amount": "Amount (₹)",
            })
            st.dataframe(boq, hide_index=True, width="stretch", height=420)


def _performance_guarantee_for_tender(
    estimated_cost: float,
    bid_percent: float,
    extraction: NITExtraction | None,
) -> dict:
    normal_percent = extraction.performance_guarantee_percent if extraction and extraction.performance_guarantee_percent is not None else 5.0
    components = {
        "Civil portion": extraction.civil_estimated_cost if extraction else None,
        "E&M portion": extraction.electrical_estimated_cost if extraction else None,
    }
    return calculate_performance_guarantee(
        estimated_cost, bid_percent, normal_pg_percent=normal_percent, components=components,
    )


def _show_performance_guarantee(
    title: str,
    estimated_cost: float,
    bid_percent: float | None,
    extraction: NITExtraction | None,
) -> None:
    if bid_percent is None or not estimated_cost:
        return
    pg = _performance_guarantee_for_tender(estimated_cost, bid_percent, extraction)
    with st.expander(f"{title}: PG at {show_percent(bid_percent)}", expanded=True):
        a, b, c, d = st.columns(4)
        a.metric("ECPT", format_inr_compact(pg["estimated_cost"]), border=True)
        b.metric("Tendered amount", format_inr_compact(pg["tendered_amount"]), border=True)
        c.metric("Total PG", format_inr_compact(pg["total_pg"]), border=True)
        d.metric("Total PG percentage", f"{pg['total_pg_percent']:.2f}%", border=True)
        additional_calculation = (
            f"{100 - pg['additional_threshold_below_percent']:.0f}% of ECPT - tendered amount"
            if pg["additional_pg"] > 0 else "Not applicable (bid is not more than 20% below)"
        )
        rows = [
            {"Component": "Normal PG", "Calculation": f"{pg['normal_pg_percent']:g}% of ECPT", "Amount": format_inr(round(pg["normal_pg"]))},
            {"Component": "Additional PG", "Calculation": additional_calculation, "Amount": format_inr(round(pg["additional_pg"]))},
            {"Component": "Total Performance Guarantee", "Calculation": "Normal PG + Additional PG", "Amount": format_inr(round(pg["total_pg"]))},
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if pg["additional_pg"] > 0:
            st.caption(
                f"The bid is {-bid_percent - pg['additional_threshold_below_percent']:.2f}% below the 20% threshold. "
                f"Total PG = {pg['normal_pg_percent']:g}% + {pg['additional_pg_percent']:.2f}% = {pg['total_pg_percent']:.2f}% of ECPT."
            )
        if pg["component_pg"]:
            component_rows = [
                {"Component": name, "Proportionate PG": format_inr(round(amount))}
                for name, amount in pg["component_pg"].items()
            ]
            st.write("**Proportionate breakup for understanding**")
            st.dataframe(pd.DataFrame(component_rows), hide_index=True, width="stretch")
            st.caption("For a composite tender, the PG may still be submitted as one consolidated guarantee, subject to the NIT conditions.")


def _render_analysis_results(result: dict, costing_result: dict | None, estimated: float, extraction) -> None:
    """Render a completed analysis from a stored bundle so results persist across reruns
    instead of vanishing the moment the user changes anything after pressing Analyze."""
    st.subheader("③ Bid estimate & analysis")
    st.caption("Results from your most recent run. Change the inputs above and press Analyze again to refresh them.")

    # Live performance-guarantee explorer — recomputes instantly and does not alter the
    # saved analysis. It sits with the results because a bid position is a decision made
    # once you can see the estimate.
    if estimated > 0:
        with st.container(border=True):
            st.markdown("**Performance guarantee — try a bid position**")
            whatif = st.number_input(
                "Bid position (% above / below)", min_value=-99.99, max_value=200.0, step=0.1,
                format="%.2f", key="pg_whatif_percent",
                help="Negative = below the estimate, positive = above. Example: -26.97 means 26.97% below.",
            )
            _show_performance_guarantee("Your bid scenario", estimated, whatif, extraction)

    if costing_result:
        st.markdown("#### BOQ profitability plan")
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("BOQ value", format_inr_compact(costing_result["boq_total"]), border=True)
        m2.metric("Execution cost", format_inr_compact(costing_result["execution_cost"]), border=True)
        m3.metric("Total internal cost", format_inr_compact(costing_result["total_internal_cost"]), border=True)
        m4.metric("Target bid amount", format_inr_compact(costing_result["target_bid_amount"]), border=True)
        m5.metric("Expected profit", format_inr_compact(costing_result["expected_profit"]), border=True)
        m6.metric("Detailed-rate coverage", f"{costing_result['override_coverage_percent']:.1f}%", border=True)
        b1, b2 = st.columns(2)
        b1.metric("Break-even tender position", show_percent(costing_result["break_even_percent"]), border=True)
        b2.metric(
            "Recommended position for target margin",
            show_percent(costing_result["recommended_bid_percent"]),
            border=True,
        )
        if result.get("most_likely_amount") is not None:
            historical_profit = float(result["most_likely_amount"]) - costing_result["total_internal_cost"]
            if historical_profit < 0:
                st.warning(
                    f"The historical most-likely bid is {format_inr_compact(abs(historical_profit))} below your planned internal cost. "
                    "Review category assumptions, supplier rates, or the decision to bid."
                )
            else:
                st.info(f"At the historical most-likely bid, the plan leaves approximately {format_inr_compact(historical_profit)} above internal cost.")
        st.markdown("##### Civil / E&M cost summary")
        work_part_costs = pd.DataFrame(costing_result["work_parts"]).rename(columns={
            "work_part": "Work Part", "boq_amount": "BOQ Amount", "planned_cost": "Planned Cost", "saving": "Potential Saving",
        })
        for column in ("BOQ Amount", "Planned Cost", "Potential Saving"):
            work_part_costs[column] = work_part_costs[column].map(format_inr_compact)
        st.dataframe(work_part_costs, hide_index=True, width="stretch")
        st.markdown("##### Category cost summary")
        category_costs = pd.DataFrame(costing_result["categories"]).rename(columns={
            "work_part": "Work Part", "category": "Category", "boq_amount": "BOQ Amount", "planned_cost": "Planned Cost", "saving": "Potential Saving",
        })
        for column in ("BOQ Amount", "Planned Cost", "Potential Saving"):
            category_costs[column] = category_costs[column].map(format_inr_compact)
        st.dataframe(category_costs, hide_index=True, width="stretch")
        _show_performance_guarantee(
            "Cost-plan recommended bid", estimated, costing_result["recommended_bid_percent"], extraction,
        )

    st.markdown(f"#### Confidence: {result['confidence']}")
    scale_label = str(result.get("cost_scale") or "not classified").title()
    st.write(f"**Comparable records used:** {result['comparable_count']} ({result['strong_comparable_count']} strong matches) · **Cost scale:** {scale_label}")
    if result["confidence"] != "Insufficient Data":
        low, high = result["range_percent"]
        low_amt, high_amt = result["amount_range"]
        a, b, c = st.columns(3)
        a.metric("Most likely result", show_percent(result["most_likely_percent"]), border=True)
        b.metric("Most likely quoted amount", format_inr_compact(result["most_likely_amount"]), border=True)
        c.metric("Normal range", f"{show_percent(low)} to {show_percent(high)}", border=True)
        st.write(f"**Expected quoted amount range:** {format_inr_compact(low_amt)} to {format_inr_compact(high_amt)}")
        _show_performance_guarantee(
            "Historical most-likely bid", estimated, result["most_likely_percent"], extraction,
        )
        similar = result["comparables"].head(20)
        st.subheader("Similar historical tenders")
        st.caption("Newest tender first. Match Score and Why Matched show the comparison strength alongside the winner, awarded value and bid percentage.")
        show_table(similar)
        contractor_history = similar.dropna(subset=["contractor_name"]).groupby("contractor_name").agg(
            Comparable_Wins=("id", "count"),
            Median_Bid_Percent=("variance_percent", "median"),
            Average_Awarded_Value=("quoted_value", "mean"),
            Latest_Award=("bid_opening_datetime", "max"),
        ).sort_values(["Comparable_Wins", "Median_Bid_Percent"], ascending=[False, True]).head(10).reset_index()
        if not contractor_history.empty:
            st.subheader("Contractors in comparable historical work")
            contractor_display = contractor_history.rename(columns={
                "contractor_name": "Contractor", "Comparable_Wins": "Comparable Wins",
                "Median_Bid_Percent": "Median Bid", "Average_Awarded_Value": "Average Awarded Value",
                "Latest_Award": "Latest Award",
            })
            contractor_display["Median Bid"] = contractor_display["Median Bid"].map(format_variance)
            contractor_display["Average Awarded Value"] = contractor_display["Average Awarded Value"].map(format_inr_compact)
            st.dataframe(contractor_display, hide_index=True, width="stretch")
            chart(contractor_history.sort_values("Comparable_Wins"), y="contractor_name", x="Comparable_Wins", orientation="h", title="Frequent winners among comparable tenders", labels={"contractor_name": "Contractor", "Comparable_Wins": "Comparable wins"})
    else:
        st.warning(result["explanation"])
    st.info(result["explanation"] + " This is a historical estimate, not a guarantee of a competitor’s future bid.")


def estimator_page(df: pd.DataFrame):
    st.title("Tender Analysis & Bid Estimator")
    st.caption(
        "Estimate a competitive bid from transparent historical CPWD awards. Upload a NIT PDF to auto-fill the "
        "details and unlock BOQ cost planning — or just enter the details yourself. A PDF is optional."
    )
    awards = df[df["is_awarded"]] if not df.empty else df
    contractor_map = {}
    if not awards.empty:
        contractor_map = awards.dropna(subset=["contractor_name"]).drop_duplicates("contractor_name").set_index("contractor_name")["awarded_contractor_id"].to_dict()

    saved_analyses = list_tender_analyses()
    saved_by_id = {int(row["id"]): row for row in saved_analyses}
    if "pending_saved_analysis_choice" in st.session_state:
        pending_saved_choice = st.session_state.pop("pending_saved_analysis_choice")
        if pending_saved_choice in saved_by_id or pending_saved_choice is None:
            st.session_state["saved_analysis_choice"] = pending_saved_choice
    st.session_state.setdefault("saved_analysis_choice", None)
    if st.session_state["saved_analysis_choice"] not in saved_by_id:
        st.session_state["saved_analysis_choice"] = None
    st.session_state.setdefault("nit_uploader_version", 0)
    st.session_state.setdefault("boq_editor_version", 0)

    def clear_analysis_workspace() -> None:
        for key in (
            "active_saved_analysis_id", "nit_extraction", "nit_pdf_signature", "nit_source_filename",
            "est_zone_circle", "est_division", "est_location", "est_cost",
            "est_description", "est_work_type", "est_opening", "last_analysis_result", "analysis_render",
            "boq_costing_plan", "boq_site_overhead", "boq_logistics", "boq_contingency", "boq_profit_margin",
            "pg_scenario_percent", "pg_whatif_percent",
        ):
            st.session_state.pop(key, None)
        # The selectbox has already been instantiated in this run; reset it before the next render.
        st.session_state["pending_saved_analysis_choice"] = None
        st.session_state["nit_uploader_version"] += 1
        st.session_state["boq_editor_version"] += 1

    with st.expander(f"Saved tender analyses ({len(saved_analyses)})", expanded=bool(saved_analyses)):
        if saved_analyses:
            selected_saved_id = st.selectbox(
                "Choose a saved analysis", [None] + list(saved_by_id), key="saved_analysis_choice",
                format_func=lambda value: "Select an analysis" if value is None else (
                    f"{saved_by_id[value]['title']} · {saved_by_id[value]['nit_no'] or 'No NIT number'} · "
                    f"updated {saved_by_id[value]['updated_at']}"
                ),
            )
            load_col, new_col, delete_col = st.columns(3)
            if load_col.button("Load into estimator", disabled=selected_saved_id is None, width="stretch"):
                saved = get_tender_analysis(int(selected_saved_id))
                if saved:
                    st.session_state["active_saved_analysis_id"] = int(saved["id"])
                    st.session_state["est_zone_circle"] = saved["zone_circle"] or ""
                    st.session_state["est_division"] = saved["division"] or ""
                    st.session_state["est_location"] = saved["location"] or ""
                    st.session_state["est_cost"] = float(saved["estimated_cost"] or 0)
                    st.session_state["est_description"] = saved["work_name"] or ""
                    st.session_state["est_work_type"] = saved["work_type"] or classify_work_type(saved["work_name"])
                    st.session_state["est_opening"] = pd.to_datetime(saved["bid_opening_date"], errors="coerce").date() if saved["bid_opening_date"] else date.today()
                    st.session_state["nit_extraction"] = saved["extraction"]
                    st.session_state["nit_source_filename"] = saved["source_filename"]
                    costing = saved.get("costing") or {}
                    saved_result = saved.get("result") or {}
                    st.session_state["boq_costing_plan"] = costing
                    st.session_state["boq_site_overhead"] = float(costing.get("site_overhead_percent", 0))
                    st.session_state["boq_logistics"] = float(costing.get("logistics_percent", 0))
                    st.session_state["boq_contingency"] = float(costing.get("contingency_percent", 0))
                    st.session_state["boq_profit_margin"] = float(costing.get("target_profit_margin_percent", 5))
                    st.session_state["pg_scenario_percent"] = float(saved_result.get("planned_bid_percent") or 0)
                    st.session_state.pop("nit_pdf_signature", None)
                    st.session_state.pop("last_analysis_result", None)
                    st.session_state.pop("analysis_render", None)
                    st.session_state["nit_uploader_version"] += 1
                    st.session_state["boq_editor_version"] += 1
                    st.rerun()
            if new_col.button("Start new analysis", width="stretch"):
                clear_analysis_workspace()
                st.rerun()
            confirm_delete = delete_col.checkbox("Confirm delete", disabled=selected_saved_id is None)
            if delete_col.button("Delete saved analysis", disabled=selected_saved_id is None or not confirm_delete, width="stretch"):
                delete_tender_analysis(int(selected_saved_id))
                sync_after_write()
                if st.session_state.get("active_saved_analysis_id") == selected_saved_id:
                    clear_analysis_workspace()
                else:
                    st.session_state["pending_saved_analysis_choice"] = None
                st.rerun()
        else:
            st.caption("No analyses have been saved yet. Your first completed estimator run will appear here.")

    active_id = st.session_state.get("active_saved_analysis_id")
    if active_id:
        active = saved_by_id.get(int(active_id))
        st.info(f"Editing saved analysis: **{active['title'] if active else f'#{active_id}'}**. A new PDF upload will replace its extracted snapshot when you re-run.")

    extraction = st.session_state.get("nit_extraction")
    with st.expander("① Upload NIT PDF to auto-fill  ·  optional", expanded=extraction is None):
        st.caption(
            "No NIT PDF? Skip this — you can type the tender details yourself below. Uploading a text-based "
            "NIT just pre-fills the form and enables BOQ cost & profit planning."
        )
        uploaded = st.file_uploader(
            "Upload tender NIT (PDF)", type=["pdf"],
            help="Maximum 5 MB. Upload a replacement while editing a saved analysis to refresh its extracted information.",
            key=f"nit_pdf_upload_{st.session_state['nit_uploader_version']}",
            max_upload_size=5,
        )
        if uploaded and uploaded.size > 5 * 1024 * 1024:
            st.error("The PDF exceeds the 5 MB limit.")
        elif uploaded:
            uploaded_content = uploaded.getvalue()
            signature = (uploaded.name, uploaded.size, hashlib.sha256(uploaded_content).hexdigest())
            if st.session_state.get("nit_pdf_signature") != signature:
                try:
                    with centered_loader("Reading the NIT and extracting tender details…"):
                        new_extraction = extract_nit_pdf(uploaded_content, uploaded.name)
                    st.session_state["nit_extraction"] = new_extraction
                    st.session_state["nit_pdf_signature"] = signature
                    st.session_state["nit_source_filename"] = uploaded.name
                    st.session_state["est_zone_circle"] = ""
                    st.session_state["est_division"] = new_extraction.division or ""
                    st.session_state["est_location"] = new_extraction.location or ""
                    st.session_state["est_cost"] = float(new_extraction.estimated_cost or 0)
                    st.session_state["est_description"] = new_extraction.work_name or ""
                    st.session_state["est_work_type"] = classify_work_type(new_extraction.work_name)
                    st.session_state["est_opening"] = new_extraction.bid_opening.date() if new_extraction.bid_opening else date.today()
                    st.session_state["boq_costing_plan"] = {}
                    st.session_state["pg_scenario_percent"] = 0.0
                    st.session_state.pop("analysis_render", None)
                    for key in ("boq_site_overhead", "boq_logistics", "boq_contingency", "boq_profit_margin"):
                        st.session_state.pop(key, None)
                    st.session_state["boq_editor_version"] += 1
                    st.rerun()
                except ValueError as exc:
                    st.session_state.pop("nit_extraction", None)
                    st.error(str(exc))
                except Exception:
                    LOG.exception("NIT PDF extraction failed")
                    st.session_state.pop("nit_extraction", None)
                    st.error("The NIT could not be extracted. Your historical database was not changed.")
    extraction = st.session_state.get("nit_extraction")
    if extraction:
        _show_nit_extraction(extraction)
    st.subheader("② Review extracted details" if extraction else "② Enter tender details")
    if not extraction:
        st.caption("Fill in what you know — historical values are suggested where available. None of this requires a NIT PDF.")
    st.session_state.setdefault("est_zone_circle", "")
    st.session_state.setdefault("est_division", "")
    st.session_state.setdefault("est_location", "")
    st.session_state.setdefault("est_cost", 0.0)
    st.session_state.setdefault("est_description", "")
    st.session_state.setdefault("est_work_type", classify_work_type(st.session_state["est_description"]))
    st.session_state.setdefault("est_opening", date.today())
    st.session_state.setdefault("boq_costing_plan", {})
    st.session_state.setdefault("boq_site_overhead", 0.0)
    st.session_state.setdefault("boq_logistics", 0.0)
    st.session_state.setdefault("boq_contingency", 0.0)
    st.session_state.setdefault("boq_profit_margin", 5.0)
    st.session_state.setdefault("pg_scenario_percent", 0.0)

    def office_options(column: str, current: str) -> tuple[list[str], pd.Series]:
        values = df.get(column, pd.Series(dtype=str)).dropna().astype(str)
        values = values[values.str.strip().ne("")]
        counts = values.value_counts()
        options = sorted(counts.index.tolist())
        if current and current not in options:
            options.insert(0, current)
        return [""] + options, counts

    zone_options, zone_counts = office_options("zone_circle", st.session_state["est_zone_circle"])
    division_options, division_counts = office_options("division", st.session_state["est_division"])

    def office_label(value: str, counts: pd.Series) -> str:
        if not value:
            return "Not selected"
        count = int(counts.get(value, 0))
        return f"{value} — {count:,} tenders" if count else value

    category_editor = None
    override_editor = None
    all_items_editor = None
    component_totals = ({
        "Civil Works": extraction.civil_estimated_cost,
        "E&M Works": extraction.electrical_estimated_cost,
    } if extraction else None)
    boq_items = reconcile_boq_items(
        extraction.boq_items, extraction.boq_total or extraction.estimated_cost, component_totals,
    ) if extraction else []
    existing_costing = st.session_state.get("boq_costing_plan") or {}
    with st.form("estimator"):
        c1, c2, c3, c4 = st.columns(4)
        zone_circle = c1.selectbox(
            "Zone / Circle", zone_options, key="est_zone_circle",
            format_func=lambda value: office_label(value, zone_counts), accept_new_options=True,
            help="Choose a historical value or type a new Zone / Circle.",
        )
        division = c2.selectbox(
            "Division", division_options, key="est_division",
            format_func=lambda value: office_label(value, division_counts), accept_new_options=True,
            help="The detected division is preselected. Choose another historical value or type a new one.",
        )
        location = c3.text_input("Location", key="est_location")
        estimated = c4.number_input("Estimated Cost (₹)", min_value=0.0, step=100000.0, key="est_cost")
        description = st.text_area("Name of work", key="est_description")
        work_type = st.selectbox("Work type", CATEGORIES, key="est_work_type")
        contractor_counts = awards["contractor_name"].value_counts() if not awards.empty else pd.Series(dtype=int)
        contractor = st.selectbox("Possible contractor (optional)", [""] + sorted(contractor_map),
                                  format_func=lambda value: "Not selected" if not value else f"{value} — {int(contractor_counts.get(value, 0)):,} awards")
        opening = st.date_input("Expected bid opening date", key="est_opening")
        planned_bid_percent = st.number_input(
            "Planned bid position (% above / below) — optional",
            min_value=-99.99, max_value=200.0, step=0.1, format="%.2f", key="pg_scenario_percent",
            help="Your intended bid relative to the estimate. Negative = below, positive = above. Saved with the "
                 "analysis; after running you can also explore other positions live in the results.",
        )
        if boq_items:
            st.markdown("### BOQ cost and profit planning")
            st.caption(
                "Set a managed-cost percentage for each category, then enter actual unit costs for high-value items or any BOQ item. "
                "These are internal execution costs; percentage-rate tenders may still require one overall above/below quote."
            )
            percentages = existing_costing.get("category_percentages", {})
            unallocated = [item for item in boq_items if str(item.get("item_no") or "").startswith("UNALLOCATED")]
            priced_boq_items = [item for item in boq_items if not str(item.get("item_no") or "").startswith("UNALLOCATED")]
            if unallocated:
                balance_text = ", ".join(
                    f"{item.get('work_part', 'BOQ')}: {format_inr_compact(item['amount'])}" for item in unallocated
                )
                st.warning(
                    f"Extracted priced rows do not represent the full stated total ({balance_text}). "
                    "Separate work-part balances are retained so Civil, E&M and overall profitability still reconcile."
                )
            category_frame = pd.DataFrame(category_summary(boq_items)).rename(columns={
                "work_part": "Work Part", "category": "Category", "item_count": "Items", "boq_amount": "BOQ Amount",
            })
            category_frame["Planned Cost %"] = category_frame.apply(
                lambda row: float(percentages.get(
                    costing_category_key(str(row["Work Part"]), str(row["Category"])),
                    percentages.get(str(row["Category"]), 100.0),
                )),
                axis=1,
            )
            category_editor = st.data_editor(
                category_frame, hide_index=True, width="stretch", key=f"boq_categories_{st.session_state['boq_editor_version']}",
                disabled=["Work Part", "Category", "Items", "BOQ Amount"],
                column_config={
                    "BOQ Amount": st.column_config.NumberColumn(format="₹ %.0f"),
                    "Planned Cost %": st.column_config.NumberColumn(
                        "Managed cost as % of BOQ", min_value=0.0, max_value=200.0, step=1.0, format="%.1f%%",
                    ),
                },
            )
            major_items = pareto_items(priced_boq_items)
            major_total = sum(float(item.get("boq_amount") or 0) for item in major_items)
            boq_value = sum(float(item.get("amount") or 0) for item in priced_boq_items)
            coverage = major_total / boq_value * 100 if boq_value else 0
            st.markdown("#### High-value item overrides")
            st.caption(
                f"{len(major_items)} items cover {coverage:.1f}% of the extracted priced-row value. Leave Actual Unit Cost blank to use the category percentage."
            )
            overrides = existing_costing.get("item_overrides", {})
            override_frame = pd.DataFrame([{
                "Item Key": item["item_key"], "Work Part": item["work_part"], "Item": item.get("item_no"), "Category": item["category"],
                "Description": item.get("description"), "Quantity": item.get("quantity"), "Unit": item.get("unit"),
                "BOQ Rate": item.get("rate"), "BOQ Amount": item.get("boq_amount"),
                "Actual Unit Cost": overrides.get(item["item_key"]),
            } for item in major_items])
            override_editor = st.data_editor(
                override_frame, hide_index=True, width="stretch", height=420,
                key=f"boq_overrides_{st.session_state['boq_editor_version']}",
                disabled=["Item Key", "Work Part", "Item", "Category", "Description", "Quantity", "Unit", "BOQ Rate", "BOQ Amount"],
                column_config={
                    "Item Key": None,
                    "Description": st.column_config.TextColumn(width="large"),
                    "BOQ Rate": st.column_config.NumberColumn(format="₹ %.2f"),
                    "BOQ Amount": st.column_config.NumberColumn(format="₹ %.0f"),
                    "Actual Unit Cost": st.column_config.NumberColumn(min_value=0.0, step=1.0, format="₹ %.2f"),
                },
            )
            with st.expander(f"All BOQ item-wise overrides ({len(priced_boq_items)} extracted items)"):
                st.caption(
                    "Fallback detailed entry: enter an Actual Unit Cost for any Civil or E&M item. Blank rows continue to use the category percentage."
                )
                all_frame = pd.DataFrame([{
                    "Item Key": item["item_key"], "Work Part": item["work_part"], "Item": item.get("item_no"),
                    "Category": item["category"], "Description": item.get("description"),
                    "Quantity": item.get("quantity"), "Unit": item.get("unit"),
                    "BOQ Rate": item.get("rate"), "BOQ Amount": item.get("boq_amount"),
                    "Actual Unit Cost": overrides.get(item["item_key"]),
                } for item in prepare_boq_items(priced_boq_items)])
                all_items_editor = st.data_editor(
                    all_frame, hide_index=True, width="stretch", height=620,
                    key=f"boq_all_items_{st.session_state['boq_editor_version']}",
                    disabled=["Item Key", "Work Part", "Item", "Category", "Description", "Quantity", "Unit", "BOQ Rate", "BOQ Amount"],
                    column_config={
                        "Item Key": None,
                        "Description": st.column_config.TextColumn(width="large"),
                        "BOQ Rate": st.column_config.NumberColumn(format="₹ %.2f"),
                        "BOQ Amount": st.column_config.NumberColumn(format="₹ %.0f"),
                        "Actual Unit Cost": st.column_config.NumberColumn(min_value=0.0, step=1.0, format="₹ %.2f"),
                    },
                )
            p1, p2, p3, p4 = st.columns(4)
            site_overhead = p1.number_input("Site & office overhead (%)", min_value=0.0, max_value=100.0, step=0.5, key="boq_site_overhead")
            logistics = p2.number_input("Logistics / location loading (%)", min_value=0.0, max_value=100.0, step=0.5, key="boq_logistics")
            contingency = p3.number_input("Contingency / risk (%)", min_value=0.0, max_value=100.0, step=0.5, key="boq_contingency")
            profit_margin = p4.number_input("Target profit margin (%)", min_value=0.0, max_value=50.0, step=0.5, key="boq_profit_margin")
        submit_label = "Re-run & Update Saved Analysis" if active_id else "Analyze & Save Tender"
        submitted = st.form_submit_button(
            submit_label,
            type="primary",
            on_click=begin_action,
            args=("Analyzing and saving the tender…",),
        )

    if submitted:
        analysis_inputs = {
            "nit_no": extraction.nit_no if extraction else None,
            "zone_circle": zone_circle or None, "division": division or None, "location": location or None,
            "estimated_cost": estimated, "work_description": description, "work_type": work_type,
            "contractor_id": contractor_map.get(contractor), "bid_opening_date": opening,
            "planned_bid_percent": planned_bid_percent,
        }
        with centered_loader("Estimating the bid from historical comparables…"):
            result = estimate_bid(awards, analysis_inputs)
        result["planned_bid_percent"] = float(planned_bid_percent)
        result["performance_guarantee"] = (
            _performance_guarantee_for_tender(estimated, planned_bid_percent, extraction) if estimated > 0 else None
        )
        costing_result = None
        costing_plan = {} if extraction is not None else None
        if category_editor is not None and override_editor is not None:
            category_percentages = {
                costing_category_key(str(row["Work Part"]), str(row["Category"])): float(row["Planned Cost %"])
                for _, row in category_editor.iterrows()
            }
            existing_overrides = {
                str(key): float(value) for key, value in existing_costing.get("item_overrides", {}).items()
                if value is not None and float(value) > 0
            }
            item_overrides = dict(existing_overrides)

            def apply_editor_changes(editor: pd.DataFrame | None) -> None:
                if editor is None:
                    return
                for _, row in editor.iterrows():
                    key = str(row["Item Key"])
                    raw_value = row["Actual Unit Cost"]
                    new_value = float(raw_value) if pd.notna(raw_value) and float(raw_value) > 0 else None
                    old_value = existing_overrides.get(key)
                    if new_value == old_value:
                        continue
                    if new_value is None:
                        item_overrides.pop(key, None)
                    else:
                        item_overrides[key] = new_value

            # High-value edits are the shortcut; a simultaneous change in the full BOQ editor is the final override.
            apply_editor_changes(override_editor)
            apply_editor_changes(all_items_editor)
            costing_result = calculate_boq_costing(
                boq_items, category_percentages, item_overrides,
                site_overhead_percent=site_overhead, logistics_percent=logistics,
                contingency_percent=contingency, target_profit_margin_percent=profit_margin,
            )
            costing_plan = {
                "version": 2, "category_percentages": category_percentages, "item_overrides": item_overrides,
                "site_overhead_percent": site_overhead, "logistics_percent": logistics,
                "contingency_percent": contingency, "target_profit_margin_percent": profit_margin,
                "summary": {key: value for key, value in costing_result.items() if key != "items"},
            }
            st.session_state["boq_costing_plan"] = costing_plan
        saved_id = save_tender_analysis(
            analysis_inputs, result, extraction=extraction, analysis_id=active_id,
            source_filename=st.session_state.get("nit_source_filename"), costing_plan=costing_plan,
        )
        st.session_state["active_saved_analysis_id"] = saved_id
        st.session_state["pending_saved_analysis_choice"] = saved_id
        st.session_state["last_analysis_result"] = result
        st.session_state["analysis_render"] = {
            "result": result, "costing_result": costing_result, "estimated": estimated, "extraction": extraction,
        }
        st.session_state["pg_whatif_percent"] = float(planned_bid_percent)
        sync_after_write()
        st.success("Tender analysis updated." if active_id else "Tender analysis saved. You can load, re-run, replace, or delete it from Saved tender analyses.")

    bundle = st.session_state.get("analysis_render")
    if bundle:
        _render_analysis_results(bundle["result"], bundle["costing_result"], bundle["estimated"], bundle.get("extraction"))


render_master_reset()
data = load_session_tenders()
nav_options = PAGES if not data.empty else ONBOARDING_PAGES
# Apply a pending redirect (e.g. jump to Dashboard after a successful import) before the
# radio widget is instantiated, since a widget's own key cannot be written afterwards.
if pending_nav := st.session_state.pop("pending_nav", None):
    if pending_nav in nav_options:
        st.session_state["nav_page"] = pending_nav
page = st.sidebar.radio("Navigation", nav_options, key="nav_page")
render_masthead()
try:
    if page == "Dashboard":
        dashboard_page(data)
    elif page == "Contractors":
        contractors_page(data)
    elif page == "Tender Analysis & Bid Estimator":
        estimator_page(data)
except Exception:
    LOG.exception("Unhandled page error on %s", page)
    st.error("This page could not be displayed. Your data has not been deleted. See the app logs for technical details.")
finally:
    clear_action_overlay()

st.html(
    """
    <footer style="margin-top:32px;padding:14px 4px 4px;border-top:1px solid #d8e1e5;
                   color:#5b6b73;font-size:.78rem;line-height:1.45;">
      <div><strong>🔒 Your data and privacy:</strong> Imported records are saved only in the browser
           you are using, so other users cannot see them. Original uploaded files are not retained.</div>
      <div style="margin-top:4px;"><strong>Public-data notice:</strong> This app is intended for tender
           information published publicly on the CPWD e-Tendering website. Verify important details
           against the official CPWD record.</div>
    </footer>
    """
)
