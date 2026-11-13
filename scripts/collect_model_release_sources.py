#!/usr/bin/env python3
"""Collect corresponding source and notices for a native Model distribution."""
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

from model_native_platforms import LINUX_SYSTEM, linux_dependencies, native_host
from package_model_runtime import dependencies, resolve_library, system_library


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def libraries(engine):
    pending = [(engine, ())]
    seen = set()
    while pending:
        binary, ancestors = pending.pop()
        for name in dependencies(binary):
            if system_library(name):
                continue
            library = resolve_library(name, binary, engine, ancestors)
            if library not in seen:
                seen.add(library)
                pending.append((library, (binary, *ancestors)))
    return seen


def linux_sources(engine, source, notices):
    # Debian copyright files refer to these texts; carry them with the binaries.
    shutil.copytree('/usr/share/common-licenses', notices / 'debian-common-licenses')
    packages = set()
    for name, path in linux_dependencies(run('ldd', str(engine))).items():
        if Path(name).name in LINUX_SYSTEM:
            continue
        # dpkg may record the pre-/usr-merge spelling.
        attempts = [str(path), str(path.resolve())]
        if str(path).startswith('/lib/'):
            attempts.append('/usr' + str(path))
        owner = None
        for candidate in attempts:
            found = subprocess.run(['dpkg-query', '-S', candidate], capture_output=True, text=True)
            if found.returncode == 0:
                owner = found.stdout.split(': /', 1)[0]
                break
        if not owner:
            raise ValueError('No Debian package owns native library: ' + str(path))
        packages.add(owner)
    records = []
    for package in sorted(packages):
        fields = run('dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}', package).split('\t')
        binary, version, origin, source_version = fields
        copyright_file = Path('/usr/share/doc') / binary.split(':')[0] / 'copyright'
        shutil.copy2(copyright_file, notices / (binary.replace(':', '-') + '-copyright'))
        subprocess.run(['apt-get', 'source', '--download-only', origin + '=' + source_version], cwd=source, check=True)
        records.append(dict(package=binary, version=version, source=origin, sourceVersion=source_version))
    return records


def mac_sources(engine, source, notices):
    cellar = Path(run('brew', '--cellar')).resolve()
    formulas = {}
    for library in libraries(engine):
        relative = library.relative_to(cellar)
        formulas[relative.parts[0]] = relative.parts[1]
    records = []
    for formula, installed_version in sorted(formulas.items()):
        info = json.loads(run('brew', 'info', '--json=v2', formula))['formulae'][0]
        stable = info['versions']['stable']
        revision = info.get('revision', 0)
        expected = stable + ('_' + str(revision) if revision else '')
        if installed_version != expected:
            raise ValueError(f'Installed {formula} {installed_version} differs from source recipe {expected}')
        # Preserve every fetched source/resource/patch, not only the main tarball.
        cache = source / (formula + '-downloads')
        subprocess.run(['brew', 'fetch', '--build-from-source', formula], check=True,
                       env={**os.environ, 'HOMEBREW_CACHE': str(cache), 'HOMEBREW_NO_AUTO_UPDATE': '1'})
        if not list(cache.rglob('*')):
            raise ValueError('Homebrew source downloads are missing')
        prefix = cellar / formula / installed_version
        shutil.copytree(prefix / '.brew', source / (formula + '-recipe'))
        for notice in prefix.glob('*'):
            if notice.is_file() and any(word in notice.name.upper() for word in ('LICENSE', 'COPYING', 'COPYRIGHT', 'NOTICE')):
                shutil.copy2(notice, notices / (formula + '-' + notice.name))
        records.append(dict(formula=formula, version=installed_version, recipe=info))
    return records


def windows_sources(vcpkg, source, notices):
    installed = vcpkg / 'installed'
    for file in (installed / 'x64-windows/share').rglob('copyright'):
        shutil.copy2(file, notices / (file.parent.name + '-copyright'))
    shutil.copytree(vcpkg / 'ports', source / 'ports')
    shutil.copytree(vcpkg / 'triplets', source / 'triplets')
    shutil.copytree(vcpkg / 'scripts', source / 'scripts')
    shutil.copytree(installed / 'vcpkg', source / 'installed-metadata')
    downloads = source / 'downloads'
    downloads.mkdir()
    for file in (vcpkg / 'downloads').iterdir():
        if file.is_file() and not file.name.endswith('.part'):
            shutil.copy2(file, downloads / file.name)
    if not list(downloads.iterdir()):
        raise ValueError('vcpkg native source downloads are missing')
    for file in ['LICENSE.txt', 'vcpkg.json', 'bootstrap-vcpkg.bat']:
        if (vcpkg / file).is_file():
            shutil.copy2(vcpkg / file, source / file)
    # Hosted VS images do not necessarily retain a local license.txt. Preserve
    # Microsoft's public component terms and redistribution list, never invent one.
    if not os.environ.get('VisualStudioVersion', '').startswith('18.'):
        raise ValueError('Review Microsoft component terms for this Visual Studio generation')
    documents = {
        'microsoft-vc-runtime-license.docx': 'https://visualstudio.microsoft.com/wp-content/uploads/2025/10/Visual-C-V14-License-Redistributable_and_Runtime_ENU.docx',
        'microsoft-build-tools-license.docx': 'https://visualstudio.microsoft.com/wp-content/uploads/2025/10/Visual_Studio_2026-License-Diagnostic_and_Build_Tools_ENU.docx',
        'microsoft-redistribution.html': 'https://learn.microsoft.com/en-us/visualstudio/releases/2026/redistribution',
    }
    for name, url in documents.items():
        with urllib.request.urlopen(url, timeout=120) as response:
            (notices / name).write_bytes(response.read())
    return [dict(vcpkgCommit=run('git', '-C', str(vcpkg), 'rev-parse', 'HEAD'),
                 visualStudioVersion=os.environ.get('VisualStudioVersion'),
                 vcToolsVersion=os.environ.get('VCToolsVersion'), microsoftTerms=documents)]



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--vcpkg', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir()
    sources = output / 'sources'
    sources.mkdir()
    notices = output / 'notices'
    notices.mkdir()
    # Retain the exact repository tree, locked Rust dependencies and their licenses.
    source_tar = output / 'worker-source.tar'
    subprocess.run(['git', 'archive', '--format=tar', '-o', str(source_tar), 'HEAD'], cwd=root, check=True)
    with tarfile.open(source_tar) as archive:
        archive.extractall(sources / 'worker', filter='data')
    source_tar.unlink()
    vendor = sources / 'vendor'
    vendor_config = re.sub(r'^directory = .*$', 'directory = "../vendor"',
                           run('cargo', 'vendor', '--locked', str(vendor), cwd=root), flags=re.M)
    (sources / 'cargo-vendor-config.toml').write_text(vendor_config + '\n')
    metadata = json.loads(run('cargo', 'metadata', '--locked', '--format-version', '1', cwd=root))
    for package in vendor.iterdir():
        for file in package.iterdir():
            if file.is_file() and any(word in file.name.upper() for word in ('LICENSE', 'COPYING', 'COPYRIGHT', 'NOTICE')):
                shutil.copy2(file, notices / (package.name + '-' + file.name))
    rustc = run('rustc', '-Vv')
    rust_commit = next(line.split(': ', 1)[1] for line in rustc.splitlines() if line.startswith('commit-hash: '))
    for name in ('LICENSE-APACHE', 'LICENSE-MIT', 'COPYRIGHT'):
        url = f'https://raw.githubusercontent.com/rust-lang/rust/{rust_commit}/{name}'
        with urllib.request.urlopen(url, timeout=120) as response:
            (notices / ('rust-' + name)).write_bytes(response.read())
    rust_docs = Path(run('rustc', '--print', 'sysroot')) / 'share/doc/rust'
    if not (rust_docs / 'COPYRIGHT-library.html').is_file():
        subprocess.run(['rustup', 'component', 'add', 'rust-docs'], check=True)
    shutil.copy2(rust_docs / 'COPYRIGHT-library.html', notices / 'rust-COPYRIGHT-library.html')
    shutil.copytree(rust_docs / 'licenses', notices / 'rust-licenses')
    native = sources / 'native'
    native.mkdir()
    host = native_host()
    if host.startswith('linux-'):
        components = linux_sources(args.engine.resolve(), native, notices)
    elif host == 'darwin-arm64':
        components = mac_sources(args.engine.resolve(), native, notices)
    else:
        components = windows_sources(args.vcpkg.resolve(), native, notices)
    record = dict(sourceCommit=run('git', 'rev-parse', 'HEAD', cwd=root), platform=host,
                  rustc=rustc, native=components,
                  crates=[{k: p.get(k) for k in ['name', 'version', 'license', 'source']} for p in metadata['packages']])
    (sources / 'components.json').write_text(json.dumps(record, indent=2) + '\n')
    (notices / 'components.json').write_text(json.dumps(record, indent=2) + '\n')
    (notices / 'SOURCE.txt').write_text('Corresponding source, dependency recipes and notices accompany this runtime in the matching sources archive on the same GitHub release. See sources/worker/.github/workflows/model-runtime.yml for the build procedure. Individual components retain their licenses.\n')
    with tarfile.open(output / 'sources.tar.gz', 'w:gz') as archive:
        archive.add(sources, arcname='sources')
        archive.add(notices, arcname='notices')


if __name__ == '__main__':
    main()
