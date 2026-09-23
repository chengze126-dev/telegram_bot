"""Entry point: ``python -m app.main``."""

from __future__ import annotations

import argparse
import logging
import os
import sys


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram lead monitor for authorized groups")
    parser.add_argument("--debug", action="store_true", help="verbose logging (also printed to the console)")
    parser.add_argument("--minimized", action="store_true", help="start hidden in the system tray")
    parser.add_argument("--data-dir", help="override the local data folder (database, session, logs)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.data_dir:
        os.environ["TLM_DATA_DIR"] = args.data_dir

    # Imported after the data dir override so config picks it up.
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.config import APP_ID, APP_NAME, APP_VERSION, Paths, SettingsStore
    from app.database.db import Database
    from app.services.logging_service import setup_logging
    from app.services.worker import TelegramWorker
    from app.ui.main_window import MainWindow
    from app.ui.theme import STYLESHEET, app_font, app_icon

    paths = Paths()
    log_handler = setup_logging(paths.log_file, debug=args.debug)
    log = logging.getLogger("app")
    log.info("%s %s starting (data folder: %s)", APP_NAME, APP_VERSION, paths.root)

    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_ID)
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(app_icon())
    app.setStyle("Fusion")
    app.setFont(app_font())
    app.setStyleSheet(STYLESHEET)
    app.setQuitOnLastWindowClosed(False)

    try:
        db = Database(paths.database)
    except Exception as exc:  # corrupt or locked database
        log.exception("Could not open the database")
        QMessageBox.critical(None, APP_NAME, f"Could not open the local database:\n{paths.database}\n\n{exc}")
        return 1

    settings = SettingsStore(paths.settings)
    worker = TelegramWorker(db, settings, paths)
    window = MainWindow(db, settings, paths, worker, log_handler)

    start_hidden = (args.minimized or settings.get().start_minimized) and window.tray is not None
    if not start_hidden:
        window.show()

    worker.start()

    def shutdown() -> None:
        log.info("Shutting down")
        worker.shutdown()
        db.close_all()

    app.aboutToQuit.connect(shutdown)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
