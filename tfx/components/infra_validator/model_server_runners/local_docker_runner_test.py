# Copyright 2020 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for tfx.components.infra_validator.model_server_runners.local_docker_runner."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os

from docker import errors as docker_errors
import mock
import tensorflow as tf
from typing import Any, Dict, Text

from google.protobuf import json_format
from tfx.components.infra_validator import binary_kinds
from tfx.components.infra_validator import error_types
from tfx.components.infra_validator.model_server_runners import local_docker_runner
from tfx.proto import infra_validator_pb2
from tfx.types import standard_artifacts
from tfx.utils import time_utils


def _create_serving_spec(payload: Dict[Text, Any]):
  result = infra_validator_pb2.ServingSpec()
  json_format.ParseDict(payload, result)
  return result


class LocalDockerRunnerTest(tf.test.TestCase):

  def setUp(self):
    super(LocalDockerRunnerTest, self).setUp()

    base_dir = os.path.join(
        os.path.dirname(  # components/
            os.path.dirname(  # infra_validator/
                os.path.dirname(__file__))),  # model_server_runners/
        'testdata'
    )
    self._model = standard_artifacts.Model()
    self._model.uri = os.path.join(base_dir, 'trainer', 'current')
    self._model_name = 'chicago-taxi'

    # Mock _find_available_port
    patcher = mock.patch.object(local_docker_runner, '_find_available_port')
    patcher.start().return_value = 1234
    self.addCleanup(patcher.stop)

    # Mock docker.DockerClient
    patcher = mock.patch('docker.DockerClient')
    self._docker_client = patcher.start().return_value
    self.addCleanup(patcher.stop)

    self._serving_spec = _create_serving_spec({
        'tensorflow_serving': {
            'model_name': self._model_name,
            'tags': ['1.15.0']},
        'local_docker': {}
    })
    self._binary_kind = binary_kinds.parse_binary_kinds(self._serving_spec)[0]
    patcher = mock.patch.object(self._binary_kind, 'MakeClient')
    self._model_server_client = patcher.start().return_value
    self.addCleanup(patcher.stop)

  def _CreateLocalDockerRunner(self):
    return local_docker_runner.LocalDockerRunner(
        model=self._model,
        binary_kind=self._binary_kind,
        serving_spec=self._serving_spec)

  def testStart(self):
    # Prepare mocks and variables.
    runner = self._CreateLocalDockerRunner()

    # Act.
    runner.Start()

    # Check calls.
    self._docker_client.containers.run.assert_called()
    _, run_kwargs = self._docker_client.containers.run.call_args
    self.assertDictContainsSubset(dict(
        image='tensorflow/serving:1.15.0',
        ports={'8500/tcp': 1234},
        environment={
            'MODEL_NAME': 'chicago-taxi',
            'MODEL_BASE_PATH': '/model'
        },
        auto_remove=True,
        detach=True
    ), run_kwargs)

  def testStartMultipleTimesFail(self):
    # Prepare mocks and variables.
    runner = self._CreateLocalDockerRunner()

    # Act.
    runner.Start()
    with self.assertRaises(error_types.IllegalState) as err:
      runner.Start()

    # Check errors.
    self.assertEqual(
        str(err.exception), 'You cannot start model server multiple times.')

  def testGetEndpoint_AfterStart(self):
    # Prepare mocks and variables.
    runner = self._CreateLocalDockerRunner()

    # Act.
    runner.Start()
    endpoint = runner.GetEndpoint()

    # Check result.
    self.assertEqual(endpoint, 'localhost:1234')

  def testGetEndpoint_FailWithoutStartingFirst(self):
    # Prepare mocks and variables.
    runner = self._CreateLocalDockerRunner()

    # Act.
    with self.assertRaises(error_types.IllegalState):
      runner.GetEndpoint()

  @mock.patch.object(time_utils, 'utc_timestamp')
  def testWaitUntilRunning(self, timestamp_mock):
    # Prepare mocks and variables.
    container = self._docker_client.containers.run.return_value
    runner = self._CreateLocalDockerRunner()
    timestamp_mock.side_effect = list(range(10))

    # Setup state.
    runner.Start()
    container.status = 'running'

    # Act.
    try:
      runner.WaitUntilRunning(deadline=10)
    except Exception as e:  # pylint: disable=broad-except
      self.fail(e)

    # Check states.
    container.reload.assert_called()

  @mock.patch.object(time_utils, 'utc_timestamp')
  def testWaitUntilRunning_FailWithoutStartingFirst(self, timestamp_mock):
    # Prepare runner.
    runner = self._CreateLocalDockerRunner()
    timestamp_mock.side_effect = list(range(10))

    # Act.
    with self.assertRaises(error_types.IllegalState) as err:
      runner.WaitUntilRunning(deadline=10)

    # Check errors.
    self.assertEqual(str(err.exception), 'container is not started.')

  @mock.patch.object(time_utils, 'utc_timestamp')
  def testWaitUntilRunning_FailWhenBadContainerStatus(self, timestamp_mock):
    # Prepare mocks and variables.
    container = self._docker_client.containers.run.return_value
    runner = self._CreateLocalDockerRunner()
    timestamp_mock.side_effect = list(range(10))

    # Setup state.
    runner.Start()
    container.status = 'dead'  # Bad status.

    # Act.
    with self.assertRaises(error_types.JobAborted):
      runner.WaitUntilRunning(deadline=10)

  @mock.patch.object(time_utils, 'utc_timestamp')
  @mock.patch('time.sleep')
  def testWaitUntilRunning_FailIfNotRunningUntilDeadline(
      self, mock_sleep, mock_timestamp):
    # Prepare mocks and variables.
    container = self._docker_client.containers.run.return_value
    runner = self._CreateLocalDockerRunner()
    mock_timestamp.side_effect = list(range(20))

    # Setup state.
    runner.Start()
    container.status = 'created'

    # Act.
    with self.assertRaises(error_types.DeadlineExceeded):
      runner.WaitUntilRunning(deadline=10)

    # Check result.
    self.assertEqual(mock_sleep.call_count, 10)

  @mock.patch.object(time_utils, 'utc_timestamp')
  def testWaitUntilRunning_FailIfContainerNotFound(self, mock_timestamp):
    # Prepare mocks and variables.
    container = self._docker_client.containers.run.return_value
    container.reload.side_effect = docker_errors.NotFound('message required.')
    runner = self._CreateLocalDockerRunner()
    mock_timestamp.side_effect = list(range(20))

    # Setup state.
    runner.Start()

    # Act.
    with self.assertRaises(error_types.JobAborted):
      runner.WaitUntilRunning(deadline=10)


if __name__ == '__main__':
  tf.test.main()
