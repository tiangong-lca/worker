#!/usr/bin/env python3
"""Build a relocatable local Model package on a supported native host; never publish it."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from model_native_platforms import bundle_linux, bundle_windows, native_host, runtime_environment


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=120, env={**os.environ, "LC_ALL": "C"}).stdout


def dependencies(binary):
    identity = command("/usr/bin/otool", "-D", str(binary)).splitlines()[1:]
    return [line.strip().split(" (compatibility", 1)[0]
            for line in command("/usr/bin/otool", "-L", str(binary)).splitlines()[1:]
            if line.strip().split(" (compatibility", 1)[0] not in identity]


def rpaths(binary):
    lines = command("/usr/bin/otool", "-l", str(binary)).splitlines()
    return [lines[i + 2].strip().split(" (offset", 1)[0].removeprefix("path ")
            for i, line in enumerate(lines) if line.strip() == "cmd LC_RPATH"]


def system_library(name):
    return name.startswith(("/System/Library/", "/usr/lib/"))


def resolve_library(name, binary, executable, ancestors=()):
    def expand(value, owner):
        return value.replace("@loader_path", str(owner.parent)).replace(
            "@executable_path", str(executable.parent))
    if name.startswith("@rpath/"):
        # dyld searches the loader's runpaths, then its ancestors in load order.
        # Each @loader_path belongs to the image declaring that LC_RPATH.
        owners = list(dict.fromkeys((binary, *ancestors, executable)))
        for owner in owners:
            for root in rpaths(owner):
                candidate = Path(expand(root, owner)) / name.removeprefix("@rpath/")
                if candidate.is_file():
                    return candidate.resolve()
        raise ValueError(f"Missing native dependency in declared runpaths: {name}")
    result = Path(expand(name, binary)).resolve(strict=True)
    if not result.is_file():
        raise ValueError(f"Not a native library: {name}")
    return result


def bundle_native(engine, root):
    executable = root / "bin/model_calculate"
    executable.parent.mkdir(parents=True)
    shutil.copy2(engine, executable)
    pending = [(engine, executable, ())]
    seen = {}
    packaged = []
    while pending:
        source, target, ancestors = pending.pop(0)
        if command("/usr/bin/lipo", "-archs", str(source)).strip() != "arm64":
            raise ValueError("Package requires native ARM64 binaries")
        changes = []
        for name in dependencies(source):
            if system_library(name):
                continue
            dependency = resolve_library(name, source, engine, ancestors)
            if dependency.name in seen and seen[dependency.name] != dependency:
                raise ValueError("Native dependency basename collision")
            if dependency.name not in seen:
                seen[dependency.name] = dependency
                destination = root / "bin/lib" / dependency.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dependency, destination)
                pending.append((dependency, destination, (source, *ancestors)))
            relative = "lib/" if target == executable else ""
            changes += ["-change", name, "@loader_path/" + relative + dependency.name]
        for old in rpaths(source):
            changes += ["-delete_rpath", old]
        if target != executable:
            changes += ["-id", "@loader_path/" + target.name]
        target.chmod(0o755)
        if changes:
            command("/usr/bin/install_name_tool", *changes, str(target))
        command("/usr/bin/codesign", "--force", "--sign", "-", str(target))
        packaged.append(target)
    for binary in packaged:
        if rpaths(binary) or any(not system_library(name) and not name.startswith("@loader_path/")
                                for name in dependencies(binary)):
            raise ValueError("Packaged native binary still references external dependencies")
    # Carry installed third-party notices; release review must also review source offers/SBOM.
    for source in seen.values():
        prefix = source.parent.parent
        notice = prefix / "LICENSE.txt"
        if notice.is_file():
            destination = root / "notices" / (prefix.parent.name + "-LICENSE.txt")
            destination.parent.mkdir(exist_ok=True)
            shutil.copy2(notice, destination)
    return executable


def digest(file):
    hash_value = hashlib.sha256()
    with file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hash_value.update(chunk)
    return hash_value.hexdigest()


def archive(root, output):
    files = []
    with output.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
        for file in sorted(root.rglob("*")):
            if file.is_symlink():
                raise ValueError("Package symlinks are not allowed")
            if file.is_dir():
                continue
            if not file.is_file():
                raise ValueError("Only regular package files are allowed")
            name = file.relative_to(root).as_posix()
            size = file.stat().st_size
            mode = 0o755 if name.startswith("bin/") else 0o644
            info = tarfile.TarInfo(name)
            info.size, info.mode = size, mode
            stream.write(info.tobuf(format=tarfile.USTAR_FORMAT))
            with file.open("rb") as content:
                shutil.copyfileobj(content, stream)
            stream.write(bytes((-size) % 512))
            files.append(dict(path=name, bytes=size, sha256=digest(file), mode=mode))
        stream.write(bytes(1024))
    return files


def publish(package, output):
    # Claim a new directory exclusively; rename() alone can replace an existing
    # empty directory created while the native readiness check was running.
    output.mkdir(mode=0o700)
    try:
        for name in ["runtime.tar.gz", "manifest.json"]:
            os.rename(package / name, output / name)
    except BaseException:
        shutil.rmtree(output)
        raise


def check_engine(engine, cache, work):
    response = subprocess.run(
        [str(engine), "--check", "--static-cache", str(cache)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=work, env=runtime_environment())
    if response.returncode:
        raise ValueError(f"Packaged engine self-check failed ({response.returncode}): "
                         f"{response.stdout} {response.stderr}")
    return json.loads(response.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--static-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--library-dir", type=Path, action="append", default=[],
                        help="Explicit Windows DLL directory; repeat for numerical and MSVC runtime libraries")
    parser.add_argument("--notices", type=Path, help="Reviewed third-party notices directory to include")
    args = parser.parse_args()
    args.output = args.output.absolute()
    host = native_host()
    if args.library_dir and host != "win32-x64":
        parser.error("--library-dir applies only to Windows DLL packaging")
    if args.output.exists():
        parser.error("Output must be a new directory")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="model-package-", dir=args.output.parent) as temporary:
        work = Path(temporary)
        root = work / "payload"
        source = args.engine.resolve(strict=True)
        if host == "darwin-arm64":
            engine = bundle_native(source, root)
        elif host.startswith("linux-"):
            engine = bundle_linux(source, root, host, command)
        else:
            engine = bundle_windows(source, root, [p.resolve(strict=True) for p in args.library_dir], command)
        if args.notices:
            shutil.copytree(args.notices, root / "notices", dirs_exist_ok=True, symlinks=True)
        manifest_file = Path(__file__).resolve().parents[1] / "crates/solver-worker/src/lcia_static_cache_bundle_manifest.json"
        manifest = json.loads(manifest_file.read_text())
        asset_names = {"lciamethods/" + entry["path"] for entry in manifest["files"].values()}
        asset_names.add("lciamethods/cache_manifest.json")
        for name in sorted(asset_names):
            destination = root / "cache" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(args.static_cache / name, destination)
        # The real native loader checks the complete method/cache contract before packaging.
        capability = check_engine(engine, root / "cache", work)
        if capability.get("status") != "ready" or capability.get("characterization") != "method-defined-sparse":
            raise ValueError("Engine lacks the required local Model capability")
        package = work / "package"
        package.mkdir()
        output = package / "runtime.tar.gz"
        files = archive(root, output)
        (package / "manifest.json").write_text(json.dumps(dict(
            schema="lca.model-runtime-package.v1", platform=host,
            engineVersion=capability["engineVersion"], files=files,
            archive=dict(bytes=output.stat().st_size, sha256=digest(output))), indent=2) + "\n")
        publish(package, args.output)
    print(json.dumps(dict(status="packaged", directory=str(args.output.resolve()),
                         platform=host, files=len(files), published=False)))


if __name__ == "__main__":
    main()
