"""Backup, verification, retention, restore and the weekly restore drill (6.18)."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .store import Database, sqlite_backup
from .util import age_str, sha256_file, to_et

log = logging.getLogger(__name__)

APP_VERSION = "1.0.0"
MAGIC = b"PENNYBKP1\n"

CRITICAL_TABLES = ("alerts", "watch", "daily", "predictions", "leaderboard_history",
                   "compliance", "backups", "kv")


def safe_extract(tar: tarfile.TarFile, path) -> None:
    """Extract an archive we created ourselves, rejecting unsafe members.

    The ``data`` filter exists on Python 3.12+ (and patched 3.8-3.11); fall back
    to a plain extraction on interpreters that do not accept it.
    """
    try:
        tar.extractall(path, filter="data")
    except TypeError:  # pragma: no cover - older interpreters
        tar.extractall(path)


@dataclass
class BackupResult:
    kind: str
    destination: str
    path: Optional[str] = None
    size: int = 0
    sha256: str = ""
    status: str = "ok"
    note: str = ""
    verified: bool = False
    row_counts: dict = field(default_factory=dict)


class BackupManager:
    def __init__(self, cfg, state_db: Database, journal_db: Database, telegram=None,
                 secrets=None):
        self.cfg = cfg
        self.state = state_db
        self.journal = journal_db
        self.telegram = telegram
        self.secrets = list(secrets or [])

    # -- paths --------------------------------------------------------------
    @property
    def local_dir(self) -> Path:
        raw = self.cfg.raw("BACKUP_LOCAL_DIR")
        base = Path(raw) if raw else self.cfg.home / "backups"
        base.mkdir(parents=True, exist_ok=True)
        return base

    @property
    def staging_dir(self) -> Path:
        p = self.cfg.home / "backups" / ".staging"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def passphrase(self) -> str:
        return str(self.cfg.raw("BACKUP_PASSPHRASE") or "")

    # -- creation -----------------------------------------------------------
    def run(self, kind: str = "manual", *, destination: Optional[str] = None) -> BackupResult:
        """Create, verify, record and prune one backup. Never raises to the caller."""
        try:
            return self._run(kind, destination)
        except Exception as exc:  # noqa: BLE001
            log.exception("backup failed: %s", exc)
            result = BackupResult(kind=kind, destination=destination or "local",
                                  status="failed", note=str(exc)[:300])
            self._record(result)
            self._alert_overdue(force=True, reason=str(exc)[:200])
            return result

    def _run(self, kind: str, destination: Optional[str]) -> BackupResult:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        workdir = Path(tempfile.mkdtemp(prefix="penny-bkp-", dir=str(self.staging_dir)))
        try:
            state_copy = workdir / "state.db"
            journal_copy = workdir / "journal.db"
            sqlite_backup(self.state, state_copy)
            sqlite_backup(self.journal, journal_copy)

            bar_files = self._copy_new_bars(workdir / "bars")
            export_files = self._export_reading_copy(workdir / "export")

            manifest = {
                "app_version": APP_VERSION,
                "created_at": time.time(),
                "created_at_et": to_et().isoformat(),
                "kind": kind,
                "schema": {"state": self.state.schema_version(),
                           "journal": self.journal.schema_version()},
                "row_counts": {"state": self.state.row_counts(),
                               "journal": self.journal.row_counts()},
                "files": [],
                "secrets_included": False,
            }

            archive = self.local_dir / f"penny-{kind}-{stamp}.tar.gz"
            self._write_archive(workdir, archive, manifest, bar_files, export_files,
                                state_copy, journal_copy)

            final = archive
            if self.passphrase:
                final = self._encrypt(archive)
                archive.unlink(missing_ok=True)

            digest = sha256_file(final)
            result = BackupResult(kind=kind, destination="local", path=str(final),
                                  size=final.stat().st_size, sha256=digest)
            ok, note = self._verify(final, manifest)
            result.verified = ok
            result.status = "ok" if ok else "unverified"
            result.note = note
            result.row_counts = manifest["row_counts"]
            self._record(result)

            self._mirror_offserver(final, kind, stamp)
            self._maybe_telegram(final, kind)

            self._prune()
            self._alert_overdue()
            if self.telegram and kind in ("daily", "manual"):
                self._safe_send(f"Backup {kind} ok: {final.name} "
                                f"({result.size / 1024:.0f} KB, verified={ok})")
            return result
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _copy_new_bars(self, dest: Path) -> list[str]:
        dest.mkdir(parents=True, exist_ok=True)
        copied = []
        bars_dir = self.cfg.bars_dir
        if not bars_dir.exists():
            return copied
        cutoff = time.time() - 48 * 3600
        for path in bars_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            target = dest / path.relative_to(bars_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(str(target.relative_to(dest.parent)))
        return copied

    def _export_reading_copy(self, dest: Path) -> list[str]:
        """Weekly CSV/JSON export readable without the application (6.18 item 9)."""
        dest.mkdir(parents=True, exist_ok=True)
        out = []
        try:
            events = [dict(r) for r in self.journal.query("SELECT * FROM events")]
            (dest / "events.json").write_text(json.dumps(events, indent=2), encoding="utf-8")
            out.append("export/events.json")
            predictions = [dict(r) for r in self.state.query(
                "SELECT * FROM predictions ORDER BY id DESC LIMIT 5000")]
            (dest / "predictions.json").write_text(json.dumps(predictions, indent=2),
                                                   encoding="utf-8")
            out.append("export/predictions.json")
            board = [dict(r) for r in self.state.query("SELECT * FROM leaderboard_history")]
            (dest / "leaderboard.json").write_text(json.dumps(board, indent=2), encoding="utf-8")
            out.append("export/leaderboard.json")
            (dest / "settings.json").write_text(
                json.dumps(self._settings_snapshot(), indent=2), encoding="utf-8")
            out.append("export/settings.json")
        except Exception as exc:  # noqa: BLE001
            log.debug("export failed: %s", exc)
        return out

    def _settings_snapshot(self) -> dict:
        """Non-secret settings only, so tuned thresholds travel with the backup."""
        from .config import SECRET_KEYS
        return {k: v for k, v in self.cfg.as_dict().items() if k not in SECRET_KEYS}

    def _write_archive(self, workdir: Path, archive: Path, manifest: dict,
                       bar_files: list[str], export_files: list[str],
                       state_copy: Path, journal_copy: Path) -> None:
        files = ["state.db", "journal.db"] + bar_files + export_files
        manifest["files"] = []
        for rel in files:
            path = workdir / rel
            if path.exists():
                manifest["files"].append({"path": rel, "sha256": sha256_file(path),
                                          "size": path.stat().st_size})
        (workdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        with tarfile.open(archive, "w:gz") as tar:
            for rel in ["manifest.json"] + files:
                path = workdir / rel
                if path.exists():
                    tar.add(path, arcname=rel)

    # -- encryption ---------------------------------------------------------
    def _encrypt(self, archive: Path) -> Path:
        """Encrypt with GnuPG symmetric when available, otherwise an internal AES scheme."""
        out = archive.with_suffix(archive.suffix + ".gpg")
        if shutil.which("gpg"):
            try:
                proc = subprocess.run(
                    ["gpg", "--batch", "--yes", "--symmetric", "--cipher-algo", "AES256",
                     "--passphrase-fd", "0", "--output", str(out), str(archive)],
                    input=self.passphrase.encode(), capture_output=True, timeout=600)
                if proc.returncode == 0 and out.exists():
                    return out
                log.warning("gpg encryption failed: %s", proc.stderr.decode()[:200])
            except Exception as exc:  # noqa: BLE001
                log.warning("gpg unavailable for encryption: %s", exc)
        return self._encrypt_internal(archive, out)

    def _decrypt(self, path: Path, dest: Path) -> bool:
        if path.suffix == ".gpg" and shutil.which("gpg"):
            try:
                proc = subprocess.run(
                    ["gpg", "--batch", "--yes", "--decrypt", "--passphrase-fd", "0",
                     "--output", str(dest), str(path)],
                    input=self.passphrase.encode(), capture_output=True, timeout=600)
                if proc.returncode == 0 and dest.exists():
                    return True
            except Exception as exc:  # noqa: BLE001
                log.warning("gpg decryption failed: %s", exc)
        if path.name.endswith(".penny.enc"):
            return self._decrypt_internal(path, dest)
        return False

    def _encrypt_internal(self, archive: Path, out: Path) -> Path:
        """AES-GCM with a PBKDF2-HMAC-SHA256 key. Used when GnuPG is unavailable."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        salt = os.urandom(16)
        nonce = os.urandom(12)
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=300_000)
        key = kdf.derive(self.passphrase.encode())
        data = archive.read_bytes()
        ciphertext = AESGCM(key).encrypt(nonce, data, None)
        target = out.with_suffix("").with_suffix(".penny.enc")
        with open(target, "wb") as fh:
            fh.write(MAGIC)
            fh.write(salt)
            fh.write(nonce)
            fh.write(ciphertext)
        return target

    def _decrypt_internal(self, path: Path, dest: Path) -> bool:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        blob = path.read_bytes()
        if not blob.startswith(MAGIC):
            return False
        body = blob[len(MAGIC):]
        salt, nonce, ciphertext = body[:16], body[16:28], body[28:]
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=300_000)
        key = kdf.derive(self.passphrase.encode())
        try:
            data = AESGCM(key).decrypt(nonce, ciphertext, None)
        except Exception as exc:  # noqa: BLE001
            log.warning("decryption failed (wrong passphrase?): %s", exc)
            return False
        dest.write_bytes(data)
        return True

    # -- verification -------------------------------------------------------
    def _verify(self, archive: Path, manifest: dict) -> tuple[bool, str]:
        """Checksum, decryption, integrity check and row-count comparison (6.18 item 7)."""
        workdir = Path(tempfile.mkdtemp(prefix="penny-verify-", dir=str(self.staging_dir)))
        try:
            plain = archive
            if archive.suffix in (".gpg", ".enc"):
                plain = workdir / "archive.tar.gz"
                if not self._decrypt(archive, plain):
                    return False, "decryption failed"
            try:
                with tarfile.open(plain, "r:gz") as tar:
                    safe_extract(tar, workdir / "extract")
            except Exception as exc:  # noqa: BLE001
                return False, f"archive unreadable: {exc}"

            extract = workdir / "extract"
            manifest_path = extract / "manifest.json"
            if not manifest_path.exists():
                return False, "manifest missing"
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in loaded.get("files", []):
                path = extract / item["path"]
                if not path.exists():
                    return False, f"missing file {item['path']}"
                if sha256_file(path) != item["sha256"]:
                    return False, f"checksum mismatch on {item['path']}"

            for name, live in (("state.db", self.state), ("journal.db", self.journal)):
                path = extract / name
                if not path.exists():
                    continue
                check_db = Database(path, "", name=name)
                try:
                    if check_db.integrity_check() != "ok":
                        return False, f"{name} integrity check failed"
                    for table, expected in (live.row_counts() or {}).items():
                        actual = check_db.row_counts().get(table, 0)
                        if table in CRITICAL_TABLES and actual != expected:
                            return False, f"{name}.{table}: {actual} != {expected}"
                finally:
                    check_db.close()
            return True, "verified"
        except Exception as exc:  # noqa: BLE001
            return False, f"verification error: {exc}"
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # -- destinations -------------------------------------------------------
    def _mirror_offserver(self, archive: Path, kind: str, stamp: str) -> None:
        remote = self.cfg.raw("BACKUP_REMOTE")
        if not remote:
            return
        try:
            if remote.startswith(("s3://", "sftp://", "gdrive:", "b2:", "r2:")) and shutil.which("rclone"):
                target = f"{remote.rstrip('/')}/{archive.name}"
                proc = subprocess.run(["rclone", "copyto", str(archive), target],
                                      capture_output=True, timeout=1800)
                status = "ok" if proc.returncode == 0 else "failed"
                self._record(BackupResult(kind=kind, destination=remote, path=target,
                                          size=archive.stat().st_size, status=status,
                                          note=proc.stderr.decode()[:200]))
                if status == "failed":
                    self._safe_send(f"Off-server backup destination failed ({remote}); "
                                    f"the local backup is intact.")
            else:
                dest = Path(remote)
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / archive.name
                shutil.copy2(archive, target)
                self._record(BackupResult(kind=kind, destination=str(dest), path=str(target),
                                          size=target.stat().st_size, status="ok"))
        except Exception as exc:  # noqa: BLE001
            log.warning("off-server mirror failed: %s", exc)
            self._record(BackupResult(kind=kind, destination=remote, status="failed",
                                      note=str(exc)[:200]))
            self._safe_send(f"Off-server backup failed ({remote}); backups queue locally.")

    def _maybe_telegram(self, archive: Path, kind: str) -> None:
        if not self.cfg.bool("BACKUP_TELEGRAM", False) or not self.telegram:
            return
        size_mb = archive.stat().st_size / (1024 * 1024)
        if size_mb > 45:
            self._safe_send(f"Learning-state backup skipped for Telegram: {size_mb:.0f} MB "
                            f"exceeds the ~50 MB limit.")
            return
        try:
            self.telegram.send_document(archive, caption=f"penny {kind} backup")
        except Exception as exc:  # noqa: BLE001
            log.debug("telegram backup upload failed: %s", exc)

    # -- retention ----------------------------------------------------------
    def _prune(self) -> None:
        """Keep the configured counts; never prune below 3 verified backups (6.18 item 6)."""
        entries = self._entries()
        verified = [e for e in entries if e["status"] == "ok" and e.get("path")]
        if len(verified) <= 3:
            return
        keep = {
            "hourly": self.cfg.int("BACKUP_KEEP_HOURLY", 48),
            "daily": self.cfg.int("BACKUP_KEEP_DAILY", 14),
            "weekly": self.cfg.int("BACKUP_KEEP_WEEKLY", 8),
            "monthly": self.cfg.int("BACKUP_KEEP_MONTHLY", 12),
        }
        for kind, limit in keep.items():
            same = [e for e in verified if e["kind"] == kind]
            if len(same) <= limit:
                continue
            same.sort(key=lambda e: e["ts"], reverse=True)
            for entry in same[limit:]:
                if len([x for x in verified if Path(x["path"]).exists()]) <= 3:
                    break
                try:
                    Path(entry["path"]).unlink(missing_ok=True)
                    log.info("pruned backup %s", entry["path"])
                except OSError:
                    pass

    def _entries(self) -> list[dict]:
        rows = self.state.query("SELECT * FROM backups ORDER BY ts DESC")
        return [{"ts": r["ts"], "kind": r["kind"], "destination": r["destination"],
                 "path": r["path"] if "path" in r.keys() else None,
                 "size": r["size"], "sha256": r["sha256"], "status": r["status"],
                 "note": r["note"]} for r in rows]

    def _record(self, result: BackupResult) -> None:
        self.state.execute(
            "INSERT INTO backups(ts, kind, destination, path, size, sha256, status, note) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), result.kind, result.destination, result.path, result.size,
             result.sha256, result.status, result.note))

    # -- overdue ------------------------------------------------------------
    def last_verified_age_hours(self) -> Optional[float]:
        rows = self.state.query(
            "SELECT MAX(ts) AS ts FROM backups WHERE status='ok' AND destination='local'")
        if not rows or rows[0]["ts"] is None:
            return None
        return (time.time() - rows[0]["ts"]) / 3600.0

    def _alert_overdue(self, force: bool = False, reason: str = "") -> None:
        max_age = self.cfg.float("BACKUP_MAX_AGE_HOURS", 26)
        age = self.last_verified_age_hours()
        if age is not None and age <= max_age and not force:
            return
        text = (f"BACKUP OVERDUE: no verified backup within {max_age:.0f} hours "
                f"(last: {age_str(age * 3600) if age else 'never'} ago).")
        if reason:
            text += f" Reason: {reason}"
        self._safe_send(text)

    # -- restore ------------------------------------------------------------
    def restore(self, source: str, *, scope: str = "full", point_in_time: Optional[float] = None,
                workdir: Optional[Path] = None) -> dict:
        """Restore a backup. Destructive; command line only (6.18)."""
        workdir = Path(workdir or tempfile.mkdtemp(prefix="penny-restore-"))
        workdir.mkdir(parents=True, exist_ok=True)
        archive = self._resolve_source(source, workdir)
        if archive is None:
            return {"ok": False, "error": f"backup not found: {source}"}

        plain = archive
        if archive.suffix in (".gpg", ".enc"):
            plain = workdir / "archive.tar.gz"
            if not self._decrypt(archive, plain):
                return {"ok": False, "error": "decryption failed (wrong passphrase?)"}

        extract = workdir / "extract"
        try:
            with tarfile.open(plain, "r:gz") as tar:
                safe_extract(tar, extract)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"archive unreadable: {exc}"}

        manifest = {}
        if (extract / "manifest.json").exists():
            manifest = json.loads((extract / "manifest.json").read_text(encoding="utf-8"))

        # Safety snapshot of whatever exists now.
        safety = self.local_dir / f"penny-prerestore-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        self._safety_snapshot(safety)

        restored = {}
        if scope in ("full", "learning", "journal") and (extract / "state.db").exists():
            if scope in ("full", "learning"):
                self.state.close()
                shutil.copy2(extract / "state.db", self.cfg.state_db)
                restored["state"] = True
        if scope in ("full", "journal") and (extract / "journal.db").exists():
            self.journal.close()
            shutil.copy2(extract / "journal.db", self.cfg.journal_db)
            restored["journal"] = True
        if scope == "full" and (extract / "bars").exists():
            dest = self.cfg.bars_dir
            for path in (extract / "bars").rglob("*"):
                if path.is_file():
                    target = dest / path.relative_to(extract / "bars")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
            restored["bars"] = True

        # Tuned settings travel with the learning state.
        if scope in ("full", "learning") and (extract / "export" / "settings.json").exists():
            try:
                settings = json.loads(
                    (extract / "export" / "settings.json").read_text(encoding="utf-8"))
                from .config import SECRET_KEYS
                for key, value in settings.items():
                    if key not in SECRET_KEYS:
                        self.cfg.set(key, value)
                self.cfg.save()
                restored["settings"] = True
            except Exception as exc:  # noqa: BLE001
                log.warning("restoring tuned settings failed: %s", exc)

        summary = self._summarise()
        shutil.rmtree(workdir, ignore_errors=True)
        return {"ok": True, "restored": restored, "safety": str(safety),
                "manifest": {"created_at_et": manifest.get("created_at_et"),
                             "app_version": manifest.get("app_version")},
                "summary": summary}

    def _resolve_source(self, source: str, workdir: Path) -> Optional[Path]:
        if source in ("latest", ""):
            entries = [e for e in self._entries() if e["status"] == "ok" and e["path"]]
            if not entries:
                return None
            return Path(entries[0]["path"])
        if source.startswith(("s3://", "sftp://", "gdrive:", "b2:", "r2:")):
            if not shutil.which("rclone"):
                return None
            target = workdir / Path(source).name
            proc = subprocess.run(["rclone", "copyto", source, str(target)],
                                  capture_output=True, timeout=1800)
            return target if proc.returncode == 0 and target.exists() else None
        path = Path(source)
        return path if path.exists() else None

    def _safety_snapshot(self, path: Path) -> None:
        try:
            with tarfile.open(path, "w:gz") as tar:
                for db_path, name in ((self.cfg.state_db, "state.db"),
                                      (self.cfg.journal_db, "journal.db")):
                    if db_path.exists():
                        tar.add(db_path, arcname=name)
        except Exception as exc:  # noqa: BLE001
            log.warning("safety snapshot failed: %s", exc)

    def _summarise(self) -> dict:
        return {
            "events": self.journal.scalar("SELECT COUNT(*) FROM events", default=0),
            "alerts": self.state.scalar("SELECT COUNT(*) FROM alerts", default=0),
            "predictions": self.state.scalar("SELECT COUNT(*) FROM predictions", default=0),
            "evaluated": self.state.scalar(
                "SELECT COUNT(*) FROM predictions WHERE status='evaluated'", default=0),
            "active_panel": self.state.kv_get("active_panel"),
        }

    # -- restore drill ------------------------------------------------------
    def drill(self) -> dict:
        """Weekly drill: restore the latest backup to a temporary dir and check it (6.18)."""
        entries = [e for e in self._entries() if e["status"] == "ok" and e["path"]]
        if not entries:
            self._safe_send("Restore drill: no verified backup available.")
            return {"ok": False, "error": "no verified backup"}
        latest = Path(entries[0]["path"])
        workdir = Path(tempfile.mkdtemp(prefix="penny-drill-", dir=str(self.staging_dir)))
        try:
            plain = latest
            if latest.suffix in (".gpg", ".enc"):
                plain = workdir / "archive.tar.gz"
                if not self._decrypt(latest, plain):
                    self._safe_send("Restore drill FAILED: decryption failed.")
                    return {"ok": False, "error": "decryption failed"}
            extract = workdir / "extract"
            with tarfile.open(plain, "r:gz") as tar:
                safe_extract(tar, extract)
            results = {}
            for name in ("state.db", "journal.db"):
                path = extract / name
                if not path.exists():
                    continue
                db = Database(path, "", name=name)
                try:
                    results[name] = {"integrity": db.integrity_check(),
                                     "rows": db.row_counts()}
                finally:
                    db.close()
            leaderboard = {}
            state_path = extract / "state.db"
            if state_path.exists():
                db = Database(state_path, "", name="state")
                try:
                    leaderboard = {"predictions": db.scalar(
                        "SELECT COUNT(*) FROM predictions", default=0)}
                finally:
                    db.close()
            ok = all(r["integrity"] == "ok" for r in results.values())
            self._safe_send(
                f"Restore drill {'PASSED' if ok else 'FAILED'}: {latest.name}; "
                f"integrity {results.get('state.db', {}).get('integrity', 'n/a')}; "
                f"predictions {leaderboard.get('predictions', 0)}")
            return {"ok": ok, "results": results, "leaderboard": leaderboard}
        except Exception as exc:  # noqa: BLE001
            self._safe_send(f"Restore drill FAILED: {exc}")
            return {"ok": False, "error": str(exc)}
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # -- helpers ------------------------------------------------------------
    def recent(self, limit: int = 10) -> list[dict]:
        return self._entries()[:limit]

    def health_text(self) -> str:
        entries = self._entries()
        age = self.last_verified_age_hours()
        lines = [f"BACKUPS ({len(entries)} recorded)",
                 f"last verified: {age_str(age * 3600) + ' ago' if age else 'never'}"]
        remote = self.cfg.raw("BACKUP_REMOTE")
        lines.append(f"off-server destination: {remote or 'none configured (not recommended)'}")
        for e in entries[:5]:
            lines.append(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(e['ts']))} "
                         f"{e['kind']} {e['destination']} {e['status']} "
                         f"{(e['size'] or 0) / 1024:.0f}KB")
        return "\n".join(lines)

    def _safe_send(self, text: str) -> None:
        if not self.telegram:
            return
        try:
            self.telegram.send(text)
        except Exception as exc:  # noqa: BLE001
            log.debug("telegram send failed: %s", exc)
