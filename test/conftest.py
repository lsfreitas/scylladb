#
# Copyright (C) 2024-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#
from __future__ import annotations

import asyncio
import itertools
import re
import logging
import sys
from argparse import BooleanOptionalAction
from pathlib import Path
from random import randint
from typing import TYPE_CHECKING

import pytest

from test import ALL_MODES, TEST_RUNNER, TOP_SRC_DIR
from test.pylib.cpp.item import CppTestFunction
from test.pylib.report_plugin import ReportPlugin
from test.pylib.util import get_configured_modes, get_modes_to_run
from test.pylib.suite.base import (
    TestSuite,
    get_testpy_test,
    init_testsuite_globals,
    prepare_dirs,
    start_3rd_party_services,
)

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop
    from collections.abc import Generator

    from test.pylib.cpp.item import CppTestFunction
    from test.pylib.suite.base import Test


logger = logging.getLogger(__name__)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption('--mode', choices=ALL_MODES, action="append", dest="modes",
                     help="Run only tests for given build mode(s)")
    parser.addoption('--tmpdir', action='store', default=str(TOP_SRC_DIR / 'testlog'),
                     help='Path to temporary test data and log files.  The data is further segregated per build mode.')
    parser.addoption('--run_id', action='store', default=None, help='Run id for the test run')
    parser.addoption('--byte-limit', action="store", default=randint(0, 2000), type=int,
                     help="Specific byte limit for failure injection (random by default)")
    parser.addoption("--gather-metrics", action=BooleanOptionalAction, default=False,
                     help='Switch on gathering cgroup metrics')
    parser.addoption('--random-seed', action="store",
                     help="Random number generator seed to be used by boost tests")

    # Following option is to use with bare pytest command.
    #
    # For compatibility with reasons need to run bare pytest with  --test-py-init option
    # to run a test.py-compatible pytest session.
    #
    # TODO: remove this when we'll completely switch to bare pytest runner.
    parser.addoption('--test-py-init', action='store_true', default=False,
                     help='Run pytest session in test.py-compatible mode.  I.e., start all required services, etc.')

    # Options for compatibility with test.py
    parser.addoption('--save-log-on-success', default=False,
                        dest="save_log_on_success", action="store_true",
                        help="Save test log output on success.")
    parser.addoption('--coverage', action='store_true', default=False,
                      help="When running code instrumented with coverage support"
                           "Will route the profiles to `tmpdir`/mode/coverage/`suite` and post process them in order to generate "
                           "lcov file per suite, lcov file per mode, and an lcov file for the entire run, "
                           "The lcov files can eventually be used for generating coverage reports")
    parser.addoption("--coverage-mode", action='append', type=str, dest="coverage_modes",
                        help="Collect and process coverage only for the modes specified. implies: --coverage, default: All built modes")
    parser.addoption("--cluster-pool-size", type=int,
                     help="Set the pool_size for PythonTest and its descendants.  Alternatively environment variable "
                          "CLUSTER_POOL_SIZE can be used to achieve the same")
    parser.addoption("--extra-scylla-cmdline-options", default=[],
                     help="Passing extra scylla cmdline options for all tests.  Options should be space separated:"
                          " '--logger-log-level raft=trace --default-log-level error'")
    parser.addoption('--repeat', action="store", default="1", type=int,
                     help="number of times to repeat test execution")
    parser.addoption('--x-log2-compaction-groups', action="store", default="0", type=int,
                     help="Controls number of compaction groups to be used by Scylla tests. Value of 3 implies 8 groups.")

    # Pass information about Scylla node from test.py to pytest.
    parser.addoption("--scylla-log-filename",
                     help="Path to a log file of a ScyllaDB node (for suites with type: Python)")


@pytest.fixture(scope="session")
def build_mode(request: pytest.FixtureRequest) -> str:
    """
    This fixture returns current build mode.
    This is for running tests through the test.py script, where only one mode is passed to the test
    """
    # to avoid issues when there's no provided mode parameter, do it in two steps: get the parameter and if it's not
    # None, get the first value from the list
    mode = request.config.getoption("modes")
    if mode:
        return mode[0]
    return "unknown"

@pytest.fixture(scope="function")
def get_params(request: pytest.FixtureRequest) -> Generator[None]:
    # this dummy fixture only needed to modify the test name with run id and mode. We don't want to parametrize with
    # some parameters, so we are returning existing params that function is accepting. This method only needed for
    # pytest_generate_tests method
    return request.param


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if TEST_RUNNER == "runpy":
        return
    repeat_count = metafunc.config.getoption("--repeat")
    modes = get_modes_to_run(metafunc.config)

    all_combinations = [*itertools.product(modes, range(repeat_count))]

    metafunc.fixturenames.append('get_params')
    metafunc.parametrize(
        'get_params',
        range(len(all_combinations)),
        ids=[f"%{mode}.{run_id + 1}%" for mode, run_id in all_combinations],
        indirect=True
    )

@pytest.fixture(autouse=True)
def print_scylla_log_filename(request: pytest.FixtureRequest) -> Generator[None]:
    """Print out a path to a ScyllaDB log.

    This is a fixture for Python test suites, because they are using a single node clusters created inside test.py,
    but it is handy to have this information printed to a pytest log.
    """

    yield

    if scylla_log_filename := request.config.getoption("--scylla-log-filename"):
        logger.info("ScyllaDB log file: %s", scylla_log_filename)


def testpy_test_fixture_scope(fixture_name: str, config: pytest.Config) -> str:
    """Dynamic scope for fixtures which rely on a current test.py suite/test.

    test.py runs tests file-by-file as separate pytest sessions, so, `session` scope is effectively close to be the
    same as `module` (can be a difference in the order.)  In case of running tests with bare pytest command, we
    need to use `module` scope to maintain same behavior as test.py, since we run all tests in one pytest session.
    """
    if config.getoption("--test-py-init"):
        return "module"
    return "session"

testpy_test_fixture_scope.__test__ = False


@pytest.fixture(scope=testpy_test_fixture_scope)
async def testpy_test(request: pytest.FixtureRequest, build_mode: str) -> Test | None:
    """Create an instance of Test class for the current test.py test."""

    if request.scope == "module":
        return await get_testpy_test(path=request.path, options=request.config.option, mode=build_mode)
    return None


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(ReportPlugin())


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item | CppTestFunction]) -> None:
    """
    This is a standard pytest method.
    This is needed to modify the test names with dev mode and run id to differ them one from another
    """
    testpy_run_id = config.getoption('run_id', None)

    def modify_test_name(s: str, testpy_run_id) -> str:
        """
        Modify the test name to extract run id from parameterized test name to the end of the name.
        Convert names like:
        cluster/test_multidc.py::test_multidc[%dev.1%]
        cluster/test_multidc.py::test_putget_2dc_with_rf[%release.1%-nodes_list0-1]
        to:
        cluster/test_multidc.py::test_multidc.dev.1
        cluster/test_multidc.py::test_putget_2dc_with_rf[nodes_list0-1].release.1
        """
        match = re.search(r'\[%([a-zA-Z_][\w]*)\.(\d+)%(-[^]]+)?]', s)
        if not match:
            return s

        mode = match.group(1)
        run_id = match.group(2)
        suffix = match.group(3)
        if suffix:
            s = re.sub(r'\[%[a-zA-Z_][\w]*\.\d+%(-[^]]+)]', f'[{suffix[1:]}]', s)
        else:
            s = re.sub(r'\[%[a-zA-Z_][\w]*\.\d+%]', '', s)
        return f"{s}.{mode}.{testpy_run_id}" if testpy_run_id else f"{s}.{mode}.{run_id}"

    for item in items:
        if not isinstance(item, CppTestFunction):
            # pytest_generate_tests is not triggered for C++ tests, so they have their own logic for test name
            # modification that handled in CppTestFunction class.
            item._nodeid = modify_test_name(item._nodeid, testpy_run_id)
            item.name = modify_test_name(item.name, testpy_run_id)


def pytest_sessionstart(session: pytest.Session) -> None:
    # test.py starts S3 mock and create/cleanup testlog by itself. Also, if we run with --collect-only option,
    # we don't need this stuff.
    if TEST_RUNNER != "pytest" or session.config.getoption("--collect-only"):
        return

    if not session.config.getoption("--test-py-init"):
        return

    init_testsuite_globals()
    TestSuite.artifacts.add_exit_artifact(None, TestSuite.hosts.cleanup)

    # Run stuff just once for the pytest session even running under xdist.
    if "xdist" not in sys.modules or not sys.modules["xdist"].is_xdist_worker(request_or_session=session):
        temp_dir = Path(session.config.getoption("--tmpdir")).absolute()
        prepare_dirs(tempdir_base=temp_dir, modes=session.config.getoption("--mode") or get_configured_modes(), gather_metrics=session.config.getoption("--gather-metrics"))
        start_3rd_party_services(tempdir_base=temp_dir, toxiproxy_byte_limit=session.config.getoption('byte_limit'))


def pytest_sessionfinish() -> None:
    if getattr(TestSuite, "artifacts", None) is not None:
        asyncio.get_event_loop().run_until_complete(TestSuite.artifacts.cleanup_before_exit())
