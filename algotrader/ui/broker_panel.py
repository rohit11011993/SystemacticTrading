"""Settings -> Zerodha Kite: guided broker setup in the desktop UI.

Steps (same as the ``kite-*`` CLI commands, see ``execution/kite_setup.py``):
  1. Save API key + secret  -> Windows Credential Manager (never files or logs)
  2. Log in (daily)         -> opens the Kite login page; you enter password + 2FA yourself;
                               the token is captured from the local redirect or pasted
  3. Check connection       -> session, account, enabled segments, static IP
  4. Update instruments     -> current contracts, lot sizes, expiries
  5. Download history       -> real daily data into the data folder

Network calls run in a background thread so the window never freezes.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QVBoxLayout)

from ..execution import kite_setup as ks
from .widgets import StateBadge

if TYPE_CHECKING:
    from .main_window import MainWindow


class TaskWorker(QThread):
    """Run one blocking call off the UI thread; report the result or the error text."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self.fn())
        except Exception as exc:  # noqa: BLE001 - shown to the operator verbatim
            self.failed.emit(str(exc))


class BrokerPanel(QGroupBox):
    def __init__(self, win: "MainWindow", factory: Callable = ks.default_client_factory,
                 secrets: ks.SecretStore | None = None, interactive: bool = True):
        super().__init__("Broker: Zerodha Kite Connect")
        self.win = win
        self.bridge = win.bridge
        self.cfg = self.bridge.cfg
        self.factory = factory
        self.interactive = interactive
        self._secrets = secrets
        self._workers: list[TaskWorker] = []

        lay = QVBoxLayout(self)
        intro = QLabel(
            "Needs a Kite Connect app (developers.kite.trade -> My apps). Set the app's <b>Redirect URL</b> to "
            f"<code>{self.cfg.system.kite_redirect_url}</code>. Log in once every trading day; sessions expire "
            "overnight. Logging in enables data download and connection checks; it does <b>not</b> start live "
            "trading.")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        form = QFormLayout()
        self.b_app, self.b_session, self.b_contracts = StateBadge(), StateBadge(), StateBadge()
        self.l_data = QLabel("-")
        self.l_ip = QLabel(self.cfg.system.static_ip or "not set (needed for live trading, REG-1)")
        form.addRow("API key and secret", self.b_app)
        form.addRow("Today's session", self.b_session)
        form.addRow("Contract table", self.b_contracts)
        form.addRow("Market data", self.l_data)
        form.addRow("Static IP", self.l_ip)
        lay.addLayout(form)

        row1, row2 = QHBoxLayout(), QHBoxLayout()
        self.btn = {}
        for row, key, text, fn in (
                (row1, "app", "1. Set API key && secret...", self.ask_app),
                (row1, "login", "2. Log in to Kite...", self.start_login),
                (row1, "paste", "Paste login address...", self.ask_paste),
                (row2, "check", "3. Check connection", self.check),
                (row2, "instr", "4. Update instruments", self.update_instruments),
                (row2, "fetch", "5. Download history...", self.ask_fetch),
                (row2, "logout", "Log out && remove credentials", self.logout)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
            self.btn[key] = b
        self.btn["logout"].setObjectName("Danger")
        row1.addStretch(1)
        row2.addStretch(1)
        lay.addLayout(row1)
        lay.addLayout(row2)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(160)
        self.log.setPlaceholderText("Results of each step appear here.")
        lay.addWidget(self.log)
        self.refresh_status()

    # -- helpers ---------------------------------------------------------------------------
    @property
    def secrets(self) -> ks.SecretStore:
        if self._secrets is None:
            self._secrets = ks.SecretStore()
        return self._secrets

    def say(self, text: str) -> None:
        self.log.appendPlainText(text)
        self.win.status(text.splitlines()[0][:200])

    def run(self, label: str, fn: Callable[[], Any], on_done: Callable[[Any], None]) -> TaskWorker:
        self.say(f"{label}...")
        w = TaskWorker(fn)
        w.done.connect(lambda res: (on_done(res), self.refresh_status()))
        w.failed.connect(lambda err: (self.say(f"{label} failed: {err}"), self.refresh_status()))
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()
        return w

    def _audit(self, event: str, payload: dict) -> None:
        """Logins and credential changes are audited (FR-11.2); secrets are never logged."""
        from ..core.audit import AuditLog
        AuditLog(self.bridge.audit_path).record(event, "operator(ui)", payload)

    def refresh_status(self) -> None:
        try:
            st = self.secrets.status()
            key = self.secrets.api_key
            self.b_app.set_state("ok" if st["app_configured"] else "bad",
                                 f"saved ({ks.mask(key)})" if st["app_configured"] else "not set - step 1")
            self.b_session.set_state("ok" if st["session_today"] else "warn",
                                     "logged in today" if st["session_today"] else "not logged in - step 2")
        except ks.KiteSetupError as exc:
            self.b_app.set_state("bad", "credential store unavailable")
            self.b_session.set_state("neutral", str(exc)[:80])
        table = self.bridge.store.get(ks.CONTRACTS_KEY)
        if table:
            fresh = table.get("updated") == date.today().isoformat()
            self.b_contracts.set_state("ok" if fresh else "warn", f"updated {table.get('updated')}"
                                       + ("" if fresh else " - refresh today (step 4)"))
        else:
            self.b_contracts.set_state("neutral", "not created yet - step 4")
        self.l_data.setText(self._data_status())

    def _data_status(self) -> str:
        data_dir = self.bridge.base / self.cfg.system.data_dir
        feed = self.bridge.registry.get(self.cfg.portfolio.regime.market_series).feed
        path = data_dir / f"{feed}.csv"
        if not path.exists():
            return f"no data in {data_dir}"
        last = path.read_text(encoding="utf-8").strip().splitlines()[-1].split(",")[0]
        kind = "downloaded from Kite" if (data_dir / "_demo_backup").exists() else "demo data (synthetic)"
        return f"{kind}, last bar {last[:10]}  ({data_dir})"

    # -- step 1 ----------------------------------------------------------------------------
    def ask_app(self) -> None:
        key, ok = QInputDialog.getText(self, "Kite API key", "API key (from developers.kite.trade -> My apps):")
        if not ok or not key.strip():
            return
        secret, ok = QInputDialog.getText(self, "Kite API secret", "API secret (hidden):", QLineEdit.Password)
        if ok:
            self.save_app(key, secret)

    def save_app(self, key: str, secret: str) -> None:
        try:
            self.secrets.save_app(key, secret)
            self._audit("kite_setup", {"api_key": ks.mask(key)})
            self.say(f"API key {ks.mask(key)} and secret saved in the credential store. Next: 2. Log in.")
        except ks.KiteSetupError as exc:
            self.say(f"Could not save: {exc}")
        self.refresh_status()

    # -- step 2 ----------------------------------------------------------------------------
    def start_login(self) -> None:
        try:
            url = ks.login_url(self.secrets, self.factory)
        except ks.KiteSetupError as exc:
            self.say(str(exc))
            return
        redirect = self.cfg.system.kite_redirect_url
        self.say("Opening the Kite login page in your browser. Sign in with your password and 2FA.\n"
                 f"AlgoTrader is listening on {redirect} for 3 minutes. If the browser shows an error page "
                 "after login, use 'Paste login address...' with the address from the browser.")
        QDesktopServices.openUrl(QUrl(url))
        self.run("Waiting for the Kite login", lambda: ks.capture_request_token(redirect, 180),
                 lambda token: self.finish_login(token) if token else
                 self.say("No login received automatically. Use 'Paste login address...'."))

    def ask_paste(self) -> None:
        text, ok = QInputDialog.getText(self, "Kite login", "Paste the full address shown in the browser after "
                                        "logging in (it contains request_token=):")
        if ok and text.strip():
            try:
                self.finish_login(ks.parse_request_token(text))
            except ks.KiteSetupError as exc:
                self.say(str(exc))

    def finish_login(self, request_token: str) -> TaskWorker:
        return self.run("Completing the login", lambda: ks.complete_login(self.secrets, request_token, self.factory),
                        lambda info: (self._audit("kite_login", info),
                                      self.say(f"Logged in as {info['user_name']} ({info['user_id']}). "
                                               "Next: 3. Check connection.")))

    # -- steps 3-5 -------------------------------------------------------------------------
    def check(self) -> TaskWorker:
        def job():
            from ..execution.kite import KiteAuth
            kc = ks.connect(self.secrets, self.factory)
            keys = sorted({k for b in self.cfg.bindings.bindings if b.enabled for k in b.instruments})
            ip = KiteAuth(self.secrets.api_key, self.cfg.system.static_ip).check_static_ip()
            return ks.check_connection(kc, self.bridge.registry, keys, ip)
        return self.run("Checking the connection", job,
                        lambda res: self.say(("All checks passed.\n" if res.ok else "Some checks failed:\n")
                                             + "\n".join(res.lines)))

    def update_instruments(self) -> TaskWorker:
        def job():
            kc = ks.connect(self.secrets, self.factory)
            table, notes = ks.resolve_contracts(kc, self.bridge.registry, self.bridge.registry.keys())
            self.bridge.store.set(ks.CONTRACTS_KEY, table)
            return table, notes

        def done(res):
            table, notes = res
            lines = [f"{k}: {c['tradingsymbol']} lot {c['lot_size']} expiry {c['expiry']}"
                     for k, c in sorted(table["futures"].items())]
            lines += [f"{k}: {len(o['chain'])} option contracts" for k, o in table["options"].items()]
            self.say("Contracts updated:\n" + "\n".join(lines + [f"NOTE {n}" for n in notes]))
        return self.run("Updating instruments", job, done)

    def ask_fetch(self) -> None:
        start, ok = QInputDialog.getText(self, "Download history", "Download daily history from (YYYY-MM-DD):",
                                         QLineEdit.Normal, "2018-01-01")
        if ok and start.strip():
            if self.win.confirm_action("Download history", "Replace the data in the data folder with Kite history?",
                                       "Demo files are kept in data\\_demo_backup. Delete the state folder before "
                                       "paper trading on real data so demo trades are not mixed in.", danger=False):
                self.fetch(date.fromisoformat(start.strip()))

    def fetch(self, start: date) -> TaskWorker:
        data_dir = Path(self.bridge.base) / self.cfg.system.data_dir
        lines: list[str] = []

        def job():
            kc = ks.connect(self.secrets, self.factory)
            table = self.bridge.store.get(ks.CONTRACTS_KEY)
            if not table:
                table, _ = ks.resolve_contracts(kc, self.bridge.registry, self.bridge.registry.keys())
                self.bridge.store.set(ks.CONTRACTS_KEY, table)
            return ks.fetch_history(kc, table, self.bridge.registry, data_dir, start, log=lines.append)
        return self.run("Downloading history (this can take a few minutes)", job,
                        lambda paths: self.say(f"Downloaded {len(paths)} files into {data_dir}:\n" + "\n".join(lines)))

    def logout(self) -> None:
        if not self.win.confirm_action("Kite logout", "Log out and remove the stored API key, secret and token?",
                                       "You will need step 1 and 2 again. Also revoke the app at developers.kite.trade "
                                       "if you stop using it."):
            return
        try:
            try:
                ks.connect(self.secrets, self.factory).invalidate_access_token()
            except Exception:  # noqa: BLE001 - removing local credentials is what matters
                pass
            self.secrets.revoke()
            self._audit("kite_logout", {})
            self.say("Logged out; credentials removed from the credential store.")
        except ks.KiteSetupError as exc:
            self.say(str(exc))
        self.refresh_status()
