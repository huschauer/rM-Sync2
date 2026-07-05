# reMarkable-Sync

Dieses Projekt enthält ein Python-Programm zum Sichern und Synchronisieren eines reMarkable-Geräts.

## Funktionen
- Backup der kompletten Datei-Struktur auf dem reMarkable
- Erstellung einer lokalen, virtuellen Ordnerstruktur unter `Files`
- Download von Dokumenten als PDF
- Upload von Dateien vom lokalen `Upload`-Ordner auf das reMarkable
- Rotierende Backups mit max. Anzahl von Backup-Ordnern
- Der `lastModified'-Wert des reMarkable wird zur schnellen inkrementellen Synchronisation verwendet
- Fortschrittsanzeige im Terminal und ausführliches Logging

## Verwendung

```bash
python remarkable_sync.py \
  --main-dir "$HOME/Nextcloud/Documents/reMarkable" \
  --remote-host 10.11.99.1 \
  --remote-user root \
  --ssh-port 22
```

### Standardpfade
- Backup: `$HOME/Nextcloud/Documents/reMarkable/Backup`
- PDFs: `$HOME/Nextcloud/Documents/reMarkable/Files`
- Log: `$HOME/Nextcloud/Documents/reMarkable/sync.log`
- Upload-Ordner: `$HOME/Nextcloud/Documents/reMarkable/Upload`
- Recent Uploads: `$HOME/Nextcloud/Documents/reMarkable/Recent Uploads`
- State-Datei: `$HOME/Nextcloud/Documents/reMarkable/Files/.remarkable_sync_state.json`

## Konfigurationsdatei
Das Skript unterstützt eine JSON-Konfigurationsdatei zur zentralen Einstellung aller Pfade und Parameter.

Beispiel `config.json`:

```json
{
  "remote_dir": "/home/root/.local/share/remarkable/xochitl/",
  "remote_host": "10.11.99.1",
  "remote_user": "root",
  "ssh_port": 22,
  "main_dir": "/home/horst/Nextcloud/Documents/reMarkable",
  "backup_dir": "/home/horst/Nextcloud/Documents/reMarkable/Backup",
  "output_dir": "/home/horst/Nextcloud/Documents/reMarkable/Files",
  "log_file": "/home/horst/Nextcloud/Documents/reMarkable/sync.log",
  "max_backups": 10,
  "upload_dir": "/home/horst/Nextcloud/Documents/reMarkable/Upload",
  "recent_uploads_dir": "/home/horst/Nextcloud/Documents/reMarkable/Recent Uploads",
  "state_file": ".remarkable_sync_state.json"
}
```

### Konfigurationsdatei nutzen

```bash
python remarkable_sync.py --config config.json
```

Optionale CLI-Parameter überschreiben die Werte aus der Konfigurationsdatei.

## Hinweise
- Beim Start der Synchronisation wird der `Recent Uploads`-Ordner neu erstellt.
- Erfolgreich hochgeladene Dateien werden aus `Upload` nach `Recent Uploads` verschoben.
