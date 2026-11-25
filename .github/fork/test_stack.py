#!/usr/bin/env python3
"""Tests for the isolated fork composition and release metadata helpers."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('stack', Path(__file__).with_name('stack.py'))
stack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stack)

pipeline_spec = importlib.util.spec_from_file_location('pipeline', Path(__file__).with_name('pipeline.py'))
pipeline = importlib.util.module_from_spec(pipeline_spec)
pipeline_spec.loader.exec_module(pipeline)


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        stack.git(self.repo, 'init', '-b', 'main')
        stack.git(self.repo, 'config', 'user.name', 'Fixture')
        stack.git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        self.base = self.commit('app.txt', 'upstream\n')
        self.config = {'schema': 1, 'upstream': 'test/upstream', 'fork': 'test/fork', 'patches': []}
        self.report = self.root / 'report.json'

    def commit(self, file, content):
        (self.repo / file).write_text(content)
        stack.git(self.repo, 'add', '--', file)
        stack.git(self.repo, 'commit', '-m', 'test: change fixture')
        return stack.git(self.repo, 'rev-parse', 'HEAD')

    def feature(self, name, file, content):
        stack.git(self.repo, 'switch', '-c', name, self.base)
        head = self.commit(file, content)
        entry = {'branch': name, 'base': self.base, 'head': head, 'pr': None}
        self.config['patches'].append(entry)
        stack.git(self.repo, 'switch', 'main')
        return entry

    def compose(self, name='output', **kwargs):
        return stack.compose(self.repo, self.config, 'main', self.root / name,
                             self.report, fetch=False, **kwargs)

    def test_multiple_features_survive_upstream_and_reproduce_exact_commit(self):
        first = self.feature('dev/first', 'first.txt', 'first\n')
        second = self.feature('dev/second', 'second.txt', 'second\n')
        upstream = self.commit('new-upstream.txt', 'new\n')
        one = self.compose()
        two = self.compose('again')
        self.assertEqual(one['composed_sha'], two['composed_sha'])
        self.assertEqual(one['upstream'], upstream)
        for file in ('first.txt', 'second.txt', 'new-upstream.txt'):
            self.assertTrue((self.root / 'output' / file).exists())
        self.assertEqual(stack.git(self.repo, 'rev-parse', 'dev/first'), first['head'])
        self.assertEqual(stack.git(self.repo, 'rev-parse', 'dev/second'), second['head'])
        self.assertEqual(stack.git(self.repo, 'rev-parse', 'main'), upstream)

    def test_conflict_preserves_last_good_ref_and_cleans_only_new_checkout(self):
        self.feature('fix/chapter', 'app.txt', 'feature\n')
        stack.git(self.repo, 'branch', 'build/local')
        upstream = self.commit('app.txt', 'changed upstream\n')
        with self.assertRaisesRegex(ValueError, 'fix/chapter.*conflicts'):
            self.compose()
        self.assertEqual(stack.git(self.repo, 'rev-parse', 'build/local'), self.base)
        self.assertEqual(stack.git(self.repo, 'rev-parse', 'main'), upstream)
        self.assertFalse((self.root / 'output').exists())
        self.assertFalse(self.report.exists())

    def test_refuses_existing_destination(self):
        target = self.root / 'output'
        target.mkdir()
        (target / 'user-work').write_text('keep me')
        with self.assertRaisesRegex(ValueError, 'Refusing to replace'):
            self.compose()
        self.assertEqual((target / 'user-work').read_text(), 'keep me')

    def test_missing_remote_branch_fails_instead_of_omitting_patch(self):
        self.feature('fix/missing', 'fix.txt', 'fix\n')
        remote = self.root / 'remote'
        stack.run('git', 'init', '--bare', remote, stdout=stack.subprocess.PIPE)
        stack.git(self.repo, 'remote', 'add', 'origin', str(remote))
        with self.assertRaises(stack.subprocess.CalledProcessError):
            stack.compose(self.repo, self.config, 'main', self.root / 'output', self.report)
        self.assertFalse((self.root / 'output').exists())

    def test_upstreamed_patch_is_recorded_as_redundant(self):
        feature = self.feature('fix/same', 'same.txt', 'same\n')
        self.commit('same.txt', 'same\n')
        result = self.compose()
        self.assertEqual(result['already_present'], [feature['head']])
        self.assertEqual(result['applied'], [])

    def test_immutable_pin_survives_feature_branch_advancement(self):
        entry = self.feature('fix/pinned', 'fix.txt', 'activated\n')
        remote = self.root / 'remote'
        stack.run('git', 'init', '--bare', remote, stdout=stack.subprocess.PIPE)
        stack.git(self.repo, 'remote', 'add', 'origin', str(remote))
        stack.git(self.repo, 'push', 'origin', f"{entry['head']}:refs/tags/patches/{entry['head']}")
        stack.git(self.repo, 'switch', 'fix/pinned')
        self.commit('fix.txt', 'unfinished later work\n')
        stack.git(self.repo, 'push', 'origin', 'fix/pinned')
        stack.git(self.repo, 'switch', 'main')
        result = stack.compose(self.repo, self.config, 'main', self.root / 'output', self.report)
        self.assertEqual((self.root / 'output' / 'fix.txt').read_text(), 'activated\n')
        self.assertEqual(result['applied'], [entry['head']])

    def test_removing_one_feature_keeps_the_other(self):
        self.feature('dev/first', 'first.txt', 'first\n')
        self.feature('dev/second', 'second.txt', 'second\n')
        self.config['patches'].pop(0)
        self.compose()
        self.assertFalse((self.root / 'output' / 'first.txt').exists())
        self.assertTrue((self.root / 'output' / 'second.txt').exists())

    def test_rejects_merge_commits(self):
        entry = self.feature('dev/merge', 'first.txt', 'first\n')
        self.commit('upstream.txt', 'new\n')
        stack.git(self.repo, 'switch', 'dev/merge')
        stack.git(self.repo, 'merge', '--no-ff', 'main', '-m', 'test: merge fixture')
        entry['head'] = stack.git(self.repo, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'merge commits are unsupported'):
            self.compose()

    def test_retirement_requires_exact_head_and_merge_in_fetched_base(self):
        entry = self.feature('fix/merged', 'first.txt', 'first\n')
        entry['pr'] = 7
        merged = self.commit('first.txt', 'first\n')
        pr = {'head': {'repo': {'full_name': 'test/fork'}, 'ref': entry['branch'],
                       'sha': entry['head']}, 'merged': True, 'state': 'closed',
              'merge_commit_sha': merged}
        def api(*args, **kwargs):
            return stack.subprocess.CompletedProcess(args, 0, json.dumps(pr))
        original_run = stack.run
        def dispatch(*args, **kwargs):
            return api(*args, **kwargs) if args[0] == 'gh' else original_run(*args, **kwargs)
        with patch.object(stack, 'run', side_effect=dispatch):
            self.assertTrue(stack.merged_upstream(self.repo, self.config, entry, merged))
            self.assertFalse(stack.merged_upstream(self.repo, self.config, entry, self.base))
            pr['head']['sha'] = self.base
            self.assertFalse(stack.merged_upstream(self.repo, self.config, entry, merged))
            pr['merged'] = False
            self.assertFalse(stack.merged_upstream(self.repo, self.config, entry, merged))


class ConfigValidationTests(unittest.TestCase):
    def test_accepts_feature_prefix_and_rejects_unknown_prefixes(self):
        self.assertTrue(stack.BRANCH.fullmatch('feature/add-custom-view'))
        self.assertTrue(stack.BRANCH.fullmatch('dev/fork-t3code'))
        self.assertIsNone(stack.BRANCH.fullmatch('features/add-custom-view'))
        self.assertIsNone(stack.BRANCH.fullmatch('wip/add-custom-view'))

    def test_required_patch_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'stack.json'
            base_cfg = {
                'schema': 1,
                'upstream': 'pingdotgg/t3code',
                'fork': 'alirafiqmalik/t3code',
                'required_patch': 'dev/fork-t3code',
                'patches': [
                    {'branch': 'dev/fork-t3code', 'base': 'a' * 40, 'head': 'b' * 40, 'pr': None}
                ],
            }
            path.write_text(json.dumps(base_cfg))
            self.assertEqual(stack.load(path)['required_patch'], 'dev/fork-t3code')

            bad_pr = json.loads(json.dumps(base_cfg))
            bad_pr['patches'][0]['pr'] = 42
            path.write_text(json.dumps(bad_pr))
            with self.assertRaisesRegex(ValueError, 'auto-retire'):
                stack.load(path)

            missing = json.loads(json.dumps(base_cfg))
            missing['patches'] = []
            path.write_text(json.dumps(missing))
            with self.assertRaisesRegex(ValueError, 'required fork configuration patch is missing'):
                stack.load(path)


class VersioningAndReleaseTests(unittest.TestCase):
    def test_compute_next_fork_version_increments_patch_and_sequence(self):
        self.assertEqual(
            pipeline.compute_next_fork_version('0.0.48', '20261125', []),
            '0.0.49-fork.20261125.1',
        )
        self.assertEqual(
            pipeline.compute_next_fork_version('0.0.48-nightly.20261124.2', '20261125', []),
            '0.0.49-fork.20261125.1',
        )
        self.assertEqual(
            pipeline.compute_next_fork_version(
                '0.0.48',
                '20261125',
                ['v0.0.49-fork.20261124.9', 'v0.0.49-fork.20261125.2'],
            ),
            '0.0.49-fork.20261125.3',
        )

    def test_fork_tag_sort_key_orders_numerically(self):
        tags = ['v0.0.49-fork.20261125.10', 'v0.0.49-fork.20261125.2', 'v0.0.49-fork.20261125.1']
        self.assertEqual(
            max(tags, key=pipeline.fork_tag_sort_key),
            'v0.0.49-fork.20261125.10',
        )
        self.assertIsNone(pipeline.fork_tag_sort_key('v0.0.49'))

    def check_metadata(self, force, build_desktop=False, prev_desktop=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            composed = root / 'composed'
            (composed / 'apps' / 'desktop').mkdir(parents=True)
            (composed / 'apps' / 'desktop' / 'package.json').write_text(
                json.dumps({'name': '@t3tools/desktop', 'version': '0.0.48'})
            )
            report = root / 'manifest.json'
            report.write_text(json.dumps({
                'fingerprint': 'source',
                'upstream': 'base',
                'composed_sha': 'composed',
                'patches': [],
                'retired': [],
            }))
            fingerprint = pipeline.hashlib.sha256(b'sourcecontroller-files').hexdigest()

            def output(*args):
                if 'rev-parse' in args:
                    return 'controller-commit'
                if 'ls-tree' in args:
                    return 'controller-files'
                if 'ls-remote' in args and 'refs/heads/build/local' in args:
                    return 'previous refs/heads/build/local'
                if 'ls-remote' in args and '--tags' in args:
                    return 'aaa refs/tags/v0.0.49-fork.20261125.10'
                return json.dumps([[{'draft': False, 'tag_name': 'v0.0.49-fork.20261125.10'}]])

            def download(args, **kwargs):
                destination = Path(args[args.index('--dir') + 1])
                (destination / 'build-manifest.json').write_text(
                    json.dumps({'build_fingerprint': fingerprint, 'build_desktop': prev_desktop})
                )

            environment = {
                'GITHUB_REPOSITORY': 'alirafiqmalik/t3code',
                'GITHUB_RUN_ID': '123',
                'GITHUB_OUTPUT': str(root / 'output'),
                'GITHUB_STEP_SUMMARY': str(root / 'summary'),
            }
            fixed_now = pipeline.datetime(2026, 11, 25, 12, 0, tzinfo=pipeline.timezone.utc)
            with patch.object(pipeline, 'output', side_effect=output), \
                    patch.object(pipeline.subprocess, 'run', side_effect=download), \
                    patch.object(pipeline, 'datetime') as mock_dt, \
                    patch.dict(pipeline.os.environ, environment):
                mock_dt.now.return_value = fixed_now
                pipeline.prepare(report, root, composed, force, build_desktop)
            return (root / 'output').read_text(), json.loads(report.read_text())

    def test_unchanged_source_and_controller_skip_compilation(self):
        outputs, report = self.check_metadata(force=False, build_desktop=False, prev_desktop=False)
        self.assertIn('should_build=false', outputs)
        self.assertEqual(report['previous_build_sha'], 'previous')

    def test_manual_build_increases_version_after_existing_fork_tag(self):
        outputs, report = self.check_metadata(force=True, build_desktop=False, prev_desktop=False)
        self.assertIn('should_build=true', outputs)
        self.assertEqual(report['version'], '0.0.49-fork.20261125.11')
        self.assertEqual(report['tag'], 'v0.0.49-fork.20261125.11')

    def test_requesting_desktop_rebuilds_when_previous_lacked_desktop(self):
        outputs, _report = self.check_metadata(force=False, build_desktop=True, prev_desktop=False)
        self.assertIn('should_build=true', outputs)


if __name__ == '__main__':
    unittest.main()
