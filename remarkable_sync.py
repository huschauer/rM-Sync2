#!/usr/bin/env python3
"""Synchronisiere und sichere Dateien eines reMarkable-Geräts."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_FILENAME = ".remarkable_sync_state.json"


def load_json_file(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise SyncError(f"Ungültige JSON-Datei {path}: {exc}") from exc


def load_config(config_path: Optional[Path]) -> Dict[str, Any]:
    if config_path is None:
        return {}
    config_path = config_path.expanduser()
    if not config_path.exists():
        return {}
    return load_json_file(config_path)


def resolve_path(value: Any, default: Path) -> Path:
    if value is None:
        return default
    return Path(str(value)).expanduser()


def load_state(output_root: Path, state_filename: str = STATE_FILENAME) -> Dict[str, Any]:
    state_file = output_root / state_filename
    state = load_json_file(state_file)
    if not isinstance(state, dict):
        return {"documents": {}}
    if "documents" not in state or not isinstance(state["documents"], dict):
        state["documents"] = {}
    return state


def save_state(output_root: Path, state: Dict[str, Any], state_filename: str = STATE_FILENAME) -> None:
    state_file = output_root / state_filename
    output_root.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def clean_state(state: Dict[str, Any], plan: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    state_documents = state.get("documents", {})
    keep_ids = set(plan.keys())
    state["documents"] = {
        item_id: data
        for item_id, data in state_documents.items()
        if item_id in keep_ids
    }
    return state


def remove_old_per_document_state_files(output_root: Path, log_path: Path) -> None:
    for path in sorted(output_root.rglob("*.sync.json")):
        log_line(f"Entferne altes per-Datei-Statefile {path}", log_path)
        path.unlink(missing_ok=True)


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


def prune_stale_files(output_root: Path, plan: Dict[str, Dict[str, Any]], log_path: Path, state_filename: str = STATE_FILENAME) -> None:
    desired_files = {
        (output_root / item["relative_path"]).with_suffix(".pdf")
        for item in plan.values()
        if item["type"] == "DocumentType"
    }

    for path in sorted(output_root.rglob("*"), reverse=True):
        if not path.is_file():
            continue
        if path.name == state_filename:
            continue
        if path.name.endswith(".sync.json"):
            continue
        if path not in desired_files:
            log_line(f"Entferne alte Datei {path}", log_path)
            path.unlink(missing_ok=True)

    for path in sorted(output_root.rglob("*"), reverse=True):
        if path.is_dir() and path != output_root and not any(path.iterdir()):
            path.rmdir()


def should_download_document(output_path: Path, state: Dict[str, Any], item_id: str, remote_mtime: Optional[str]) -> bool:
    if not output_path.exists():
        return True

    state_documents = state.get("documents", {})
    document_state = state_documents.get(item_id, {})
    existing_remote_value = document_state.get("lastModified")

    if remote_mtime is None:
        return existing_remote_value is None

    try:
        remote_value = int(str(remote_mtime))
    except ValueError:
        return True

    if existing_remote_value is None or existing_remote_value == "":
        return True

    try:
        local_value = int(str(existing_remote_value))
    except ValueError:
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


def download_documents(plan: Dict[str, Dict[str, Any]], output_root: Path, remote_host: str, log_path: Path, state: Dict[str, Any], state_filename: str = STATE_FILENAME) -> int:
    document_ids = [item_id for item_id, item in plan.items() if item["type"] == "DocumentType"]
    downloaded = 0
    for index, item_id in enumerate(document_ids, start=1):
        item = plan[item_id]
        output_path = (output_root / item["relative_path"]).with_suffix(".pdf")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        remote_mtime = item.get("lastModified")
        if not should_download_document(output_path, state, item_id, remote_mtime):
            print_progress(index, len(document_ids), f"Übersprungen: {output_path.name}")
            continue

        url = f"http://{remote_host}/download/{item_id}/placeholder"
        try:
            print_progress(index, len(document_ids), f"Lade herunter: {output_path.name}")
            log_line(f"Lade {item_id} -> {output_path}", log_path)
            run_command(["curl", "-s", "-o", str(output_path), "-J", url])
            state.setdefault("documents", {})[item_id] = {
                "lastModified": str(remote_mtime) if remote_mtime is not None else "",
                "relative_path": str(item["relative_path"]),
            }
            downloaded += 1
        except SyncError as exc:
            log_line(f"Fehler beim Download von {item_id}: {exc}", log_path)

    save_state(output_root, state, state_filename=state_filename)
    return downloaded


def sync_remarkable(remote_dir: str, remote_host: str, ssh_port: int, remote_user: str, main_dir: Path, backup_dir: Path, output_dir: Path, log_path: Path, upload_dir: Path, recent_uploads_dir: Path, max_backups: int = 10, state_filename: str = STATE_FILENAME) -> int:
    if not main_dir.exists():
        main_dir.mkdir(parents=True, exist_ok=True)

    if recent_uploads_dir.exists():
        shutil.rmtree(recent_uploads_dir, ignore_errors=True)
    recent_uploads_dir.mkdir(parents=True, exist_ok=True)

    check_remote_connection(remote_host, ssh_port, remote_user)
    backup_remote(remote_dir, remote_host, ssh_port, remote_user, backup_dir, log_path, max_backups=max_backups)
    metadata_by_id = discover_remote_metadata(remote_dir, remote_host, ssh_port, remote_user)

    plan = build_output_plan(metadata_by_id, str(output_dir))
    ensure_directory_structure(plan, output_dir)
    remove_old_per_document_state_files(output_dir, log_path)
    prune_stale_files(output_dir, plan, log_path, state_filename=state_filename)

    state = load_state(output_dir, state_filename=state_filename)
    state = clean_state(state, plan)
    downloaded = download_documents(plan, output_dir, remote_host, log_path, state, state_filename=state_filename)
    uploaded = upload_files(upload_dir, recent_uploads_dir, remote_host, log_path)
    log_line(f"Synchronisation abgeschlossen. Heruntergeladen: {downloaded}, Hochgeladen: {uploaded}", log_path)
    return downloaded


def main() -> int:
    parser = argparse.ArgumentParser(description="Sichere und synchronisiere Dateien von einem reMarkable")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--remote-dir", default=None)
    parser.add_argument("--remote-host", default=None)
    parser.add_argument("--remote-user", default=None)
    parser.add_argument("--ssh-port", type=int, default=None)
    parser.add_argument("--main-dir", default=None)
    parser.add_argument("--backup-dir", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--log-file", default=None)
    parser.add_argument("--max-backups", type=int, default=None)
    parser.add_argument("--upload-dir", default=None)
    parser.add_argument("--recent-uploads-dir", default=None)
    parser.add_argument("--state-file", default=None)
    args = parser.parse_args()

    config = load_config(Path(args.config)) if args.config else {}
    remote_dir = str(args.remote_dir or config.get("remote_dir") or "/home/root/.local/share/remarkable/xochitl/")
    remote_host = str(args.remote_host or config.get("remote_host") or "10.11.99.1")
    remote_user = str(args.remote_user or config.get("remote_user") or "root")
    ssh_port = int(args.ssh_port or config.get("ssh_port") or 22)

    main_dir = resolve_path(args.main_dir or config.get("main_dir"), Path.home() / "Nextcloud" / "Documents" / "reMarkable")
    backup_dir = resolve_path(args.backup_dir or config.get("backup_dir"), main_dir / "Backup")
    output_dir = resolve_path(args.output_dir or config.get("output_dir"), main_dir / "Files")
    upload_dir = resolve_path(args.upload_dir or config.get("upload_dir"), main_dir / "Upload")
    recent_uploads_dir = resolve_path(args.recent_uploads_dir or config.get("recent_uploads_dir"), main_dir / "Recent Uploads")
    log_path = resolve_path(args.log_file or config.get("log_file"), main_dir / "sync.log")
    max_backups = int(args.max_backups or config.get("max_backups") or 10)
    state_filename = str(args.state_file or config.get("state_file") or STATE_FILENAME)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)

    try:
        start = datetime.now()
        log_line(f"Start: {start.strftime('%Y-%m-%d %H:%M:%S')}", log_path)
        downloaded = sync_remarkable(
            remote_dir=remote_dir,
            remote_host=remote_host,
            ssh_port=ssh_port,
            remote_user=remote_user,
            main_dir=main_dir,
            backup_dir=backup_dir,
            output_dir=output_dir,
            log_path=log_path,
            upload_dir=upload_dir,
            recent_uploads_dir=recent_uploads_dir,
            max_backups=max_backups,
            state_filename=state_filename,
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
