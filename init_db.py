import os
import sys
import logging
import shutil
import datetime
import traceback
import argparse

from artha import create_app
from artha.extensions import db

app = create_app("development")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Constants
BACKUP_RETENTION_DAYS = int(os.getenv("DB_BACKUP_RETENTION_DAYS", "30"))
SKIP_CONFIRM_ENV = os.getenv("INIT_DB_SKIP_CONFIRM", "").lower() in ("1", "true", "yes")

def cleanup_old_backups(backup_dir, retention_days):
    now = datetime.datetime.now()
    cutoff = now - datetime.timedelta(days=retention_days)
    for f in os.listdir(backup_dir):
        if f.startswith("site.db") and f.endswith(".bak"):
            full_path = os.path.join(backup_dir, f)
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(full_path))
            if mtime < cutoff:
                try:
                    os.remove(full_path)
                    logging.info(f"🗑️ Removed old backup: {f}")
                except Exception as e:
                    logging.warning(f"Failed to remove old backup {f}: {e}")

def main():
    # Production safeguard
    if os.getenv("FLASK_ENV") == "production":
        logging.error("❌ Refusing to run init_db.py in production environment.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Initialize the database with safety features.")
    parser.add_argument("--skip-confirm", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--restore", type=str, help="Backup filename to restore from")
    parser.add_argument("--force", action="store_true", help="Force reset DB without confirmation")
    parser.add_argument("--no-interaction", action="store_true", help="Disable all prompts and confirmations")
    args = parser.parse_args()

    # Determine flags
    skip_confirm = SKIP_CONFIRM_ENV or args.skip_confirm or args.no_interaction
    force_reset = args.force

    if not skip_confirm and not force_reset:
        confirm = input("⚠️  This will initialize the database. Type 'YES' to continue: ")
        if confirm.strip().upper() != "YES":
            logging.info("Initialization cancelled by user.")
            sys.exit(0)

    with app.app_context():
        db_path = os.path.join(app.instance_path, "site.db")

        # Cleanup old backups
        cleanup_old_backups(app.instance_path, BACKUP_RETENTION_DAYS)

        backups = [f for f in os.listdir(app.instance_path) if f.startswith("site.db") and f.endswith(".bak")]
        if backups and not force_reset:
            logging.info("Available backups:")
            for b in backups:
                logging.info(f" - {b}")
            logging.info("Press Enter to skip restore and continue initialization.")
        else:
            logging.info("No backups found or force reset requested.")

        # Determine restore choice
        restore_choice = args.restore
        if restore_choice is None and not args.no_interaction and not force_reset:
            restore_choice = input("Do you want to restore a previous backup instead? Type backup filename or press Enter to skip: ").strip()

        if restore_choice:
            backup_path = os.path.join(app.instance_path, restore_choice)
            if os.path.exists(backup_path):
                shutil.copy2(backup_path, db_path)
                logging.info(f"🔄 Restored database from {backup_path}")
                sys.exit(0)
            else:
                logging.error(f"Backup file {backup_path} not found.")
                sys.exit(1)
        elif backups and not force_reset:
            latest_backup = max(
                [os.path.join(app.instance_path, b) for b in backups],
                key=os.path.getmtime
            )
            shutil.copy2(latest_backup, db_path)
            logging.info(f"🔄 Automatically restored most recent backup: {latest_backup}")
            sys.exit(0)

        if os.path.exists(db_path) and not force_reset:
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{db_path}.{timestamp}.bak"
            shutil.copy2(db_path, backup_path)
            logging.info(f"📦 Existing database backed up to {backup_path}")

        try:
            db.create_all()
            logging.info("✅ Database tables created successfully.")
        except Exception as e:
            logging.error("❌ Failed to create database tables:")
            logging.error(traceback.format_exc())
            sys.exit(1)

if __name__ == "__main__":
    main()