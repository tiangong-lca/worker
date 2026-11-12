"""Portable producer regression tests; native relocation needs a real macOS case."""
import gzip
import hashlib
import io
import os
import platform
import shutil
import subprocess
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from package_model_runtime import archive, bundle_native, dependencies, publish, resolve_library, rpaths, system_library


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

    def test_native_names_and_ordered_runpaths(self):
        self.assertTrue(system_library('/usr/lib/libSystem.B.dylib'))
        self.assertFalse(system_library('/opt/homebrew/lib/libumfpack.dylib'))
        with patch('package_model_runtime.command', side_effect=['file:\n@rpath/own-id.dylib\n', 'file:\n\t@rpath/own-id.dylib (compatibility version 1.0.0)\n\t@rpath/libtest.dylib (compatibility version 1.0.0)\n']):
            self.assertEqual(dependencies(Path('file')), ['@rpath/libtest.dylib'])
        with patch('package_model_runtime.command', return_value=' cmd LC_RPATH\n cmdsize 48\n path @loader_path/lib (offset 12)\n'):
            self.assertEqual(rpaths(Path('file')), ['@loader_path/lib'])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binary = root / 'engine'
            library = root / 'libtest.dylib'
            library.write_text('not executed')
            with patch('package_model_runtime.rpaths', return_value=['@loader_path']):
                self.assertEqual(resolve_library('@rpath/libtest.dylib', binary, binary), library)
                self.assertEqual(resolve_library('@loader_path/libtest.dylib', binary, binary), library)
                with self.assertRaises(ValueError):
                    resolve_library('@rpath/missing', binary, binary)
                with self.assertRaises(ValueError):
                    resolve_library(str(root), binary, binary)
            (root / 'alt').mkdir()
            (root / 'alt/libtest.dylib').write_text('other')
            with patch('package_model_runtime.rpaths', return_value=['@loader_path/alt', '@loader_path']):
                self.assertEqual(resolve_library('@rpath/libtest.dylib', binary, binary), root / 'alt/libtest.dylib')
            with patch('package_model_runtime.rpaths', return_value=[]):
                with self.assertRaises(ValueError):
                    resolve_library('@rpath/libtest.dylib', binary, binary)

    def test_relative_paths_keep_the_declaring_image_and_ancestor_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            engine = root / 'bin/engine'
            parent = root / 'lib/libparent.dylib'
            child = root / 'lib/sub/libchild.dylib'
            for image in [engine, parent, child]:
                image.parent.mkdir(parents=True, exist_ok=True)
            dependency = root / 'shared/libdependency.dylib'
            dependency.parent.mkdir()
            dependency.write_text('fixture')
            paths = {engine: ['@loader_path/../shared'], parent: ['@loader_path/../shared']}
            for owner in [engine, parent]:
                with patch('package_model_runtime.rpaths', side_effect=lambda image: paths[owner] if image == owner else []):
                    self.assertEqual(resolve_library('@rpath/libdependency.dylib', child, engine, (parent, engine)), dependency)
            self.assertEqual(resolve_library('@executable_path/../shared/libdependency.dylib', child, engine), dependency)

    def test_directory_symlinks_fail_instead_of_silently_omitting_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'payload'
            root.mkdir()
            (root / 'real').mkdir()
            (root / 'real/value').write_text('data')
            (root / 'linked').symlink_to('real', target_is_directory=True)
            with self.assertRaises(ValueError):
                archive(root, Path(temporary) / 'package.tar.gz')

    def test_publication_claims_new_target_and_cleans_only_owned_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, output = root / 'staged', root / 'published'
            package.mkdir()
            for name in ['runtime.tar.gz', 'manifest.json']:
                (package / name).write_text(name)
            output.mkdir()  # Another caller can create it during the readiness check.
            identity = output.stat().st_ino
            with self.assertRaises(FileExistsError):
                publish(package, output)
            self.assertEqual(output.stat().st_ino, identity)
            output.rmdir()
            rename = os.rename
            calls = []
            def interrupted(source, target):
                calls.append(source.name)
                if source.name == 'manifest.json':
                    raise OSError('interrupted')
                return rename(source, target)
            with patch('package_model_runtime.os.rename', side_effect=interrupted):
                with self.assertRaises(OSError):
                    publish(package, output)
            self.assertEqual(calls, ['runtime.tar.gz', 'manifest.json'])
            self.assertFalse(output.exists())
            (package / 'runtime.tar.gz').write_text('restaged')
            publish(package, output)
            self.assertEqual((output / 'manifest.json').read_text(), 'manifest.json')

    @unittest.skipUnless(platform.system() == 'Darwin' and platform.machine() == 'arm64', 'native macOS ARM64 relocation case')
    def test_native_transitive_relative_libraries_survive_source_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source, target = root / 'source', root / 'installed'
            for name in ['bin', 'lib/sub', 'shared']:
                (source / name).mkdir(parents=True, exist_ok=True)
            def build(name, code, *flags):
                c = source / (name + '.c')
                c.write_text(code)
                subprocess.run(['/usr/bin/clang', str(c), '-Wl,-headerpad_max_install_names', *flags, '-o', str(source / name)], check=True, capture_output=True, timeout=60)
            build('shared/libgrand.dylib', 'int grand(void) { return 7; }', '-dynamiclib', '-Wl,-install_name,@rpath/libgrand.dylib')
            build('lib/sub/libchild.dylib', 'extern int grand(void); int child(void) { return grand()+1; }', '-dynamiclib', '-Wl,-install_name,@rpath/libchild.dylib', '-L'+str(source / 'shared'), '-lgrand')
            build('lib/libparent.dylib', 'extern int child(void); int parent(void) { return child()+1; }', '-dynamiclib', '-Wl,-install_name,@rpath/libparent.dylib', '-Wl,-rpath,@loader_path/sub', '-L'+str(source / 'lib/sub'), '-lchild')
            build('bin/engine', 'extern int parent(void); int main(void) { return parent() == 9 ? 0 : 1; }', '-Wl,-rpath,@loader_path/../lib', '-Wl,-rpath,@loader_path/../shared', '-L'+str(source / 'lib'), '-lparent')
            executable = bundle_native(source / 'bin/engine', target)
            shutil.rmtree(source)
            subprocess.run([str(executable)], env={'PATH': '/usr/bin:/bin'}, check=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
