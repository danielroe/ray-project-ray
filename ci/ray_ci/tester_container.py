import json
import platform
import shutil
import subprocess
import tempfile
from typing import List, Tuple, Optional
import shutil
from typing import Dict, List, Optional
from os import path, listdir

from ci.ray_ci.utils import shard_tests, chunk_into_n
from ci.ray_ci.utils import logger
from ci.ray_ci.container import Container
from ci.ray_ci.data.test_result import TestResult

BAZEL_EVENT_LOGS = "/tmp/bazel_event_logs"


class TesterContainer(Container):
    """
    A wrapper for running tests in ray ci docker container
    """

    def __init__(
        self,
        bazel_log_dir: str,
        shard_count: int = 1,
        gpus: int = 0,
        bazel_log_dir: str = "/tmp",
        network: Optional[str] = None,
        test_envs: Optional[List[str]] = None,
        shard_ids: Optional[List[int]] = None,
        skip_ray_installation: bool = False,
        build_type: Optional[str] = None,
    ) -> None:
        """
        :param gpu: Number of gpus to use in the container. If 0, used all gpus.
        :param shard_count: The number of shards to split the tests into. This can be
        used to run tests in a distributed fashion.
        :param shard_ids: The list of shard ids to run. If none, run no shards.
        """
        self.bazel_log_dir = bazel_log_dir
        self.shard_count = shard_count
        self.shard_ids = shard_ids or []
        self.test_envs = test_envs or []
        self.build_type = build_type
        self.network = network
        self._init_bazel_log_dir()
        self.gpus = gpus
        assert (
            self.gpus == 0 or self.gpus >= self.shard_count
        ), f"Not enough gpus ({self.gpus} provided) for {self.shard_count} shards"

        self._init_bazel_log_dir()
        if not skip_ray_installation:
            self.install_ray(build_type)

    def _get_bazel_log_mount_dir(self) -> Tuple[str, str]:
        """
        Create a temporary directory in the current container to store bazel event logs
        produced by the test runs. We do this by using the artifact mount directory from
        the host machine as a shared directory between all containers.
        """
        artifact_host, artifact_container = self.get_artifact_mount()
        bazel_log_dir_container = tempfile.mkdtemp(dir=artifact_container)
        bazel_log_dir_host = bazel_log_dir_container.replace(
            artifact_container, artifact_host
        )
        return (bazel_log_dir_host, bazel_log_dir_container)

    def run_tests(
        self,
        test_targets: List[str],
        test_arg: Optional[str] = None,
    ) -> bool:
        """
        Run tests parallelly in docker.  Return whether all tests pass.
        """
        # shard tests and remove empty chunks
        chunks = list(
            filter(
                len,
                [
                    shard_tests(test_targets, self.shard_count, i)
                    for i in self.shard_ids
                ],
            )
        )
        if not chunks:
            # no tests to run
            return True

        # divide gpus evenly among chunks
        gpu_ids = chunk_into_n(list(range(self.gpus)), len(chunks))
        (bazel_log_dir_host, bazel_log_dir_container) = self._get_bazel_log_mount_dir()
        runs = [
            self._run_tests_in_docker(
                chunks[i], gpu_ids[i], bazel_log_dir_host, self.test_envs, test_arg
            )
            for i in range(len(chunks))
        ]
        exits = [run.wait() for run in runs]
        self.persist_test_results(bazel_log_dir_container)

        return all(exit == 0 for exit in exits)

    def persist_test_results(self, bazel_log_dir: str) -> None:
        logger.info("Uploading test results")
        self._upload_build_info(bazel_log_dir)

        # clean up so we don't pollute the host machine
        shutil.rmtree(bazel_log_dir)

    def _upload_build_info(self, bazel_log_dir) -> None:
        subprocess.check_call(
            [
                "bash",
                "ci/build/upload_build_info.sh",
                bazel_log_dir,
            ]
        )

    def _upload_test_results(self) -> None:
        for event in self._get_test_result_events():
            TestResult.from_bazel_event(event).upload()

    def _get_test_result_events(self) -> List[Dict[str, any]]:
        bazel_logs = []
        # Find all bazel logs
        for file in listdir(self.bazel_log_dir):
            log = path.join(self.bazel_log_dir, file)
            if path.isfile(log) and file.startswith("bazel_log"):
                bazel_logs.append(log)

        result_events = []
        # Parse bazel logs and print test results
        for file in bazel_logs:
            with open(file, "rb") as f:
                for line in f.readlines():
                    data = json.loads(line.decode("utf-8"))
                    if "testResult" in data:
                        result_events.append(data)

        return result_events

    def _upload_test_results(self) -> None:
        for event in self._get_test_result_events():
            TestResult.from_bazel_event(event).upload()

    def _get_test_result_events(self) -> List[Dict[str, any]]:
        bazel_logs = []
        # Find all bazel logs
        for file in listdir(self.bazel_log_dir):
            log = path.join(self.bazel_log_dir, file)
            if path.isfile(log) and file.startswith("bazel_log"):
                bazel_logs.append(log)

        result_events = []
        # Parse bazel logs and print test results
        for file in bazel_logs:
            with open(file, "rb") as f:
                for line in f.readlines():
                    data = json.loads(line.decode("utf-8"))
                    if "testResult" in data:
                        result_events.append(data)

        return result_events

    def _run_tests_in_docker(
        self,
        test_targets: List[str],
        gpu_ids: List[int],
        bazel_log_dir_host: str,
        test_envs: List[str],
        test_arg: Optional[str] = None,
    ) -> subprocess.Popen:
        logger.info("Running tests: %s", test_targets)
        commands = [
            f'cleanup() {{ chmod -R a+r "{self.bazel_log_dir}"; }}',
            "trap cleanup EXIT",
        ]
        if platform.system() == "Windows":
            # allow window tests to access aws services
            commands.append(
                "powershell ci/pipeline/fix-windows-container-networking.ps1"
            )
        if self.build_type == "ubsan":
            # clang currently runs into problems with ubsan builds, this will revert to
            # using GCC instead.
            commands.append("unset CC CXX")
        # note that we run tests serially within each docker, since we already use
        # multiple dockers to shard tests
        test_cmd = "bazel test --jobs=1 --config=ci $(./ci/run/bazel_export_options) "
        if self.build_type == "debug":
            test_cmd += "--config=ci-debug "
        if self.build_type == "asan":
            test_cmd += "--config=asan --config=asan-buildkite "
        if self.build_type == "clang":
            test_cmd += "--config=llvm "
        if self.build_type == "asan-clang":
            test_cmd += "--config=asan-clang "
        if self.build_type == "ubsan":
            test_cmd += "--config=ubsan "
        if self.build_type == "tsan-clang":
            test_cmd += "--config=tsan-clang "
        for env in test_envs:
            test_cmd += f"--test_env {env} "
        if test_arg:
            test_cmd += f"--test_arg {test_arg} "
        test_cmd += f"{' '.join(test_targets)}"
        commands.append(test_cmd)
        return subprocess.Popen(
            self.get_run_command(
                commands,
                network=self.network,
                gpu_ids=gpu_ids,
                volumes=[f"{bazel_log_dir_host}:{self.bazel_log_dir}"],
            )
        )
