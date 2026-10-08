"""Actual Caddy/Tunnel-container checks; fictional accounts and internal networks only."""
import argparse
import json
from pathlib import Path
import subprocess
from life_fleet.store import Fleet
from life_fleet.operations import Operations


def run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, timeout=180, **kwargs)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', required=True)
    p.add_argument('--image', required=True)
    p.add_argument('--caddy-image', required=True)
    p.add_argument('--tunnel-image', required=True)
    args = p.parse_args()
    root = Path(args.root).resolve()
    project = 'life-pilot-ingress-fixture'
    if root.exists() and any(root.iterdir()) and not (root/'FICTIONAL-TEST-ONLY').exists():
        raise ValueError('fixture_directory_required')
    prior = run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project='+project])
    prior.check_returncode()
    if prior.stdout.strip():
        info = run(['docker', 'inspect', *prior.stdout.split()]); info.check_returncode()
        assert all(c['Config']['Labels'].get('com.docker.compose.project.config_files') == str(root/'compose.json') for c in json.loads(info.stdout)), 'fixture_project_collision'
    fleet = Fleet(root); ops = Operations(fleet)
    (root/'FICTIONAL-TEST-ONLY').write_text('No real account or provider access.\n')
    token = root/'tunnel-token'; token.write_text('FICTIONAL_INVALID_TUNNEL_TOKEN'); token.chmod(0o600)
    (root/'operator.json').write_text(json.dumps({'base_url':'https://fixture.invalid/v1','api_key':'fictional-only','model':'fixture-model','asr_model':'fixture-asr',
        'ingress':{'caddy_image':args.caddy_image,'tunnel_image':args.tunnel_image,'token_file':str(token)}}))
    for i in range(5): fleet.create(f'fixture{i}'); fleet.state(f'fixture{i}', 'active')
    spec = fleet.compose(args.image, 'fixture.invalid'); spec['name'] = project
    for network in spec['networks'].values(): network['internal'] = True
    repo = Path(__file__).resolve().parents[1]
    spec['services']['broker']['volumes'].append({'type':'bind','source':str(repo/'tests/container_fixture_broker.py'),'target':'/fixture-broker.py','read_only':True})
    spec['services']['broker']['entrypoint'] = ['python','/fixture-broker.py']
    spec['services']['tunnel']['restart'] = 'no'
    (root/'Caddyfile').write_text(fleet.caddyfile('fixture.invalid'))
    (root/'compose.json').write_text(json.dumps(spec,indent=2))
    report = {'real_accounts_verified':False,'public_network_tested':False,'caddy_image':args.caddy_image,'tunnel_image':args.tunnel_image}
    try:
        result = run(ops.command('up','-d','caddy',*[f'fixture{i}' for i in range(5)]))
        if result.returncode: raise RuntimeError(result.stderr[-3000:])
        for i in range(5):
            name = f'fixture{i}'; ops.health(name)
            code = 'PAYLOAD = '+repr({'name':name,'phase':'initialize'})+'\n'+(repo/'tests/container_probe.py').read_text()
            result = run(ops.command('exec','-T',name,'python','-'), input=code)
            if result.returncode: raise RuntimeError(result.stderr[-2000:])
        # An invalid, non-secret token must be readable and rejected as invalid.
        # With internal-only networking it cannot establish a real tunnel.
        result = run(ops.command('run','--rm','--no-deps','tunnel'))
        error = (result.stdout+result.stderr).lower()
        report['tunnel_token_readable'] = result.returncode != 0 and 'permission denied' not in error and ('invalid' in error or 'not valid' in error)
        if not report['tunnel_token_readable']: report['tunnel_diagnostic'] = error[-1500:]
        (root/'ingress-partial.json').write_text(json.dumps(report,indent=2))
        result = run(['docker','run','--rm','-i','--network',project+'_entry','--read-only','--tmpfs','/tmp:size=32m','--cap-drop','ALL','--entrypoint','python',args.image,'-'], input=(repo/'tests/container_ingress_probe.py').read_text())
        if result.returncode: raise RuntimeError(result.stderr[-3000:])
        report['web_checks'] = json.loads(result.stdout)
        report['status'] = 'passed_local_ingress_scope' if report['tunnel_token_readable'] else 'failed_token_permissions'
        (root/'ingress-check.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report,indent=2))
        assert report['tunnel_token_readable'], 'tunnel cannot use private token file'
    finally:
        for row in fleet.tenants(): fleet.state(row['id'], 'frozen')
        result = run(ops.command('stop','--timeout','120'))
        result.check_returncode()


if __name__ == '__main__': main()
