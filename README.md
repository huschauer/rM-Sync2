# reMarkable-Sync

Dieses Projekt enthält ein Python-Programm zum Sichern und synchronisieren eines reMarkable-Geräts.

## Funktionen
- Backup der kompletten Datei-Struktur auf dem reMarkable
- Erstellung einer lokalen, virtuellen Ordnerstruktur unter Files
- Download von Dokumenten als PDF
- einfache Fortschrittsausgabe und Logging

## Verwendung

```bash
python remarkable_sync.py \
  --main-dir "$HOME/Nextcloud/Documents/reMarkable" \
  --remote-host 10.11.99.1 \
  --remote-user root \
  --ssh-port 22
```

Die Ausgabe landet standardmäßig in:
- Backup: $HOME/Nextcloud/Documents/reMarkable/Backup
- PDFs: $HOME/Nextcloud/Documents/reMarkable/Files
- Log: $HOME/Nextcloud/Documents/reMarkable/sync.log
