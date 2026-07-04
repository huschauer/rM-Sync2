#!/usr/bin/env python3
"""Synchronisiere und sichere Dateien eines reMarkable-Geräts."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


class SyncError(Exception):
    """Eigene Ausnahme für Synchronisationsfehler."""


def log_line(message: str, log_path: Path) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def run_command(command: List[str], *, capture_output: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, capture_output=capture_output, text=True)
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        raise SyncError(f"Befehl fehlgeschlagen: {' '.join(command)}\nSTDOUT: {stdout}\nSTDERR: {stderr}")
    return completed


def parse_metadata(raw: str, item_id: str) -> Dict[str, Any]:
    data = json.loads(raw)
    return {
        "id": item_id,
        "visibleName": data.get("visibleName", item_id),
        "type": data.get("type", "UnknownType"),
        "parent": data.get("parent"),
        "lastModified": data.get("lastModified"),
        "deleted": bool(data.get("deleted", False)),
        "raw": data,
    }


def check_remote_connection(remote_host: str, ssh_port: int, remote_user: str) -> None:
    command = ["ssh", "-p", str(ssh_port), f"{remote_user}@{remote_host}", "-q", "exit"]
    try:
        run_command(command)
    except SyncError as exc:
        raise SyncError(f"Keine Verbindung zum reMarkable unter {remote_host}: {exc}") from exc


def discover_remote_metadata(remote_dir: str, remote_host: str, ssh_port: int, remote_user: str) -> Dict[str, Dict[str, Any]]:
    command = ["ssh", "-p", str(ssh_port), f"{remote_user}@{remote_host}", f"find {remote_dir} -maxdepth 1 -type f -name '*.metadata' -print"]
    result = run_command(command)
    metadata_files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    metadata_by_id: Dict[str, Dict[str, Any]] = {}

    for metadata_file in metadata_files:
        item_id = Path(metadata_file).stem
        cat_command = ["ssh", "-p", str(ssh_port), f"{remote_user}@{remote_host}", f"cat {metadata_file}"]
        cat_result = run_command(cat_command)
        metadata_by_id[item_id] = parse_metadata(cat_result.stdout, item_id)

    return metadata_by_id


def build_output_plan(metadata_by_id: Dict[str, Dict[str, Any]], root: str) -> Dict[str, Dict[str, Any]]:
    output: Dict[str, Dict[str, Any]] = {}

    def resolve_path(item_id: str) -> Dict[str, Any]:
        if item_id in output:
            return output[item_id]

        item = metadata_by_id[item_id]
        if item["parent"] in (None, "", "trash"):
            parent_path = Path("Trash") if item["parent"] == "trash" else Path()
        elif item["parent"] in metadata_by_id:
            parent_path = resolve_path(item["parent"])["relative_path"]
        else:
            parent_path = Path()

        relative_path = parent_path / item["visibleName"]
        entry = {
            "id": item_id,
            "relative_path": relative_path,
            "display_name": item["visibleName"],
            "type": item["type"],
        }
        output[item_id] = entry
        return entry

    for item_id in metadata_by_id:
        resolve_path(item_id)

    return output


def prune_old_backups(backup_dir: Path, max_backups: int, log_path: Path) -> None:
    if max_backups < 1:
        return

    backup_dirs = sorted([path for path in backup_dir.iterdir() if path.is_dir()], key=lambda item: item.name)
    while len(backup_dirs) > max_backups:
        oldest = backup_dirs.pop(0)
        log_line(f"Entferne alten Backup-Ordner {oldest}", log_path)
        shutil.rmtree(oldest, ignore_errors=True)


def backup_remote(remote_dir: str, remote_host: str, ssh_port: int, remote_user: str, backup_dir: Path, log_path: Path, max_backups: int = 10) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target_dir = backup_dir / timestamp
    target_dir.mkdir(parents=True, exist_ok=True)

    log_line(f"Starte Backup nach {target_dir}", log_path)
    scp_command = ["scp", "-r", f"{remote_user}@{remote_host}:{remote_dir}*", str(target_dir)]
    run_command(scp_command)
    (target_dir / "files.json").write_text(json.dumps(list(discover_remote_metadata(remote_dir, remote_host, ssh_port, remote_user).values()), indent=2), encoding="utf-8")
    prune_old_backups(backup_dir, max_backups=max_backups, log_path=log_path)
    log_line("Backup abgeschlossen", log_path)
    return target_dir


def ensure_directory_structure(plan: Dict[str, Dict[str, Any]], output_root: Path) -> None:
    for item in plan.values():
        target = output_root / item["relative_path"]
        if item["type"] == "CollectionType":
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)


def prune_stale_files(output_root: Path, plan: Dict[str, Dict[str, Any]], log_path: Path) -> None:
    desired_files = {
        (output_root / item["relative_path"]).with_suffix(".pdf")
        for item in plan.values()
        if item["type"] == "DocumentType"
    }

    for path in sorted(output_root.rglob("*"), reverse=True):
        if not path.is_file():
            continue
        if path.name.endswith(".sync.json"):
            continue
        if path not in desired_files:
            log_line(f"Entferne alte Datei {path}", log_path)
            path.unlink(missing_ok=True)

    for path in sorted(output_root.rglob("*"), reverse=True):
        if path.is_dir() and path != output_root and not any(path.iterdir()):
            path.rmdir()


def should_download_document(output_path: Path, metadata_path: Path, remote_mtime: str | None) -> bool:
    if not output_path.exists():
        return True

    if not metadata_path.exists():
        return True

    try:
        remote_value = int(str(remote_mtime or "0"))
    except ValueError:
        return True

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        local_value = int(str(metadata.get("lastModified", "0") or "0"))
    except (json.JSONDecodeError, ValueError):
        local_value = 0

    if remote_value <= 0:
        return True
    if local_value <= 0:
        return True

    return remote_value > local_value


def print_progress(current: int, total: int, label: str) -> None:
    if total <= 0:
        return
    width = 30
    filled = int(width * current / total)
    bar = "#" * filled + "-" * (width - filled)
    print(f"[{current}/{total}] [{bar}] {label}", flush=True)


def upload_files(upload_dir: Path, recent_uploads_dir: Path, remote_host: str, log_path: Path) -> int:
    if not upload_dir.exists():
        return 0

    files = sorted([path for path in upload_dir.iterdir() if path.is_file()])
    if not files:
        return 0

    recent_uploads_dir.mkdir(parents=True, exist_ok=True)
    for index, file_path in enumerate(files, start=1):
        print_progress(index, len(files), f"Lade hoch: {file_path.name}")
        try:
            run_command(["curl", "-s", "-X", "POST", "-F", f"file=@{file_path}", f"http://{remote_host}/upload"])
            destination = recent_uploads_dir / file_path.name
            if destination.exists():
                destination.unlink()
            shutil.move(str(file_path), str(destination))
            log_line(f"Upload erfolgreich: {file_path.name}", log_path)
        except SyncError as exc:
            log_line(f"Upload fehlgeschlagen für {file_path.name}: {exc}", log_path)

    return len(files)


def download_documents(plan: Dict[str, Dict[str, Any]], output_root: Path, remote_host: str, log_path: Path) -> int:
    document_ids = [item_id for item_id, item in plan.items() if item["type"] == "DocumentType"]
    downloaded = 0
    for index, item_id in enumerate(document_ids, start=1):
        item = plan[item_id]
        output_path = (output_root / item["relative_path"]).with_suffix(".pdf")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path = output_path.with_suffix(".sync.json")

        remote_mtime = item.get("lastModified")
        if not should_download_document(output_path, metadata_path, remote_mtime):
            print_progress(index, len(document_ids), f"Übersprungen: {output_path.name}")
            continue

        url = f"http://{remote_host}/download/{item_id}/placeholder"
        try:
            print_progress(index, len(document_ids), f"Lade herunter: {output_path.name}")
            log_line(f"Lade {item_id} -> {output_path}", log_path)
            run_command(["curl", "-s", "-o", str(output_path), "-J", url])
            metadata_path.write_text(json.dumps({"lastModified": remote_mtime}, indent=2), encoding="utf-8")
            downloaded += 1
        except SyncError as exc:
            log_line(f"Fehler beim Download von {item_id}: {exc}", log_path)
    return downloaded


def sync_remarkable(remote_dir: str, remote_host: str, ssh_port: int, remote_user: str, main_dir: Path, backup_dir: Path, output_dir: Path, log_path: Path, max_backups: int = 10) -> int:
    if not main_dir.exists():
        main_dir.mkdir(parents=True, exist_ok=True)

    upload_dir = main_dir / "Upload"
    recent_uploads_dir = main_dir / "Recent Uploads"
    if recent_uploads_dir.exists():
        shutil.rmtree(recent_uploads_dir, ignore_errors=True)
    recent_uploads_dir.mkdir(parents=True, exist_ok=True)

    check_remote_connection(remote_host, ssh_port, remote_user)
    backup_remote(remote_dir, remote_host, ssh_port, remote_user, backup_dir, log_path, max_backups=max_backups)
    metadata_by_id = discover_remote_metadata(remote_dir, remote_host, ssh_port, remote_user)

    plan = build_output_plan(metadata_by_id, str(output_dir))
    ensure_directory_structure(plan, output_dir)
    prune_stale_files(output_dir, plan, log_path)

    downloaded = download_documents(plan, output_dir, remote_host, log_path)
    uploaded = upload_files(upload_dir, recent_uploads_dir, remote_host, log_path)
    log_line(f"Synchronisation abgeschlossen. Heruntergeladen: {downloaded}, Hochgeladen: {uploaded}", log_path)
    return downloaded


def main() -> int:
    parser = argparse.ArgumentParser(description="Sichere und synchronisiere Dateien von einem reMarkable")
    parser.add_argument("--remote-dir", default="/home/root/.local/share/remarkable/xochitl/")
    parser.add_argument("--remote-host", default="10.11.99.1")
    parser.add_argument("--remote-user", default="root")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--main-dir", default=str(Path.home() / "Nextcloud" / "Documents" / "reMarkable"))
    parser.add_argument("--backup-dir", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--max-backups", type=int, default=10)
    args = parser.parse_args()

    main_dir = Path(args.main_dir).expanduser()
    backup_dir = Path(args.backup_dir).expanduser() if args.backup_dir else main_dir / "Backup"
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else main_dir / "Files"
    log_path = Path(args.log_file).expanduser() if args.log_file else main_dir / "sync.log"

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)

    try:
        start = datetime.now()
        log_line(f"Start: {start.strftime('%Y-%m-%d %H:%M:%S')}", log_path)
        downloaded = sync_remarkable(
            remote_dir=args.remote_dir,
            remote_host=args.remote_host,
            ssh_port=args.ssh_port,
            remote_user=args.remote_user,
            main_dir=main_dir,
            backup_dir=backup_dir,
            output_dir=output_dir,
            log_path=log_path,
            max_backups=args.max_backups,
        )
        end = datetime.now()
        duration = end - start
        log_line(f"Erfolg. Heruntergeladen: {downloaded} Dateien. Dauer: {duration}", log_path)
        print(f"Erfolg. Heruntergeladen: {downloaded} Dateien.")
        return 0
    except Exception as exc:  # noqa: BLE001
        log_line(f"Fehler: {exc}", log_path)
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    main()
