"""Offline launcher regression tests; dummy values never contact services."""

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

import runtime


class RuntimeTests(unittest.TestCase):
    """Check credential handling, safe defaults and state boundaries."""

    def setUp(self):
        self.values = {
            'LLM_API_KEY': 'dummy-credential-for-offline-tests',
            'LLM_BASE_URL': 'https://llm.example.invalid/v1',
            'LLM_MODEL_ID': 'offline-model'}

    def test_only_references_in_config(self):
        config = runtime.configuration(self.values)
        encoded = json.dumps(config)
        for value in self.values.values():
            self.assertNotIn(value, encoded)
        self.assertEqual(config['model'], 'workspace/configured')
        self.assertEqual(config['share'], 'disabled')
        self.assertEqual(config['permission'], {'*': 'deny'})
        self.assertEqual(config['mcp'], {})

    def test_missing_llm_values_refused(self):
        for key in runtime.LLM_KEYS:
            values = dict(self.values)
            del values[key]
            with self.assertRaises(ValueError):
                runtime.configuration(values)

    def test_interpolation_and_control_characters_refused(self):
        for value in ('bad\nvalue', 'bad\"value', 'bad\\value',
                      '{file:/etc/passwd}', '{env:OTHER_KEY}', ''):
            with self.assertRaises(ValueError):
                runtime.safe_value(value)

    def test_unsafe_urls_refused(self):
        for url in ('http://service.example.invalid/mcp',
                    'https://user:password@example.invalid',
                    'https://example.invalid/mcp?token=hidden',
                    'https://example.invalid/#hidden', 'file:///etc/passwd'):
            with self.assertRaises(ValueError):
                runtime.safe_url(url)
        self.assertEqual(runtime.safe_url('http://127.0.0.1:9000/v1'),
                         'http://127.0.0.1:9000/v1')

    def test_mcp_requires_explicit_enable(self):
        self.values.update({'GITHUB_MCP_URL': 'https://mcp.example.invalid/mcp',
                            'GITHUB_MCP_API_KEY': 'dummy-mcp-credential'})
        self.assertEqual(runtime.configuration(self.values)['mcp'], {})
        self.values['GITHUB_MCP_ENABLED'] = '1'
        config = runtime.configuration(self.values)['mcp']['github']
        self.assertFalse(config['oauth'])
        self.assertEqual(config['headers']['Authorization'],
                         'Bearer {env:GITHUB_MCP_API_KEY}')
        self.values['GITHUB_MCP_AUTH_HEADER'] = 'X-API-Key'
        self.values['GITHUB_MCP_AUTH_SCHEME'] = ''
        config = runtime.configuration(self.values)['mcp']['github']
        self.assertEqual(config['headers']['X-API-Key'], '{env:GITHUB_MCP_API_KEY}')

    def test_enabled_mcp_requires_credentials(self):
        self.values['FIRECRAWL_MCP_ENABLED'] = '1'
        with self.assertRaises(ValueError):
            runtime.configuration(self.values)

    def test_all_four_mcp_slots(self):
        for name in runtime.MCP_NAMES:
            self.values.update({f'{name}_MCP_ENABLED': '1',
                                f'{name}_MCP_URL': 'https://mcp.example.invalid/mcp',
                                f'{name}_MCP_API_KEY': 'dummy-mcp-credential'})
        self.assertEqual(len(runtime.configuration(self.values)['mcp']), 4)

    def test_environment_allowlist(self):
        self.values.update({'CODER_AGENT_TOKEN': 'must-not-inherit',
                            'GIT_AUTHOR_EMAIL': 'private@example.invalid',
                            'OPENCODE_CONFIG_CONTENT': 'must-not-inherit'})
        child = runtime.environment(self.values, Path('/tmp/test-acp'))
        self.assertEqual(child['LLM_API_KEY'], self.values['LLM_API_KEY'])
        for key in ('CODER_AGENT_TOKEN', 'GIT_AUTHOR_EMAIL', 'OPENCODE_CONFIG_CONTENT'):
            self.assertNotIn(key, child)
        self.assertEqual(child['HOME'], '/tmp/test-acp')

    def test_prepare_is_repeatable_and_secret_free(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'runtime'
            runtime.prepare(state)
            runtime.prepare(state)
            config_path = state / '.acpx/config.json'
            config = json.loads(config_path.read_text())
            self.assertEqual(config['defaultAgent'], 'opencode')
            self.assertNotIn('auth', config)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            self.assertEqual([path.name for path in state.rglob('*') if path.is_file()],
                             ['config.json'])

    def test_symlink_state_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            target.mkdir()
            state = Path(directory) / 'runtime'
            state.symlink_to(target, target_is_directory=True)
            with self.assertRaises(ValueError):
                runtime.prepare(state)
            self.assertEqual(list(target.iterdir()), [])

    def test_hardlinked_config_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'runtime'
            runtime.prepare(state)
            config_path = state / '.acpx/config.json'
            backup = Path(directory) / 'linked.json'
            os.link(config_path, backup)
            original = backup.read_bytes()
            with self.assertRaises(ValueError):
                runtime.prepare(state)
            self.assertEqual(backup.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
