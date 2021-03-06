# Lint as: python2, python3
# Copyright 2019 Google LLC. All Rights Reserved.
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
"""TFX local trainer executor."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import os
from typing import Any, Dict, List, Text

import absl
import tensorflow as tf
import tensorflow_model_analysis as tfma

from google.protobuf import json_format

from tensorflow.python.lib.io import file_io  # pylint: disable=g-direct-tensorflow-import
from tensorflow_metadata.proto.v0 import schema_pb2
from tfx import types
from tfx.components.base import base_executor
from tfx.proto import trainer_pb2
from tfx.types import artifact_utils
from tfx.utils import import_utils
from tfx.utils import io_utils
from tfx.utils import path_utils

# Key for base model in executor input_dict.
BASE_MODEL_KEY = 'base_model'
# Key for examples in executor input_dict.
EXAMPLES_KEY = 'examples'
# Key for hyperparameters in executor input_dict.
HYPERPARAMETERS_KEY = 'hyperparameters'
# Key for schema in executor input_dict.
SCHEMA_KEY = 'schema'
# Key for transform graph in executor input_dict.
TRANSFORM_GRAPH_KEY = 'transform_graph'

# Key for output model in executor output_dict.
OUTPUT_MODEL_KEY = 'model'


def _all_files_pattern(file_pattern: Text) -> Text:
  return os.path.join(file_pattern, '*')


class TrainerFnArgs(object):
  """Wrapper class to help migrate from contrib.HParam to new data structure."""

  def __init__(self, **kwargs):
    self._data = kwargs

  def __getitem__(self, key):
    return self._data[key]

  def __getattr__(self, key):
    return self._data[key]


class GenericExecutor(base_executor.BaseExecutor):
  """Local generic trainer executor for the TFX Trainer component.

  The Trainer executor supplements TensorFlow training with a component to
  enable warm-start training of any user-specified TF model. The Trainer is
  a library built on top of TensorFlow that is expected to be integrated into a
  custom user-specified binary.

  To include Trainer in a TFX pipeline, configure your pipeline similar to
  https://github.com/tensorflow/tfx/blob/master/tfx/examples/chicago_taxi_pipeline/taxi_pipeline_simple.py#L104.

  For more details on the Trainer component itself, please refer to
  https://tensorflow.org/tfx/guide/trainer.  For a tutorial on Tensorflow,
  please refer to https://www.tensorflow.org/tutorials.

  How to create a trainer callback function to be used by this Trainer executor:
  A model training can be executed by TFX by first creating a run_fn callback
  method that defines, trains an TF Model and saves it to the provided location,
  This becomes the basis of the Executor for GenericTrainer. This Executor will
  then execute the run_fn with correct parameters by resolving the input
  artifacts, output artifacts and execution properties.
  """

  # Name of subdirectory which contains checkpoints from prior runs
  _CHECKPOINT_FILE_NAME = 'checkpoint'

  def _GetFn(self, exec_properties: Dict[Text, Any], fn_name: Text) -> Any:
    """Loads and returns user-defined function."""

    has_module_file = bool(exec_properties.get('module_file'))
    has_fn = bool(exec_properties.get(fn_name))

    if has_module_file == has_fn:
      raise ValueError(
          'Neither or both of module file and user function have been supplied in '
          "'exec_properties'.")

    if has_module_file:
      return import_utils.import_func_from_source(
          exec_properties['module_file'], fn_name)

    fn_path_split = exec_properties[fn_name].split('.')
    return import_utils.import_func_from_module('.'.join(fn_path_split[0:-1]),
                                                fn_path_split[-1])

  def _GetFnArgs(self, input_dict: Dict[Text, List[types.Artifact]],
                 output_dict: Dict[Text, List[types.Artifact]],
                 exec_properties: Dict[Text, Any]) -> TrainerFnArgs:
    custom_config = exec_properties.get('custom_config') or {}
    if not isinstance(custom_config, dict):
      raise ValueError('Expect custom_config to be a dict but got %s instead' %
                       type(custom_config))

    # Set up training parameters
    train_files = [
        _all_files_pattern(
            artifact_utils.get_split_uri(input_dict[EXAMPLES_KEY], 'train'))
    ]
    transform_output = artifact_utils.get_single_uri(
        input_dict[TRANSFORM_GRAPH_KEY]) if input_dict.get(
            TRANSFORM_GRAPH_KEY, None) else None
    eval_files = [
        _all_files_pattern(
            artifact_utils.get_split_uri(input_dict[EXAMPLES_KEY], 'eval'))
    ]
    schema_file = io_utils.get_only_uri_in_dir(
        artifact_utils.get_single_uri(input_dict[SCHEMA_KEY]))
    # TODO(ruoyu): Make this a dict of tag -> uri instead of list.
    base_model = path_utils.serving_model_path(
        artifact_utils.get_single_uri(input_dict[BASE_MODEL_KEY])
    ) if input_dict.get(BASE_MODEL_KEY) else None
    if input_dict.get(HYPERPARAMETERS_KEY):
      hyperparameters_file = io_utils.get_only_uri_in_dir(
          artifact_utils.get_single_uri(input_dict[HYPERPARAMETERS_KEY]))
      hyperparameters_config = json.loads(
          file_io.read_file_to_string(hyperparameters_file))
    else:
      hyperparameters_config = None

    train_args = trainer_pb2.TrainArgs()
    eval_args = trainer_pb2.EvalArgs()
    json_format.Parse(exec_properties['train_args'], train_args)
    json_format.Parse(exec_properties['eval_args'], eval_args)

    # https://github.com/tensorflow/tfx/issues/45: Replace num_steps=0 with
    # num_steps=None.  Conversion of the proto to python will set the default
    # value of an int as 0 so modify the value here.  Tensorflow will raise an
    # error if num_steps <= 0.
    train_steps = train_args.num_steps or None
    eval_steps = eval_args.num_steps or None

    output_path = artifact_utils.get_single_uri(output_dict[OUTPUT_MODEL_KEY])
    serving_model_dir = path_utils.serving_model_dir(output_path)
    eval_model_dir = path_utils.eval_model_dir(output_path)

    # TODO(b/126242806) Use PipelineInputs when it is available in third_party.
    return TrainerFnArgs(
        # A list of uris for train files.
        train_files=train_files,
        # An optional single uri for transform graph produced by TFT. Will be
        # None if not specified.
        transform_output=transform_output,
        # A single uri for the output directory of the serving model.
        serving_model_dir=serving_model_dir,
        # A single uri for the output directory of the eval model.
        # Note that this is estimator only, Keras doesn't require it for TFMA.
        eval_model_dir=eval_model_dir,
        # A list of uris for eval files.
        eval_files=eval_files,
        # A single uri for schema file.
        schema_file=schema_file,
        # Number of train steps.
        train_steps=train_steps,
        # Number of eval steps.
        eval_steps=eval_steps,
        # Base model that will be used for this training job.
        base_model=base_model,
        # An optional kerastuner.HyperParameters config.
        hyperparameters=hyperparameters_config,
        # Additional parameters to pass to trainer function.
        **custom_config)

  def Do(self, input_dict: Dict[Text, List[types.Artifact]],
         output_dict: Dict[Text, List[types.Artifact]],
         exec_properties: Dict[Text, Any]) -> None:
    """Uses a user-supplied run_fn to train a TensorFlow model locally.

    The Trainer Executor invokes a run_fn callback function provided by
    the user via the module_file parameter. In this function, user defines the
    model and train it, then save the model to the provided location.

    Args:
      input_dict: Input dict from input key to a list of ML-Metadata Artifacts.
        - examples: Examples used for training, must include 'train' and 'eval'
          splits.
        - transform_output: Optional input transform graph.
        - schema: Schema of the data.
      output_dict: Output dict from output key to a list of Artifacts.
        - output: Exported model.
      exec_properties: A dict of execution properties.
        - train_args: JSON string of trainer_pb2.TrainArgs instance, providing
          args for training.
        - eval_args: JSON string of trainer_pb2.EvalArgs instance, providing
          args for eval.
        - module_file: Python module file containing UDF model definition.
        - warm_starting: Whether or not we need to do warm starting.
        - warm_start_from: Optional. If warm_starting is True, this is the
          directory to find previous model to warm start on.
        - custom_config: Optional. Additional parameters to pass to trainer
          function.

    Returns:
      None

    Raises:
      ValueError: When neither or both of 'module_file' and 'run_fn'
        are present in 'exec_properties'.
      RuntimeError: If run_fn failed to generate model in desired location.
    """
    self._log_startup(input_dict, output_dict, exec_properties)

    fn_args = self._GetFnArgs(input_dict, output_dict, exec_properties)
    run_fn = self._GetFn(exec_properties, 'run_fn')

    run_fn(fn_args)

    # Train the model
    absl.logging.info('Training model.')
    run_fn(fn_args)
    if not tf.io.gfile.exists(fn_args.serving_model_dir):
      raise RuntimeError('run_fn failed to generate model.')
    absl.logging.info('Training complete. Model written to %s',
                      fn_args.serving_model_dir)


class Executor(GenericExecutor):
  """Local estimator based trainer executor used by the TFX Trainer component.

  How to create a trainer callback function to be used by this Trainer executor:
  An estimator can be executed by TFX by first creating a trainer_fn callback
  method that returns an estimator and some additional parameters, similar to
  https://github.com/tensorflow/tfx/blob/master/tfx/examples/chicago_taxi_pipeline/taxi_utils.py#L285.
  This becomes the basis of the new Executor for Trainer. This Executor will
  then train and evaluate this estimator using the
  tf.estimator.train_and_evaluate API to train locally.
  """

  def Do(self, input_dict: Dict[Text, List[types.Artifact]],
         output_dict: Dict[Text, List[types.Artifact]],
         exec_properties: Dict[Text, Any]) -> None:
    """Uses a user-supplied tf.estimator to train a TensorFlow model locally.

    The Trainer Executor invokes a training_fn callback function provided by
    the user via the module_file parameter.  With the tf.estimator returned by
    this function, the Trainer Executor then builds a TensorFlow model using the
    user-provided tf.estimator.

    Args:
      input_dict: Input dict from input key to a list of ML-Metadata Artifacts.
        - examples: Examples used for training, must include 'train' and 'eval'
          splits.
        - transform_output: Optional input transform graph.
        - schema: Schema of the data.
      output_dict: Output dict from output key to a list of Artifacts.
        - output: Exported model.
      exec_properties: A dict of execution properties.
        - train_args: JSON string of trainer_pb2.TrainArgs instance, providing
          args for training.
        - eval_args: JSON string of trainer_pb2.EvalArgs instance, providing
          args for eval.
        - module_file: Python module file containing UDF model definition.
        - warm_starting: Whether or not we need to do warm starting.
        - warm_start_from: Optional. If warm_starting is True, this is the
          directory to find previous model to warm start on.
        - custom_config: Optional. Additional parameters to pass to trainer
          function.

    Returns:
      None

    Raises:
      ValueError: When neither or both of 'module_file' and 'trainer_fn'
        are present in 'exec_properties'.
    """
    self._log_startup(input_dict, output_dict, exec_properties)

    fn_args = self._GetFnArgs(input_dict, output_dict, exec_properties)
    trainer_fn = self._GetFn(exec_properties, 'trainer_fn')

    schema = io_utils.parse_pbtxt_file(fn_args.schema_file, schema_pb2.Schema())

    training_spec = trainer_fn(fn_args, schema)

    # Train the model
    absl.logging.info('Training model.')
    tf.estimator.train_and_evaluate(training_spec['estimator'],
                                    training_spec['train_spec'],
                                    training_spec['eval_spec'])
    absl.logging.info('Training complete.  Model written to %s',
                      fn_args.serving_model_dir)

    # Export an eval savedmodel for TFMA
    # For distributed training, master and worker(s) try to export multiple
    # eval_savedmodels (b/147378113). To avoid that, only export
    # eval_savedmodel if eval_model_dir does not exist as an intermediate
    # solution until b/147378113 is resolved.
    if not tf.io.gfile.exists(fn_args.eval_model_dir):
      absl.logging.info('Exporting eval_savedmodel for TFMA.')
      tfma.export.export_eval_savedmodel(
          estimator=training_spec['estimator'],
          export_dir_base=fn_args.eval_model_dir,
          eval_input_receiver_fn=training_spec['eval_input_receiver_fn'])

      absl.logging.info('Exported eval_savedmodel to %s.',
                        fn_args.eval_model_dir)
