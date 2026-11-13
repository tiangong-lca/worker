#!/usr/bin/env python3
"""Verify complete four-platform release assets before publishing any runtime."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import tarfile

PLATFORMS = ('darwin-arm64', 'linux-x64', 'linux-arm64', 'win32-x64')


def verify(directory, commit, cli_version):
    expected = {f'model-runtime-{p}{suffix}' for p in PLATFORMS for suffix in
                ('.tar.gz', '-sources.tar.gz', '-qualification.json', '-cli-qualification.json', '-provenance.json')}
    if {p.name for p in directory.iterdir()} != expected:
        raise ValueError('Release must contain exactly four complete platform asset sets')
    for platform in PLATFORMS:
        prefix = directory / ('model-runtime-' + platform)
        provenance = json.loads(Path(str(prefix) + '-provenance.json').read_text())
        if provenance['commit'] != commit or provenance['platform'] != platform or provenance['repository'] != 'tiangong-lca/worker':
            raise ValueError('Release provenance does not match this source/platform')
        proof = json.loads(Path(str(prefix) + '-qualification.json').read_text())
        if proof['status'] != 'passed' or proof['platform'] != platform or proof['developerLibraryEnvironmentRemoved'] is not True:
            raise ValueError('Missing native relocation proof')
        if len(proof['cases']) != 2:
            raise ValueError('Both numerical cases are required')
        for case, amount in zip(proof['cases'], (3, 6), strict=True):
            if case['functionalAmount'] != amount or case['expectedInventoryAndImpact'] != amount * 7 or not math.isclose(case['actualImpact'], amount * 7, rel_tol=1e-12):
                raise ValueError('Native oracle mismatch')
        consumer = json.loads(Path(str(prefix) + '-cli-qualification.json').read_text())
        required = {'setup model-calculation', 'dataset model prepare', 'dataset model calculate',
                    'calculation get', 'calculation result', 'report render'}
        if consumer['status'] != 'passed' or consumer['isolatedState'] is not True or consumer['actualImpacts'] != [21, 42] or consumer['cliVersion'] != cli_version or not required.issubset(consumer['operations']):
            raise ValueError('Public CLI consumer qualification is missing or invalid')
        with tarfile.open(str(prefix) + '.tar.gz') as package:
            members = {m.name.removeprefix('./'): m for m in package.getmembers() if m.isfile()}
            if set(members) != {'manifest.json', 'runtime.tar.gz'}:
                raise ValueError('Invalid install package contents')
            manifest = json.load(package.extractfile(members['manifest.json']))
            if manifest['schema'] != 'lca.model-runtime-package.v1' or manifest['platform'] != platform:
                raise ValueError('Invalid runtime manifest')
            archive = members['runtime.tar.gz']
            digest = hashlib.file_digest(package.extractfile(archive), 'sha256').hexdigest()
            if archive.size != manifest['archive']['bytes'] or digest != manifest['archive']['sha256']:
                raise ValueError('Runtime integrity mismatch')
            if not any(f['path'].startswith('notices/') for f in manifest['files']):
                raise ValueError('Runtime notices are missing')
        with tarfile.open(str(prefix) + '-sources.tar.gz') as source:
            components = json.load(source.extractfile('sources/components.json'))
            if components['sourceCommit'] != commit or components['platform'] != platform or not components['native'] or not components['crates']:
                raise ValueError('Corresponding source identity/inventory is missing')
            names = source.getnames()
            for required in ('sources/worker/Cargo.lock', 'sources/cargo-vendor-config.toml', 'notices/SOURCE.txt'):
                if required not in names:
                    raise ValueError('Incomplete corresponding source: ' + required)
    return {'status': 'passed', 'commit': commit, 'platforms': list(PLATFORMS)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--cli-version', required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.directory, args.commit, args.cli_version)))
