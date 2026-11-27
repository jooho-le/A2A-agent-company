"""Borrowed async test fixtures must restore resources in their owner's runner."""
import importlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import pytest

TESTS = str(Path(__file__).resolve().parents[1])
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)


@pytest.mark.parametrize('module_name,class_name,directory_path', [
    ('test_owned_observation_pipeline', 'OwnedObservationPipelineTests', 'base.directory'),
    ('test_single_agent_pipeline', 'SingleAgentPipelineTests', 'f.directory'),
    ('test_single_agent_evaluation_export', 'SingleEvaluationExportTests', 'f.directory'),
    ('test_single_agent_mcp', 'SingleMCPFileTests', 'fixture.directory'),
])
def test_borrowed_fixture_setup_restores_environment_and_removes_directory(module_name, class_name, directory_path):
    base = getattr(importlib.import_module(module_name), class_name)
    directories = []

    async def run_test(self):
        value = self
        for part in directory_path.split('.'):
            value = getattr(value, part)
        assert value.is_dir()
        directories.append(value)

    probe = type('CleanupProbe', (base,), {'runTest': run_test})
    _run_probe(probe, directories)


@pytest.mark.parametrize('single', [False, True])
def test_semantic_review_borrowed_fixture_restores_nested_resources(single):
    base = importlib.import_module('test_product_semantic_review').ProductSemanticReviewTests
    directories = []

    async def run_test(self):
        fixture = self.fixture(single=single)
        directories.append(fixture.f.directory if single else fixture.directory)

    probe = type('CleanupProbe', (base,), {'runTest': run_test})
    _run_probe(probe, directories)


def test_single_fix_borrowed_fixture_restores_nested_resources():
    harness = importlib.import_module('test_single_agent_fix_pipeline')._SingleFixHarness
    directories = []

    async def run_test(self):
        directories.append(harness(self).f.directory)

    probe = type('CleanupProbe', (unittest.IsolatedAsyncioTestCase,), {'runTest': run_test})
    _run_probe(probe, directories)


def _run_probe(probe, directories):
    sentinel = 'CAPSTONE_TEST_ASYNC_FIXTURE_CLEANUP_SENTINEL'
    # Restore the surrounding environment even if the regression reappears.
    with patch.dict(os.environ, {sentinel: 'inert-cleanup-probe'}):
        result = unittest.TestResult()
        probe().run(result)
        assert result.wasSuccessful(), 'fixture setup or cleanup failed'
        restored_sentinel = os.environ.get(sentinel)
        assert restored_sentinel == 'inert-cleanup-probe', 'fixture leaked its cleared environment'
        assert directories and all(not directory.exists() for directory in directories), 'fixture directory leaked'
