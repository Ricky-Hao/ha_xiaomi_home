"""Offline bootstrap regressions; no downloads or real model/tool sessions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bootstrap
import runtime


class BootstrapTests(unittest.TestCase):
    def test_lifecycle_uses_image_and_nonfatal_bootstrap(self):
        config = json.loads((bootstrap.HERE.parent / 'devcontainer.json').read_text())
        self.assertIn('image', config)
        self.assertNotIn('build', config)
        command = config['postStartCommand']
        self.assertIn('/acp/bootstrap.py', command)
        self.assertIn('|| printf', command)
        self.assertNotIn('opencode acp', command)

    def test_clean_install_environment(self):
        with patch.dict(bootstrap.os.environ, {'LLM_API_KEY': 'dummy', 'CODER_AGENT_TOKEN': 'dummy'}):
            env = bootstrap.clean_env()
        self.assertNotIn('LLM_API_KEY', env)
        self.assertNotIn('CODER_AGENT_TOKEN', env)
        self.assertEqual(env['GIT_CONFIG_GLOBAL'], '/dev/null')

    def test_successful_existing_trial_matches_original_patch(self):
        # Test fixed provenance without depending on a locally completed task.
        self.assertEqual(bootstrap.trial.COMMIT, runtime.TOOL_IDENTITY['upstreamCommit'])
        self.assertEqual(bootstrap.trial.PATCH_SHA, runtime.PATCH_SHA256)
        self.assertEqual(bootstrap.trial.digest(bootstrap.HERE / 'source-build/catalog-readiness.patch'),
                         runtime.PATCH_SHA256)

    def test_cached_setup_never_spawns_or_compiles(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(runtime, 'TOOLS', Path(tmp)), \
                patch.object(runtime, 'verify_tools') as verify, \
                patch.object(bootstrap, 'link_commands') as link, \
                patch.object(bootstrap.subprocess, 'Popen') as spawn, \
                patch.object(bootstrap.sys, 'argv', ['bootstrap.py']):
            bootstrap.main()
            verify.assert_called_once_with(binary=True)
            link.assert_called_once()
            spawn.assert_not_called()

    def test_invalid_cache_does_not_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(runtime, 'TOOLS', Path(tmp)), \
                patch.object(runtime, 'verify_tools', side_effect=ValueError('bad cache')), \
                patch.object(bootstrap.subprocess, 'Popen') as spawn, \
                patch.object(bootstrap.sys, 'argv', ['bootstrap.py']):
            with self.assertRaises(ValueError):
                bootstrap.main()
            spawn.assert_not_called()

    def test_existing_job_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(runtime, 'TOOLS', Path(tmp) / 'absent-tools'), \
                patch.object(bootstrap, 'JOB', Path(tmp)), \
                patch.object(bootstrap.subprocess, 'Popen') as spawn, \
                patch.object(bootstrap.sys, 'argv', ['bootstrap.py']):
            bootstrap.main()
            spawn.assert_not_called()

    def test_incomplete_source_trial_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'status.json').write_text(json.dumps({'state': 'failed', 'exit_code': 1}))
            with patch.object(bootstrap.trial, 'ROOT', root):
                with self.assertRaises(ValueError):
                    bootstrap.successful_trial()

    def test_tampered_local_manifest_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'build.json').write_text(json.dumps({'identity': runtime.TOOL_IDENTITY,
                                                       'trialInputHashes': {}}))
            with self.assertRaises(ValueError):
                runtime.verify_tools(root)


if __name__ == '__main__':
    unittest.main()
