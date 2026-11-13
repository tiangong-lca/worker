"""Portable producer regression tests; native relocation needs a real macOS case."""
import gzip
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from qualify_model_runtime import qualify, CO2, METHOD
from model_native_platforms import native_host, linux_dependencies, bundle_linux, windows_dependencies, bundle_windows, runtime_environment
from package_model_runtime import check_engine, command, archive, bundle_native, dependencies, publish, resolve_library, rpaths, system_library


class PackageTests(unittest.TestCase):
    def test_reviewed_manifest_bytes_survive_windows_checkout(self):
        root = Path(__file__).resolve().parents[1]
        path = 'crates/solver-worker/src/lcia_static_cache_bundle_manifest.json'
        canonical = subprocess.check_output(['git', 'show', 'HEAD:' + path], cwd=root)
        windows = subprocess.check_output(['git', '-c', 'core.autocrlf=true', 'cat-file', '--filters', 'HEAD:' + path], cwd=root)
        self.assertEqual(windows, canonical)

    def test_engine_self_check_preserves_native_failure_diagnostic(self):
        result = subprocess.CompletedProcess([], 101, '', 'reviewed manifest raw SHA-256 drift')
        with patch('package_model_runtime.subprocess.run', return_value=result):
            with self.assertRaisesRegex(ValueError, '101.*reviewed manifest raw SHA-256 drift'):
                check_engine(Path('engine'), Path('cache'), Path('.'))


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


class PlatformTests(unittest.TestCase):
    def test_supported_hosts_are_os_architecture_pairs_and_linux_requires_glibc(self):
        with patch('model_native_platforms.platform.libc_ver', return_value=('glibc', '2.36')):
            for system, machine, expected in [('Darwin', 'arm64', 'darwin-arm64'), ('Linux', 'aarch64', 'linux-arm64'), ('Linux', 'x86_64', 'linux-x64'), ('Windows', 'AMD64', 'win32-x64')]:
                with patch('model_native_platforms.platform.system', return_value=system), patch('model_native_platforms.platform.machine', return_value=machine):
                    self.assertEqual(native_host(), expected)
            for system, machine in [('Darwin', 'x86_64'), ('Windows', 'arm64'), ('Linux', 'i686')]:
                with patch('model_native_platforms.platform.system', return_value=system), patch('model_native_platforms.platform.machine', return_value=machine):
                    with self.assertRaises(ValueError):
                        native_host()
        with patch('model_native_platforms.platform.system', return_value='Linux'), patch('model_native_platforms.platform.machine', return_value='aarch64'), patch('model_native_platforms.platform.libc_ver', return_value=('musl', '1.2')):
            with self.assertRaisesRegex(ValueError, 'glibc'):
                native_host()

    def test_loader_output_requires_complete_dependencies(self):
        output = 'linux-vdso.so.1 (0xffff)\nlibsolver.so => /opt/my libs/libsolver.so (0x123)\n/lib/ld-linux-aarch64.so.1 (0x456)\n/opt/libdirect.so (0x789)'
        self.assertEqual(linux_dependencies(output), {'libsolver.so': Path('/opt/my libs/libsolver.so'), '/opt/libdirect.so': Path('/opt/libdirect.so')})
        for invalid in ['libmissing.so => not found', 'unrecognized format', 'libbad.so => relative/file (0x123)', 'a => /one (0x1)\na => /two (0x2)']:
            with self.assertRaises(ValueError):
                linux_dependencies(invalid)
        self.assertEqual(windows_dependencies('  KERNEL32.dll\n  umfpack.dll\n  1000 .data\n  umfpack.dll\n'), ['KERNEL32.dll', 'umfpack.dll'])

    def test_linux_keeps_os_loader_and_rewrites_each_numerical_library_runpath(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine, library = root / 'engine', root / 'libnumeric.so'
            engine.write_text('engine')
            library.write_text('library')
            package = root / 'package'
            changes = []
            def run(tool, *args):
                if tool == 'ldd':
                    source = library if Path(args[0]) == engine else package / 'bin/lib/libnumeric.so'
                    return f'libnumeric.so => {source} (0x123)\n/lib/ld-linux-aarch64.so.1 (0x456)'
                if tool == 'readelf':
                    return 'Class: ELF64\nMachine: AArch64\n'
                if args[0] == '--print-needed':
                    return 'libnumeric.so' if Path(args[1]).name == 'model_calculate' else 'ld-linux-aarch64.so.1'
                changes.append(args)
                return ''
            with patch('model_native_platforms.linux_dependencies', side_effect=[{'libnumeric.so': library}, {'libnumeric.so': package / 'bin/lib/libnumeric.so'}]):
                executable = bundle_linux(engine, package, 'linux-arm64', run)
            self.assertEqual(executable, package / 'bin/model_calculate')
            self.assertEqual(sorted(p.name for p in (package / 'bin/lib').iterdir()), ['libnumeric.so'])
            self.assertEqual([v[1] for v in changes], ['$ORIGIN/lib', '$ORIGIN'])

    def test_windows_copies_transitive_dlls_and_rejects_missing_ambiguous_or_wrong_arch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            for name in ['engine.exe', 'solver.dll', 'blas.dll', 'unused.dll']:
                (source / name).write_text(name)
            graph = {'engine.exe': ['KERNEL32.dll', 'solver.dll'], 'solver.dll': ['blas.dll', 'api-ms-win-core-synch-l1-2-0.dll'], 'blas.dll': []}
            def run(tool, option, binary):
                return '8664 machine (x64)' if option == '/headers' else '\n'.join(graph[Path(binary).name])
            bundle_windows(source / 'engine.exe', root / 'package', [], run)
            self.assertEqual(sorted(p.name for p in (root / 'package/bin').iterdir()), ['blas.dll', 'model_calculate.exe', 'solver.dll'])
            with self.assertRaisesRegex(ValueError, 'x64 PE'):
                bundle_windows(source / 'engine.exe', root / 'bad-arch', [], lambda *args: 'AA64 machine (ARM64)')
            second = root / 'second'
            second.mkdir()
            (second / 'blas.dll').write_text('other')
            with self.assertRaisesRegex(ValueError, 'ambiguous DLL'):
                bundle_windows(source / 'engine.exe', root / 'ambiguous', [second], run)
            (source / 'blas.dll').unlink()
            with self.assertRaisesRegex(ValueError, 'Missing'):
                bundle_windows(source / 'engine.exe', root / 'missing', [], run)

    def test_runtime_environment_does_not_forward_library_search_or_credentials(self):
        with patch.dict(os.environ, {'LD_LIBRARY_PATH': '/developer', 'DYLD_LIBRARY_PATH': '/developer', 'TOKEN': 'secret', 'PATH': '/developer', 'HOME': '/home/test'}, clear=True):
            env = runtime_environment()
            self.assertNotIn('TOKEN', env)
            self.assertNotIn('LD_LIBRARY_PATH', env)
            self.assertNotIn('DYLD_LIBRARY_PATH', env)
            self.assertNotEqual(env['PATH'], '/developer')

    @unittest.skipUnless(platform.system() == 'Linux', 'native Linux relocation case')
    def test_native_linux_dependency_chain_survives_source_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'source'
            source.mkdir()
            def build(name, code, *flags):
                c = source / (name + '.c')
                c.write_text(code)
                subprocess.run(['cc', str(c), *flags, '-o', str(source / name)], check=True, capture_output=True, timeout=60)
            build('libchild.so', 'int child(void) { return 7; }', '-shared', '-fPIC', '-Wl,-soname,libchild.so')
            build('libparent.so', 'extern int child(void); int parent(void) { return child()+1; }', '-shared', '-fPIC', '-L'+str(source), '-lchild', '-Wl,-soname,libparent.so', '-Wl,-rpath,'+str(source))
            build('engine', 'extern int parent(void); int main(void) { return parent()==8 ? 0 : 1; }', '-L'+str(source), '-lparent', '-Wl,-rpath,'+str(source))
            executable = bundle_linux(source / 'engine', root / 'installed', native_host(), command)
            shutil.rmtree(source)
            subprocess.run([str(executable)], env=runtime_environment(), check=True, timeout=10)

    @unittest.skipUnless(platform.system() == 'Windows', 'native Windows x64 DLL case')
    def test_native_windows_dependency_chain_survives_source_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / 'source'
            source.mkdir()
            def build(name, code, *flags):
                c = source / (name + '.c')
                c.write_text(code)
                link = ['/OUT:' + str(source / name)]
                if name.endswith('.dll'):
                    link.append('/IMPLIB:' + str(source / (Path(name).stem + '.lib')))
                result = subprocess.run(['cl', '/nologo', '/MT', str(c), *flags, '/link', *link], cwd=source, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            build('child.dll', '__declspec(dllexport) int child(void) { return 7; }', '/LD')
            build('parent.dll', '__declspec(dllimport) int child(void); __declspec(dllexport) int parent(void) { return child()+1; }', '/LD', str(source / 'child.lib'))
            build('engine.exe', '__declspec(dllimport) int parent(void); int main(void) { return parent()==8 ? 0 : 1; }', str(source / 'parent.lib'))
            executable = bundle_windows(source / 'engine.exe', root / 'installed', [], command)
            shutil.rmtree(source)
            subprocess.run([str(executable)], env=runtime_environment(), check=True, timeout=10)


class QualificationTests(unittest.TestCase):
    def test_numerical_oracle_rejects_wrong_impact_incomplete_or_invalid_residual(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / 'cache/lciamethods'
            cache.mkdir(parents=True)
            with gzip.open(cache / 'flow_factors.json.gz', 'wt', encoding='utf-8') as stream:
                json.dump({CO2+':OUTPUT': {'@version': '03.00.004', 'factor': [{'key': METHOD, 'value': 1}]}}, stream)
            for index, change in enumerate([{}, {'h': [0]}, {'completeLciaScores': False}, {'maxResidual': float('nan')}, {'maxResidual': -1}]):
                def execute(args, **kwargs):
                    run = Path(args[args.index('--output-dir')+1])
                    run.mkdir()
                    amount = json.loads(Path(args[args.index('--input')+1]).read_text())['amount']
                    result = {'g': [amount*7], 'h': [amount*7], 'completeLciaScores': True, 'maxResidual': 0, **change}
                    (run / 'result.json').write_text(json.dumps(result))
                with patch('qualify_model_runtime.subprocess.run', side_effect=execute):
                    if not change:
                        receipt = qualify(root / 'engine', cache.parent, root / str(index))
                        self.assertEqual([v['actualImpact'] for v in receipt['cases']], [21, 42])
                    else:
                        with self.assertRaises(ValueError):
                            qualify(root / 'engine', cache.parent, root / str(index))


if __name__ == '__main__':
    unittest.main()
