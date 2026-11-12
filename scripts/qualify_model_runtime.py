#!/usr/bin/env python3
"""Exercise a relocated local Model engine with synthetic foregrounds and real LCIA factors."""
import argparse
import gzip
import json
import math
from pathlib import Path
import subprocess

from model_native_platforms import native_host, runtime_environment

CO2 = '08a91e70-3ddc-11dd-923d-0050c2490048'
METHOD = '6209b35f-9447-40b5-b68c-a1099e3674a0'


def model_input(amount):
    def ref(id):
        return {'@refObjectId': id, '@version': '03.00.004' if id == CO2 else '1'}
    admin = {'publicationAndOwnership': {'common:dataSetVersion': '1'}}
    def exchange(id, flow, direction, value):
        return {'@dataSetInternalID': id, 'referenceToFlowDataSet': ref(flow),
                'exchangeDirection': direction, 'resultingAmount': value}
    def process(id, exchanges):
        return {'processDataSet': {'processInformation': {'dataSetInformation': {'common:UUID': id},
                'quantitativeReference': {'referenceToReferenceFlow': 'p'}},
                'administrativeInformation': admin, 'exchanges': {'exchange': exchanges}}}
    datasets = [process('foreground', [exchange('p', 'product', 'Output', 2),
                exchange('i', 'feed', 'Input', 1), exchange('e', CO2, 'Output', 10)]),
                process('supplier', [exchange('p', 'feed', 'Output', 1), exchange('e', CO2, 'Output', 4)])]
    for id in ['product', 'feed', CO2]:
        datasets.append({'flowDataSet': {'flowInformation': {'dataSetInformation': {'common:UUID': id},
            'quantitativeReference': {'referenceToReferenceFlowProperty': '0'}},
            'administrativeInformation': {'publicationAndOwnership': {'common:dataSetVersion': ref(id)['@version']}},
            'flowProperties': {'flowProperty': {'@dataSetInternalID': '0', 'meanValue': '1', 'referenceToFlowPropertyDataSet': ref('mass')}},
            'modellingAndValidation': {'LCIMethod': {'typeOfDataSet': 'Elementary flow' if id == CO2 else 'Product flow'}}}})
    datasets.extend([
        {'flowPropertyDataSet': {'flowPropertiesInformation': {'dataSetInformation': {'common:UUID': 'mass'},
            'quantitativeReference': {'referenceToReferenceUnitGroup': ref('kg')}}, 'administrativeInformation': admin}},
        {'unitGroupDataSet': {'unitGroupInformation': {'dataSetInformation': {'common:UUID': 'kg'},
            'quantitativeReference': {'referenceToReferenceUnit': '0'}}, 'administrativeInformation': admin,
            'units': {'unit': {'@dataSetInternalID': '0', 'name': 'kg', 'meanValue': '1'}}}}])
    instances = [
        {'@dataSetInternalID': 'a', 'referenceToProcess': ref('foreground'), 'connections': {'outputExchange': []}},
        {'@dataSetInternalID': 'b', 'referenceToProcess': ref('supplier'), 'connections': {'outputExchange': [
            {'@flowUUID': 'feed', '@version': '1', 'downstreamProcess': {'@id': 'a', '@flowUUID': 'feed', '@version': '1'}}]}}]
    model = {'lifeCycleModelDataSet': {'lifeCycleModelInformation': {'dataSetInformation': {'common:UUID': 'model'},
        'quantitativeReference': {'referenceToReferenceProcess': 'a'}, 'technology': {'processes': {'processInstance': instances}}},
        'administrativeInformation': admin}}
    return {'schemaVersion': 'lca.model-input.v1', 'model': model, 'datasets': datasets,
            'methods': [{'id': METHOD, 'version': '01.00.000'}], 'amount': amount,
            'backgrounds': [], 'cutoffs': [], 'limits': {}}


def qualify(engine, cache, output):
    output.mkdir(mode=0o700)
    with gzip.open(cache / 'lciamethods/flow_factors.json.gz', 'rt', encoding='utf-8') as stream:
        factors = json.load(stream)[CO2 + ':OUTPUT']
    values = {float(f['value']) for f in factors['factor'] if f['key'] == METHOD}
    if factors['@version'] != '03.00.004' or values != {1.0}:
        raise ValueError('Qualification requires the reviewed fossil CO2 climate factor of 1')
    cases = []
    for amount in [3, 6]:
        input_file = output / f'input-{amount}.json'
        input_file.write_text(json.dumps(model_input(amount)), encoding='utf-8')
        run = output / f'run-{amount}'
        subprocess.run([str(engine), '--input', str(input_file), '--static-cache', str(cache), '--output-dir', str(run)],
                       check=True, capture_output=True, text=True, timeout=120, cwd=output, env=runtime_environment())
        result = json.loads((run / 'result.json').read_text(encoding='utf-8'))
        expected = amount * (10 / 2 + 4 / 2)
        if len(result['g']) != 1 or len(result['h']) != 1 or not all(math.isclose(v[0], expected, rel_tol=1e-12, abs_tol=1e-12) for v in [result['g'], result['h']]):
            raise ValueError('Native result disagrees with the independent mass/factor oracle')
        if not 0 <= result['maxResidual'] <= 1e-12 or result['completeLciaScores'] is not True:
            raise ValueError('Native result is incomplete or has an excessive residual')
        cases.append({'functionalAmount': amount, 'expectedInventoryAndImpact': expected, 'actualImpact': result['h'][0]})
    receipt = {'status': 'passed', 'platform': native_host(), 'input': 'synthetic two-process model with explicit supplier',
               'method': METHOD, 'cases': cases, 'developerLibraryEnvironmentRemoved': True}
    (output / 'qualification.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', type=Path, required=True)
    parser.add_argument('--static-cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(args.engine.resolve(strict=True), args.static_cache.resolve(strict=True), args.output.absolute())))
