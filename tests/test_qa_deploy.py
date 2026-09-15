"""Release authorization and recovery without touching launchd or live models."""
import copy
import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('qa_deploy', Path(__file__).parents[1] / 'scripts/qa_deploy.py')
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)
SHA = 'a' * 40


def request():
    deployment = {'id': 9, 'sha': SHA, 'creator': {'login': qa.BOT}, 'environment': 'production',
                  'payload': {'sha': SHA, 'requested_by': 'pr-qa-agent', 'pull_request': 150}}
    pr = {'number': 150, 'merged': True, 'merge_commit_sha': SHA,
          'base': {'ref': 'main', 'repo': {'full_name': qa.REPOSITORY}}}
    branch = {'commit': {'sha': SHA}}
    checks = {'check_runs': [{'name': 'verify', 'head_sha': SHA, 'app': {'slug': 'github-actions'},
                              'status': 'completed', 'conclusion': 'success'}]}
    return deployment, pr, branch, checks


def test_merged_exact_main_sha_requires_green_ci():
    args = request()
    assert qa.validate_request(*args)
    args[3]['check_runs'][0]['status'] = 'in_progress'
    assert not qa.validate_request(*args)
    args[3]['check_runs'][0].update(status='completed', conclusion='failure')
    with pytest.raises(qa.Refused):
        qa.validate_request(*args)


@pytest.mark.parametrize('part,key,value', [
    (0, 'creator', {'login': 'someone-else'}), (0, 'environment', 'staging'),
    (0, 'sha', 'main'), (0, 'payload', {}), (1, 'merged', False),
    (1, 'merge_commit_sha', 'b' * 40), (1, 'number', 151),
    (1, 'base', {'ref': 'other'}), (2, 'commit', {'sha': 'b' * 40}),
])
def test_invalid_release_is_refused(part, key, value):
    args = request()
    args[part][key] = value
    with pytest.raises(qa.Refused):
        qa.validate_request(*args)


def test_wrong_ci_app_cannot_authorize():
    args = request()
    args[3]['check_runs'][0]['app']['slug'] = 'untrusted'
    assert not qa.validate_request(*args)


def test_direct_connections_strip_all_proxy_spellings(monkeypatch):
    monkeypatch.setenv('https_proxy', 'http://localhost:8084')
    monkeypatch.setenv('ALL_PROXY', 'http://localhost:8084')
    assert 'https_proxy' not in qa.direct_environment()
    assert 'ALL_PROXY' not in qa.direct_environment()


class Fake(qa.Controller):
    def __init__(self, tmp_path):
        super().__init__(tmp_path, tmp_path / 'router.plist', tmp_path, tmp_path / 'log')
        self.events = []
        self.current = SHA
        self.healthy = True
    def git(self, *args):
        return self.current
    def matches(self, sha):
        return self.healthy and self.current == sha
    def rollback(self, record):
        self.events.append('rollback')
        self.current = record['previous_sha']
    def status(self, deployment_id, state, description):
        self.events.append(state)


@pytest.mark.parametrize('healthy,expected', [(True, ['success']), (False, ['rollback', 'failure'])])
def test_interrupted_deploy_recovers_before_another_release(tmp_path, healthy, expected):
    controller = Fake(tmp_path)
    controller.healthy = healthy
    qa.atomic_json(controller.journal, {'id': 9, 'sha': SHA, 'previous_sha': 'b' * 40, 'phase': 'applying'})
    assert controller.recover()
    assert controller.events == expected
    assert not controller.recover()
    assert controller.events == expected


def test_uncertain_status_receipt_is_retried_without_redeploy(tmp_path):
    controller = Fake(tmp_path)
    qa.atomic_json(controller.journal, {'id': 9, 'phase': 'terminal', 'outcome': 'success', 'message': 'healthy'})
    assert not controller.recover()
    assert controller.events == ['success']


@pytest.mark.parametrize('failed', [False, True])
def test_deploy_checks_new_process_and_restores_on_failure(tmp_path, monkeypatch, failed):
    import plistlib
    controller = Fake(tmp_path)
    controller.current = 'b' * 40
    controller.plist.write_bytes(plistlib.dumps({'EnvironmentVariables': {}}))
    controller.preflight = lambda: {'EnvironmentVariables': {}}
    controller.quiet = lambda: controller.events.append('quiet')
    controller.restart = lambda: controller.events.append('restart')
    def git(*args):
        if args == ('rev-parse', 'origin/main'):
            return SHA
        if args == ('rev-parse', 'HEAD'):
            return controller.current
        if args[:2] == ('checkout', '--detach'):
            controller.current = args[2]
        return ''
    controller.git = git
    def command(*args, **kwargs):
        if args[0] == 'curl':
            return '{"models":[{"name":"qwen3.5:4b-64k"},{"name":"qwen3.5:4b-256k"}]}'
        controller.events.append(args[0])
        return ''
    monkeypatch.setattr(qa, 'run', command)
    def verify(sha):
        controller.events.append('verify')
        if failed:
            raise qa.Refused('wrong process')
    controller.wait_for_release = verify
    controller.deploy(request()[0])
    assert controller.events[:4] == ['quiet', 'in_progress', 'uv', 'restart']
    assert controller.events[4:] == (['verify', 'rollback', 'failure'] if failed else ['verify', 'success'])
    assert controller.current == ('b' * 40 if failed else SHA)
