"""Platform-specific native library relocation for trusted local Model builds."""
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil


SUPPORTED = ('darwin-arm64', 'linux-x64', 'linux-arm64', 'win32-x64')


def native_host():
    system, machine = platform.system(), platform.machine().lower()
    architecture = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(machine)
    name = {'Darwin': 'darwin', 'Linux': 'linux', 'Windows': 'win32'}.get(system)
    host = f'{name}-{architecture}'
    if host not in SUPPORTED:
        raise ValueError('Supported native hosts: ' + ', '.join(SUPPORTED) + '; macOS Intel is unsupported')
    if name == 'linux' and platform.libc_ver()[0] != 'glibc':
        raise ValueError('Linux packages require glibc; musl is unsupported')
    return host


# These belong to the target OS libc, not to the distributable numerical runtime.
LINUX_SYSTEM = {'libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2', 'librt.so.1',
                'libresolv.so.2', 'libutil.so.1', 'libanl.so.1',
                'ld-linux-x86-64.so.2', 'ld-linux-aarch64.so.1'}
WINDOWS_SYSTEM = {'kernel32.dll', 'ntdll.dll', 'advapi32.dll', 'bcrypt.dll', 'bcryptprimitives.dll',
                  'crypt32.dll', 'cryptbase.dll', 'userenv.dll', 'ws2_32.dll', 'ncrypt.dll',
                  'secur32.dll', 'sspicli.dll', 'user32.dll', 'gdi32.dll',
                  'shell32.dll', 'ole32.dll', 'oleaut32.dll', 'rpcrt4.dll', 'shlwapi.dll',
                  'version.dll', 'psapi.dll', 'dbghelp.dll', 'iphlpapi.dll', 'winmm.dll',
                  'msvcrt.dll', 'ucrtbase.dll', 'normaliz.dll', 'powrprof.dll'}


def linux_dependencies(output):
    """ldd on a trusted executable reports its full transitive loader closure."""
    resolved = {}
    for line in output.splitlines():
        line = line.strip()
        if 'not found' in line:
            raise ValueError('Unresolved native dependency: ' + line)
        match = re.fullmatch(r'(\S+)\s+=>\s+(.+?)\s+\(0x[0-9a-fA-F]+\)', line)
        if match:
            name, source = match.groups()
            if not PurePosixPath(source).is_absolute():
                raise ValueError('Expected an absolute native dependency path')
            if name in resolved and resolved[name] != Path(source):
                raise ValueError('Conflicting native dependency: ' + name)
            resolved[name] = Path(source)
        elif line and not re.fullmatch(r'(linux-vdso\S*|/\S*ld-linux\S*)\s+\(0x[0-9a-fA-F]+\)', line):
            # Absolute DT_NEEDED entries appear without an => separator.
            direct = re.fullmatch(r'(/.+?)\s+\(0x[0-9a-fA-F]+\)', line)
            if not direct:
                raise ValueError('Unrecognized native loader output: ' + line)
            resolved[direct[1]] = Path(direct[1])
    return resolved


def bundle_linux(engine, root, host, run):
    expected = 'AArch64' if host == 'linux-arm64' else 'Advanced Micro Devices X86-64'
    resolved = linux_dependencies(run('ldd', str(engine)))
    executable = root / 'bin/model_calculate'
    executable.parent.mkdir(parents=True)
    shutil.copy2(engine, executable)
    copies = [(engine, executable)]
    identities = {}
    for name, source in resolved.items():
        basename = Path(name).name
        if basename in LINUX_SYSTEM:
            continue
        if basename in identities and identities[basename] != source.resolve(strict=True):
            raise ValueError('Native dependency basename collision: ' + basename)
        if basename not in identities:
            identities[basename] = source.resolve(strict=True)
            target = root / 'bin/lib' / basename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copies.append((source, target))
    for source, target in copies:
        header = run('readelf', '-h', str(source))
        if not re.search(r'Class:\s+ELF64\b', header) or not re.search(r'Machine:\s+' + re.escape(expected) + r'\s*$', header, re.M):
            raise ValueError('Native ELF architecture does not match ' + host)
        for name in run('patchelf', '--print-needed', str(target)).splitlines():
            if name not in resolved and Path(name).name not in LINUX_SYSTEM:
                raise ValueError('Dependency not present in resolved loader closure: ' + name)
            if '/' in name:
                run('patchelf', '--replace-needed', name, Path(name).name, str(target))
        # RUNPATH is set on every image; it does not propagate to grandchildren.
        target.chmod(0o755)
        run('patchelf', '--set-rpath', '$ORIGIN/lib' if target == executable else '$ORIGIN', str(target))
    # Verify relocation selects packaged libraries, rather than a development copy.
    for name, source in linux_dependencies(run('ldd', str(executable))).items():
        if Path(name).name not in LINUX_SYSTEM and not source.resolve().is_relative_to(root.resolve()):
            raise ValueError('Packaged binary still loads an external library: ' + name)
    return executable


def windows_dependencies(output):
    return list(dict.fromkeys(line.strip() for line in output.splitlines()
                              if re.fullmatch(r'[^\\/:\s]+\.dll', line.strip(), re.I)))


def windows_system(name):
    return name.lower() in WINDOWS_SYSTEM or name.lower().startswith(('api-ms-win-', 'ext-ms-win-'))


def bundle_windows(engine, root, library_dirs, run):
    executable = root / 'bin/model_calculate.exe'
    executable.parent.mkdir(parents=True)
    shutil.copy2(engine, executable)
    # Explicit directories only: never sweep PATH or copy arbitrary System32 files.
    directories = list(dict.fromkeys([engine.parent, *library_dirs]))
    available = {}
    for directory in directories:
        for file in directory.iterdir():
            if file.is_file() and file.suffix.lower() == '.dll':
                available.setdefault(file.name.lower(), []).append(file.resolve())
    pending = [(engine, executable)]
    copied = {}
    while pending:
        source, target = pending.pop(0)
        if not re.search(r'8664\s+machine\s+\(x64\)', run('dumpbin', '/headers', str(source)), re.I):
            raise ValueError('Windows package requires x64 PE binaries')
        for name in windows_dependencies(run('dumpbin', '/dependents', str(source))):
            if windows_system(name):
                continue
            matches = set(available.get(name.lower(), []))
            if len(matches) != 1:
                raise ValueError('Missing or ambiguous DLL; supply its directory with --library-dir: ' + name)
            dependency = matches.pop()
            if name.lower() not in copied:
                destination = executable.parent / name
                shutil.copy2(dependency, destination)
                copied[name.lower()] = dependency
                pending.append((dependency, destination))
    return executable


def runtime_environment():
    """Readiness must not rely on developer library paths or business credentials."""
    names = ('HOME', 'USERPROFILE', 'TMPDIR', 'TMP', 'TEMP', 'SystemRoot', 'SYSTEMROOT', 'WINDIR')
    env = {name: os.environ[name] for name in names if name in os.environ}
    env['PATH'] = os.path.join(os.environ.get('SystemRoot', 'C:\\Windows'), 'System32') if os.name == 'nt' else '/usr/bin:/bin'
    return env
