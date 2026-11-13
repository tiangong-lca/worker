#!/usr/bin/env python3
"""Exercise a public installed CLI against the native package in isolated state."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile


def qualify(cli, package, input_file, version, output):
    installed = json.loads((cli.parent.parent / 'package.json').read_text(encoding='utf-8'))
    if installed['name'] != '@tiangong-lca/cli' or installed['version'] != version:
        raise ValueError('Public CLI version does not match the selected release consumer')
    with tempfile.TemporaryDirectory(prefix='model-cli-consumer-') as directory:
        work = Path(directory)
        env = {**os.environ, 'XDG_STATE_HOME': str(work / 'state')}
        operations = []

        def invoke(*args):
            response = subprocess.run(['node', str(cli), *args, '--json'], cwd=work,
                                      env=env, capture_output=True, text=True, encoding='utf-8', timeout=180)
            if response.returncode:
                raise ValueError(f'Installed CLI failed: {response.stdout} {response.stderr}')
            value = json.loads(response.stdout)
            if value.get('status') != 'completed':
                raise ValueError('Installed CLI operation failed: ' + response.stdout)
            operations.append(value['operation'])
            return value

        invoke('setup', 'model-calculation', '--install', str(package))
        invoke('setup', 'model-calculation', '--check')
        original = json.loads(input_file.read_text(encoding='utf-8'))
        request = {k: v for k, v in original.items() if k != 'schemaVersion'}
        request['model'] = {'content': request['model']}
        request['datasets'] = [{'content': value} for value in request['datasets']]
        (work / 'request.json').write_text(json.dumps(request))
        invoke('dataset', 'model', 'prepare', '--input', str(work / 'request.json'), '--output', str(work / 'input.json'))
        facts = []
        for amount in (3, 6):
            document = json.loads((work / 'input.json').read_text(encoding='utf-8'))
            document['amount'] = amount
            source = work / f'input-{amount}.json'
            source.write_text(json.dumps(document))
            run = work / f'run-{amount}'
            invoke('dataset', 'model', 'calculate', '--file', str(source), '--output-dir', str(run))
            result = json.loads((run / 'result.json').read_text(encoding='utf-8'))
            if len(result['h']) != 1 or not math.isclose(result['h'][0], amount * 7, rel_tol=1e-12):
                raise ValueError('Installed CLI disagrees with the independent numerical oracle')
            invoke('calculation', 'get', '--dir', str(run))
            for kind in ('activities', 'lci', 'lcia'):
                invoke('calculation', 'result', '--dir', str(run), '--kind', kind, '--output', str(work / f'{kind}-{amount}.json'))
            facts.append(result['h'][0])
        report = {'title': {'en': 'Installed runtime verification', 'zh': '运行时安装验证'},
                  'results': [{'label': 'Synthetic base demand', 'value': facts[0], 'unit': 'kg CO2-eq'},
                              {'label': 'Synthetic doubled demand', 'value': facts[1], 'unit': 'kg CO2-eq'}],
                  'limitations': ['Synthetic qualification model; not a product study.']}
        (work / 'report.json').write_text(json.dumps(report))
        invoke('report', 'render', '--input', str(work / 'report.json'), '--output', str(work / 'report.html'))
        html = (work / 'report.html').read_text(encoding='utf-8')
        if '<html' not in html or not all(str(int(value)) in html for value in facts):
            raise ValueError('HTML report is missing computed results')
        receipt = dict(status='passed', cliVersion=version, isolatedState=True,
                       expectedImpacts=[21, 42], actualImpacts=facts, operations=operations)
        output.write_text(json.dumps(receipt, indent=2) + '\n')
        return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(args.cli.resolve(), args.package.resolve(), args.input.resolve(), args.version, args.output)))
