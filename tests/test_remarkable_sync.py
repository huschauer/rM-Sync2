import json
import tempfile
import unittest
from pathlib import Path

from remarkable_sync import build_output_plan, parse_metadata, prune_old_backups, prune_stale_files, should_download_document


class RemarkableSyncTests(unittest.TestCase):
    def test_parse_metadata_file(self):
        raw = '''{
            "createdTime": "0",
            "lastModified": "1721920602260",
            "parent": "trash",
            "type": "DocumentType",
            "visibleName": "Beratermeeting 240405"
        }'''

        parsed = parse_metadata(raw, "abc123")

        self.assertEqual(parsed["id"], "abc123")
        self.assertEqual(parsed["visibleName"], "Beratermeeting 240405")
        self.assertEqual(parsed["type"], "DocumentType")
        self.assertEqual(parsed["parent"], "trash")
        self.assertEqual(parsed["lastModified"], "1721920602260")

    def test_build_output_plan_places_documents_in_virtual_folders(self):
        metadata = {
            "root-folder": {
                "id": "root-folder",
                "visibleName": "Archiv",
                "type": "CollectionType",
                "parent": None,
                "lastModified": "1",
            },
            "doc-1": {
                "id": "doc-1",
                "visibleName": "Meeting",
                "type": "DocumentType",
                "parent": "root-folder",
                "lastModified": "2",
            },
        }

        plan = build_output_plan(metadata, root="Files")

        self.assertEqual(plan["root-folder"]["relative_path"], Path("Archiv"))
        self.assertEqual(plan["doc-1"]["relative_path"], Path("Archiv/Meeting"))
        self.assertEqual(plan["doc-1"]["display_name"], "Meeting")

    def test_prune_old_backups_keeps_latest_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backup_dir = Path(tmpdir)
            for name in ["20240101-010101", "20240102-010101", "20240103-010101"]:
                (backup_dir / name).mkdir(parents=True, exist_ok=True)

            prune_old_backups(backup_dir, max_backups=2, log_path=backup_dir / "log.txt")

            self.assertEqual(sorted(p.name for p in backup_dir.iterdir() if p.is_dir()), ["20240102-010101", "20240103-010101"])

    def test_should_download_document_uses_saved_timestamp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            output_path = output_dir / "doc.pdf"
            output_path.write_bytes(b"content")
            metadata_path = output_dir / "doc.sync.json"
            metadata_path.write_text(json.dumps({"lastModified": "1700000000000"}), encoding="utf-8")

            self.assertFalse(should_download_document(output_path, metadata_path, "1700000000000"))
            self.assertTrue(should_download_document(output_path, metadata_path, "1800000000000"))

    def test_prune_stale_files_keeps_sync_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            (output_dir / "doc.pdf").write_bytes(b"content")
            (output_dir / "doc.sync.json").write_text("{}", encoding="utf-8")
            (output_dir / "stale.txt").write_text("remove", encoding="utf-8")

            plan = {
                "doc-1": {
                    "id": "doc-1",
                    "relative_path": Path("doc"),
                    "type": "DocumentType",
                }
            }

            prune_stale_files(output_dir, plan, output_dir / "log.txt")

            self.assertTrue((output_dir / "doc.sync.json").exists())
            self.assertFalse((output_dir / "stale.txt").exists())


if __name__ == "__main__":
    unittest.main()
