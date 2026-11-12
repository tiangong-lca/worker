"""Portable producer regression tests; native relocation needs a real macOS case."""
import gzip
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from package_model_runtime import archive, dependencies, resolve_library, rpaths, system_library


class PackageTests(unittest.TestCase):
    def test_archive_is_exact_regular_inventory_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            (root / "bin").mkdir(parents=True)
            (root / "bin/model_calculate").write_bytes(bytes(range(256)) * 3)
            (root / "cache").mkdir()
            (root / "cache/data.json").write_text('{"test": true}')
            output = Path(temporary) / "package.tar.gz"
            files = archive(root, output)
            data = gzip.decompress(output.read_bytes())
            self.assertEqual(len(data), 1024 + sum(512 + ((f["bytes"] + 511) // 512) * 512 for f in files))
            with tarfile.open(fileobj=io.BytesIO(data)) as reader:
                self.assertEqual(reader.getnames(), [f["path"] for f in files])
                for entry, metadata in zip(reader.getmembers(), files, strict=True):
                    self.assertTrue(entry.isfile())
                    self.assertEqual(entry.mode, metadata["mode"])
                    self.assertEqual(hashlib.sha256(reader.extractfile(entry).read()).hexdigest(), metadata["sha256"])
            with self.assertRaises(FileExistsError):
                archive(root, output)

    def test_symlinks_are_not_archived(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            root.mkdir()
            (root / "value").write_text("x")
            (root / "link").symlink_to("value")
            with self.assertRaises(ValueError):
                archive(root, Path(temporary) / "package.tar.gz")

    def test_native_names_paths_and_ambiguous_dependency(self):
        self.assertTrue(system_library('/usr/lib/libSystem.B.dylib'))
        self.assertFalse(system_library('/opt/homebrew/lib/libumfpack.dylib'))
        with patch('package_model_runtime.command', return_value='file:\n\t@rpath/libtest.dylib (compatibility version 1.0.0)\n'):
            self.assertEqual(dependencies(Path('file')), ['@rpath/libtest.dylib'])
        with patch('package_model_runtime.command', return_value=' cmd LC_RPATH\n cmdsize 48\n path @loader_path/lib (offset 12)\n'):
            self.assertEqual(rpaths(Path('file')), ['@loader_path/lib'])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binary = root / 'engine'
            library = root / 'libtest.dylib'
            library.write_text('not executed')
            with patch('package_model_runtime.rpaths', return_value=[]):
                self.assertEqual(resolve_library('@rpath/libtest.dylib', binary, binary), library)
                self.assertEqual(resolve_library('@loader_path/libtest.dylib', binary, binary), library)
                with self.assertRaises(ValueError):
                    resolve_library('@rpath/missing', binary, binary)
                with self.assertRaises(ValueError):
                    resolve_library(str(root), binary, binary)
            (root / 'alt').mkdir()
            (root / 'alt/libtest.dylib').write_text('other')
            with patch('package_model_runtime.rpaths', return_value=[str(root / 'alt')]):
                with self.assertRaises(ValueError):
                    resolve_library('@rpath/libtest.dylib', binary, binary)


if __name__ == '__main__':
    unittest.main()
