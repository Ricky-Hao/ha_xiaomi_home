"""Offline checks for the project-only devcontainer contract."""
import json
from pathlib import Path
import re
import shlex
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent


class ProjectEnvironmentTests(unittest.TestCase):
    """Keep dependency setup aligned with CI and reject legacy tool hooks."""

    def test_only_project_create_hook(self):
        """Creation installs CI test dependencies, without recurring setup."""
        config = json.loads((ROOT / 'devcontainer.json').read_text())
        hooks = {name for name in config if name.endswith('Command')}
        self.assertEqual(hooks, {'postCreateCommand'})
        commands = [shlex.split(command) for command in
                    config['postCreateCommand'].split('&&')]
        self.assertEqual(commands, [
            ['python', '-m', 'pip', 'install', '-r',
             '.devcontainer/requirements-dev.txt'],
            ['python', '-m', 'pip', 'install', '--no-deps',
             'homeassistant==2024.4.4']])
        self.assertNotIn('build', config)
        self.assertNotIn('features', config)

    def test_requirements_match_ci_and_separate_validator_fixture(self):
        """Avoid drifting into full service dependencies or unrelated packages."""
        requirements = [line.strip() for line in
                        (ROOT / 'requirements-dev.txt').read_text().splitlines()
                        if line.strip() and not line.lstrip().startswith('#')]
        workflow = (PROJECT / '.github/workflows/test.yaml').read_text()
        match = re.search(r'^\s+pip install (pytest .+)$', workflow, re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(requirements, shlex.split(match.group(1)))
        self.assertIn('pip install --no-deps homeassistant==2024.4.4', workflow)
        self.assertTrue(all(re.fullmatch(r'[A-Za-z0-9_.-]+', item)
                            for item in requirements))

    def test_no_legacy_payload_or_runtime_references(self):
        """The devcontainer has only project setup, documentation and tests."""
        files = {path.relative_to(ROOT).as_posix() for path in ROOT.rglob('*')
                 if path.is_file() and '__pycache__' not in path.parts}
        self.assertEqual(files, {
            '.dockerignore', 'devcontainer.json', 'README.md',
            'requirements-dev.txt', 'tests/test_project_environment.py'})
        for name in ('devcontainer.json', 'README.md', 'requirements-dev.txt'):
            self.assertNotRegex((ROOT / name).read_text().lower(),
                                r'opencode|acpx|bootstrap\.py|runtime\.py|'
                                r'catalog-readiness|/acp/|llm_api_key|_mcp_')


if __name__ == '__main__':
    unittest.main()
