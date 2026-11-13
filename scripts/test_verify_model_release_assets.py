import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from verify_model_release_assets import PLATFORMS, verify


def archive(path, files):
    with tarfile.open(path, 'w:gz') as out:
        for name, data in files.items():
            data = data if isinstance(data, bytes) else json.dumps(data).encode()
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            out.addfile(entry, io.BytesIO(data))


class ReleaseAssetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.commit = 'a' * 40
        for platform in PLATFORMS:
            stem = 'model-runtime-' + platform
            (self.root / (stem + '-provenance.json')).write_text(json.dumps(dict(
                commit=self.commit, repository='tiangong-lca/worker', platform=platform)))
            (self.root / (stem + '-qualification.json')).write_text(json.dumps(dict(
                status='passed', platform=platform, developerLibraryEnvironmentRemoved=True,
                cases=[dict(functionalAmount=a, expectedInventoryAndImpact=a*7, actualImpact=a*7) for a in (3, 6)])))
            (self.root / (stem + '-cli-qualification.json')).write_text(json.dumps(dict(
                status='passed', isolatedState=True, cliVersion='0.2.1', actualImpacts=[21, 42],
                operations=['setup model-calculation', 'dataset model prepare', 'dataset model calculate',
                            'calculation get', 'calculation result', 'report render'])))
            payload = b'fixture archive'
            archive(self.root / (stem + '.tar.gz'), {
                'manifest.json': dict(schema='lca.model-runtime-package.v1', platform=platform,
                    archive=dict(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()),
                    files=[dict(path='notices/SOURCE.txt')]), 'runtime.tar.gz': payload})
            archive(self.root / (stem + '-sources.tar.gz'), {
                'sources/components.json': dict(sourceCommit=self.commit, platform=platform, native=['fixture'], crates=['fixture']),
                'sources/worker/Cargo.lock': b'fixture', 'sources/cargo-vendor-config.toml': b'fixture', 'notices/SOURCE.txt': b'fixture'})

    def test_complete_native_matrix(self):
        self.assertEqual(verify(self.root, self.commit, '0.2.1')['status'], 'passed')

    def test_missing_platform_cannot_publish(self):
        (self.root / 'model-runtime-win32-x64.tar.gz').unlink()
        with self.assertRaisesRegex(ValueError, 'exactly four'):
            verify(self.root, self.commit, '0.2.1')

    def test_missing_public_consumer_operation_cannot_publish(self):
        file = self.root / 'model-runtime-win32-x64-cli-qualification.json'
        data = json.loads(file.read_text())
        data['operations'].remove('report render')
        file.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'consumer'):
            verify(self.root, self.commit, '0.2.1')

    def test_other_commit_cannot_publish(self):
        with self.assertRaisesRegex(ValueError, 'provenance'):
            verify(self.root, 'b' * 40, '0.2.1')

    def test_incorrect_numerical_receipt_cannot_publish(self):
        file = self.root / 'model-runtime-linux-x64-qualification.json'
        receipt = json.loads(file.read_text())
        receipt['cases'][1]['actualImpact'] = 41
        file.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'oracle'):
            verify(self.root, self.commit, '0.2.1')

    def test_mismatched_source_cannot_publish(self):
        archive(self.root / 'model-runtime-darwin-arm64-sources.tar.gz', {
            'sources/components.json': dict(sourceCommit='b'*40, platform='darwin-arm64', native=['fixture'], crates=['fixture'])})
        with self.assertRaisesRegex(ValueError, 'source identity'):
            verify(self.root, self.commit, '0.2.1')


if __name__ == '__main__':
    unittest.main()
