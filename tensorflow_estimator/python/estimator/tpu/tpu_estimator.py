# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
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
# ===================================================================
"""TPUEstimator class."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import copy
import enum
import math
import os
import signal
import sys
import threading
import time

import numpy as np
import six
from six.moves import queue as Queue  # pylint: disable=redefined-builtin
from six.moves import xrange  # pylint: disable=redefined-builtin

from tensorflow.core.framework import variable_pb2
from tensorflow.core.framework.summary_pb2 import Summary
from tensorflow.core.protobuf import config_pb2
from tensorflow.core.protobuf.tpu import compilation_result_pb2 as tpu_compilation_result
from tensorflow.python.client import session as tf_session
from tensorflow.python.data.ops import dataset_ops
from tensorflow.python.data.util import nest as data_nest
from tensorflow.python.distribute.cluster_resolver import tpu_cluster_resolver
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import errors
from tensorflow.python.framework import function
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import batch_ops
from tensorflow.python.ops import check_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import control_flow_util
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import resource_variable_ops
from tensorflow.python.ops import state_ops
from tensorflow.python.ops import summary_ops_v2 as contrib_summary
from tensorflow.python.ops import variable_scope
from tensorflow.python.ops import variables
from tensorflow.python.platform import tf_logging as logging
from tensorflow.python.saved_model import tag_constants
from tensorflow.python.summary import summary
from tensorflow.python.tpu import functional as tpu_functional
from tensorflow.python.tpu import preempted_hook
from tensorflow.python.tpu import session_support
from tensorflow.python.tpu import tensor_tracer
from tensorflow.python.tpu import tpu
from tensorflow.python.tpu import tpu_embedding_gradient
from tensorflow.python.tpu import tpu_feed
from tensorflow.python.tpu import tpu_function
from tensorflow.python.tpu import training_loop
from tensorflow.python.tpu.ops import tpu_ops
from tensorflow.python.training import basic_session_run_hooks
from tensorflow.python.training import evaluation
from tensorflow.python.training import session_run_hook
from tensorflow.python.training import training
from tensorflow.python.training import training_util
from tensorflow.python.util import function_utils
from tensorflow.python.util import nest
from tensorflow.python.util import tf_inspect
from tensorflow.python.util.tf_export import estimator_export
from tensorflow_estimator.python.estimator import estimator as estimator_lib
from tensorflow_estimator.python.estimator import model_fn as model_fn_lib
from tensorflow_estimator.python.estimator.export import export_output as export_output_lib
from tensorflow_estimator.python.estimator.tpu import _tpu_estimator_embedding
from tensorflow_estimator.python.estimator.tpu import error_handling
from tensorflow_estimator.python.estimator.tpu import iteration_count_estimator
from tensorflow_estimator.python.estimator.tpu import tpu_config
from tensorflow_estimator.python.estimator.tpu import tpu_context
from tensorflow_estimator.python.estimator.tpu import util as util_lib
from tensorflow_estimator.python.estimator.tpu._tpu_estimator_embedding import AdagradParameters  # pylint: disable=unused-import
from tensorflow_estimator.python.estimator.tpu._tpu_estimator_embedding import AdamParameters  # pylint: disable=unused-import
from tensorflow_estimator.python.estimator.tpu._tpu_estimator_embedding import EmbeddingConfigSpec  # pylint: disable=unused-import
from tensorflow_estimator.python.estimator.tpu._tpu_estimator_embedding import StochasticGradientDescentParameters  # pylint: disable=unused-import

_INITIAL_LOSS = 1e7
_ZERO_LOSS = 0.
_TPU_ESTIMATOR = 'tpu_estimator'
_ITERATIONS_PER_LOOP_VAR = 'iterations_per_loop'
_BATCH_SIZE_KEY = 'batch_size'
_CTX_KEY = 'context'
_USE_TPU_KEY = 'use_tpu'
_CROSS_REPLICA_SUM_OP = 'CrossReplicaSum'
_ONE_GIGABYTE = 1024 * 1024 * 1024
_TPU_ENQUEUE_OPS = '_tpu_enqueue_ops'
_TPU_TRAIN_OP = '_tpu_train_op'
_INFERENCE_ON_TPU_MODE = '_inference_on_tpu'
_KEY_WHEN_PREDICTIONS_IS_A_TENSOR = '_key_when_predictions_is_a_tensor'
_TENSOR_PACKER_SMALL_FEATURE_DIM_SIZE = 1
_TENSOR_PACKER_MINIMUM_NUM_SMALL_FEATURES_TO_GROUP = 5
_TENSOR_PACKER_CONCATENATED_SMALL_FEATURES_KEY = '_concatenated_small_features'

# Ideally _USE_TPU_KEY should be reserved as well. However there are already
# models that make use of this key, thus it can not be reserved now to prevent
# breakage. In the long run, we would like to mitigate this by migrating models
# off of using _USE_TPU_KEY.
_RESERVED_PARAMS_KEYS = [_BATCH_SIZE_KEY, _CTX_KEY]

# TODO(b/65703635): Flip the value and remove all dead code. Currently, this is
# only used for per-core based deployments. For per-host based pipelines, if a
# user returns a Dataset instance it will be automatically wrapped in a
# tf.while_loop (This can be disabled by returning features and labels
# explicitly).
_WRAP_INPUT_FN_INTO_WHILE_LOOP = False

if ops.get_to_proto_function(
    '{}_{}'.format(_TPU_ESTIMATOR, _ITERATIONS_PER_LOOP_VAR)) is None:
  ops.register_proto_function(
      '{}_{}'.format(_TPU_ESTIMATOR, _ITERATIONS_PER_LOOP_VAR),
      proto_type=variable_pb2.VariableDef,
      to_proto=resource_variable_ops._to_proto_fn,  # pylint: disable=protected-access
      from_proto=resource_variable_ops._from_proto_fn)  # pylint: disable=protected-access


def _is_iterable(obj):
  """A Python 2 and 3 compatible util to check whether `obj` is iterable."""
  try:
    iter(obj)
    return True
  except TypeError:
    return False


class CatchInvalidHostcallFunctions(control_flow_ops.XLAControlFlowContext):

  def AddOp(self, op):
    if op.type in [
        'AudioSummary', 'AudioSummaryV2', 'HistogramSummary', 'ImageSummary',
        'MergeSummary', 'ScalarSummary', 'TensorSummary', 'TensorSummaryV2'
    ]:
      raise ValueError('Please use tf.contrib.summary instead of tf.summary '
                       'inside of host_calls.')


def _create_global_step(graph):
  graph = graph or ops.get_default_graph()
  if training.get_global_step(graph) is not None:
    raise ValueError('"global_step" already exists.')
  # Create in proper graph and base name_scope.
  with graph.as_default() as g, g.name_scope(None):
    return variable_scope.get_variable(
        ops.GraphKeys.GLOBAL_STEP,
        shape=[],
        dtype=dtypes.int64,
        initializer=init_ops.zeros_initializer(),
        trainable=False,
        use_resource=True,
        collections=[ops.GraphKeys.GLOBAL_VARIABLES, ops.GraphKeys.GLOBAL_STEP])


def _create_or_get_iterations_per_loop():
  """Creates or gets the iterations_per_loop variable.

  In TPUEstimator, the user provided computation, the model_fn, is wrapped
  inside a tf.while_loop for peak performance. The iterations of the loop are
  specified by this variable, which adjusts its value on the CPU after each TPU
  program execution and before the next TPU execution.

  The purpose of using a variable, rather then a constant, is to allow
  TPUEstimator adapt the TPU training iterations according to the final steps
  specified by users. For example, if the user sets the iterations_per_loop as 4
  in TPUConfig and steps as 10 in TPUEstimator.train(), the iterations_per_loop
  variable will have the following value before each TPU training.

      - 1-th TPU execution: iterations_per_loop = 4
      - 2-th TPU execution: iterations_per_loop = 4
      - 3-th TPU execution: iterations_per_loop = 2

  As model_fn increases the global step once per train_op invocation, the global
  step is 10 after all TPU executions, matching the steps=10 inputs passed in by
  users.

  Returns:
    A TF non-trainable resource variable.

  Raises:
    RuntimeError: If multi iterations_per_loop variables were found.
  """
  graph = ops.get_default_graph()
  collection_name = '{}_{}'.format(_TPU_ESTIMATOR, _ITERATIONS_PER_LOOP_VAR)
  iter_vars = graph.get_collection(collection_name)
  if len(iter_vars) == 1:
    return iter_vars[0]
  elif len(iter_vars) > 1:
    raise RuntimeError('Multiple iterations_per_loop_var in collection.')

  with ops.colocate_with(training_util.get_global_step()):
    with variable_scope.variable_scope(
        _TPU_ESTIMATOR, reuse=variable_scope.AUTO_REUSE):
      return variable_scope.get_variable(
          _ITERATIONS_PER_LOOP_VAR,
          initializer=init_ops.zeros_initializer(),
          shape=[],
          dtype=dtypes.int32,
          trainable=False,
          collections=[collection_name, ops.GraphKeys.LOCAL_VARIABLES],
          use_resource=True)


def _sync_variables_ops(ctx):
  """Create varriables synchronization ops.

  Gets the variables back from TPU nodes. This means the variables updated
  by TPU will now be *synced* to host memory.
  In BROADCAST mode, we skip this sync since the variables are ususally too
  big to transmit via RPC.

  Args:
    ctx: A `_InternalTPUContext` instance with mode.

  Returns:
    A list of sync ops.
  """

  if not ctx.is_input_broadcast_with_iterators():
    return [
        array_ops.check_numerics(v.read_value(),
                                 'Gradient for %s is NaN' % v.name).op
        for v in variables.trainable_variables()
    ]
  else:
    return [control_flow_ops.no_op()]


def _increase_eval_step_op(iterations_per_loop):
  """Returns an op to increase the eval step for TPU evaluation.

  Args:
    iterations_per_loop: Tensor. The number of eval steps running in TPU system
      before returning to CPU host for each `Session.run`.

  Returns:
    An operation
  """
  eval_step = evaluation._get_or_create_eval_step()  # pylint: disable=protected-access
  # Estimator evaluate increases 1 by default. So, we increase the difference.
  return state_ops.assign_add(
      eval_step,
      math_ops.cast(iterations_per_loop - 1, dtype=eval_step.dtype),
      use_locking=True)


def _extract_key_names(tensor_or_dict):
  if isinstance(tensor_or_dict, dict):
    return sorted(tensor_or_dict.keys())
  return []


class PeriodicLogger(object):

  def __init__(self, seconds):
    self._log_every_n_seconds = seconds
    self._last_log_time = 0

  def log(self, msg, *args, **kw):
    if time.time() - self._last_log_time > self._log_every_n_seconds:
      self._last_log_time = time.time()
      logging.info(msg, *args, **kw)


class _SIGNAL(object):
  """Signal used to control the thread of infeed/outfeed.

  All preserved signals must be negative numbers. Positive numbers are used to
  indicate the number of iterations for next training/evaluation loop.
  """
  NEXT_BATCH = -1
  STOP = -2


@estimator_export(v1=['estimator.tpu.TPUEstimatorSpec'])
class TPUEstimatorSpec(model_fn_lib._TPUEstimatorSpec):  # pylint: disable=protected-access
  """Ops and objects returned from a `model_fn` and passed to `TPUEstimator`.

  See `EstimatorSpec` for `mode`, `predictions`, `loss`, `train_op`, and
  `export_outputs`.

  For evaluation, `eval_metrics `is a tuple of `metric_fn` and `tensors`, where
  `metric_fn` runs on CPU to generate metrics and `tensors` represents the
  `Tensor`s transferred from TPU system to CPU host and passed to `metric_fn`.
  To be precise, TPU evaluation expects a slightly different signature from the
  `tf.estimator.Estimator`. While `EstimatorSpec.eval_metric_ops` expects a
  dict, `TPUEstimatorSpec.eval_metrics` is a tuple of `metric_fn` and `tensors`.
  The `tensors` could be a list of `Tensor`s or dict of names to `Tensor`s. The
  `tensors` usually specify the model logits, which are transferred back from
  TPU system to CPU host. All tensors must have be batch-major, i.e., the batch
  size is the first dimension. Once all tensors are available at CPU host from
  all shards, they are concatenated (on CPU) and passed as positional arguments
  to the `metric_fn` if `tensors` is list or keyword arguments if `tensors` is
  a dict. `metric_fn` takes the `tensors` and returns a dict from metric string
  name to the result of calling a metric function, namely a `(metric_tensor,
  update_op)` tuple. See `TPUEstimator` for MNIST example how to specify the
  `eval_metrics`.

  `scaffold_fn` is a function running on CPU to generate the `Scaffold`. This
  function should not capture any Tensors in `model_fn`.

  `host_call` is a tuple of a `function` and a list or dictionary of `tensors`
  to pass to that function and returns a list of Tensors. `host_call` currently
  works for train() and evaluate(). The Tensors returned by the function is
  executed on the CPU on every step, so there is communication overhead when
  sending tensors from TPU to CPU. To reduce the overhead, try reducing the
  size of the tensors. The `tensors` are concatenated along their major (batch)
  dimension, and so must be >= rank 1. The `host_call` is useful for writing
  summaries with `tf.contrib.summary.create_file_writer`.
  """

  def __new__(cls,
              mode,
              predictions=None,
              loss=None,
              train_op=None,
              eval_metrics=None,
              export_outputs=None,
              scaffold_fn=None,
              host_call=None,
              training_hooks=None,
              evaluation_hooks=None,
              prediction_hooks=None):
    """Creates a validated `TPUEstimatorSpec` instance."""
    host_calls = {}
    if eval_metrics is not None:
      host_calls['eval_metrics'] = eval_metrics
    if host_call is not None:
      host_calls['host_call'] = host_call
    _OutfeedHostCall.validate(host_calls)

    training_hooks = tuple(training_hooks or [])
    evaluation_hooks = tuple(evaluation_hooks or [])
    prediction_hooks = tuple(prediction_hooks or [])

    for hook in training_hooks + evaluation_hooks + prediction_hooks:
      if not isinstance(hook, session_run_hook.SessionRunHook):
        raise TypeError('All hooks must be SessionRunHook instances, given: {}'
                        .format(hook))

    return super(TPUEstimatorSpec, cls).__new__(
        cls,
        mode=mode,
        predictions=predictions,
        loss=loss,
        train_op=train_op,
        eval_metrics=eval_metrics,
        export_outputs=export_outputs,
        scaffold_fn=scaffold_fn,
        host_call=host_call,
        training_hooks=training_hooks,
        evaluation_hooks=evaluation_hooks,
        prediction_hooks=prediction_hooks)

  def as_estimator_spec(self):
    """Creates an equivalent `EstimatorSpec` used by CPU train/eval."""
    host_calls = {}
    if self.eval_metrics is not None:
      host_calls['eval_metrics'] = self.eval_metrics
    if self.host_call is not None:
      host_calls['host_call'] = self.host_call
    host_call_ret = _OutfeedHostCall.create_cpu_hostcall(host_calls)
    eval_metric_ops = None
    if self.eval_metrics is not None:
      eval_metric_ops = host_call_ret['eval_metrics']
    hooks = None
    if self.host_call is not None:
      hooks = [_OutfeedHostCallHook(host_call_ret['host_call'])]
    loss = self.loss
    if tensor_tracer.TensorTracer.is_enabled() \
       and self.train_op is not None:
      tt = tensor_tracer.TensorTracer()
      loss = tt.trace_cpu(ops.get_default_graph(), loss, self.train_op)

    hooks = tuple(hooks or [])
    scaffold = self.scaffold_fn() if self.scaffold_fn else None
    return model_fn_lib.EstimatorSpec(
        mode=self.mode,
        predictions=self.predictions,
        loss=loss,
        train_op=self.train_op,
        eval_metric_ops=eval_metric_ops,
        export_outputs=self.export_outputs,
        scaffold=scaffold,
        training_hooks=self.training_hooks + hooks,
        evaluation_hooks=self.evaluation_hooks + hooks,
        prediction_hooks=self.prediction_hooks + hooks)


class _OpQueueContext(object):
  """Manages work queue and thread for a infeed/outfeed thread."""

  def __init__(self, name, target, args):
    self._name = name
    self._queue = Queue.Queue()
    args = (self,) + args
    self._thread = threading.Thread(name=name, target=target, args=args)
    self._thread.daemon = True
    self._thread.start()

  def stop(self):
    self._queue.put(_SIGNAL.STOP)

  def send_next_batch_signal(self, iterations):
    self._queue.put(iterations)

  def read_iteration_counts(self):
    while True:
      iterations = self._queue.get(block=True)
      logging.debug('%s read iterations %s', self._name, iterations)
      if iterations == _SIGNAL.STOP:
        logging.info('%s received shutdown signal, stopping.', self._name)
        return
      yield iterations

  def join(self):
    logging.info('Shutting down %s thread.', self._name)
    self.stop()
    self._thread.join()


class _OpSignalOnceQueueContext(_OpQueueContext):
  """Manages work queue and thread for a infeed/outfeed thread.

  This subclass only signals once.
  """

  def __init__(self, name, target, args):
    super(_OpSignalOnceQueueContext, self).__init__(name, target, args)
    self._has_signaled = False

  def send_next_batch_signal(self, iterations):
    if not self._has_signaled:
      self._queue.put(iterations)
      self._has_signaled = True


class TPUInfeedOutfeedSessionHook(session_run_hook.SessionRunHook):
  """A Session hook setting up the TPU initialization, infeed, and outfeed.

  This hook does two major things:
  1. initialize and shutdown TPU system.
  2. launch and join the threads for infeed enqueue and (optional) outfeed
     dequeue.
  """

  def __init__(self,
               ctx,
               enqueue_ops,
               dequeue_ops,
               tpu_compile_op,
               run_infeed_loop_on_coordinator=True,
               rendezvous=None,
               master=None,
               session_config=None,
               tpu_init_ops=None,
               outfeed_every_n_steps=1):
    self._master_job = ctx.master_job
    self._enqueue_ops = enqueue_ops
    self._dequeue_ops = dequeue_ops
    self._rendezvous = rendezvous
    self._master = master
    self._session_config = session_config
    self._init_ops = list(tpu_init_ops or [])
    if ctx.embedding_config is None:
      self._embedding_layer_config = None
    else:
      self._embedding_layer_config = (
          ctx.embedding_config.tpu_embedding.config_proto)
    self._run_infeed_loop_on_coordinator = run_infeed_loop_on_coordinator
    self._initial_infeed_sleep_secs = (
        ctx.config.tpu_config.initial_infeed_sleep_secs)
    self._tpu_compile_op = tpu_compile_op

    # When using model parallelism, the TPU is pre-initialized at startup to
    # fetch mesh information. We skip re-initializing it here for
    # MeshTensorFlow since it places variables on TPU directly. Reinitialize tpu
    # is causing the variable corruption since the previous allocated memory
    # might be overwritten for other purpose.
    if (ctx.model_parallelism_enabled and
        (ctx.config.tpu_config.per_host_input_for_training is
         tpu_config.InputPipelineConfig.BROADCAST)):
      self._should_initialize_tpu = False
    else:
      self._should_initialize_tpu = True
    self._outfeed_every_n_steps = outfeed_every_n_steps

  def begin(self):
    logging.info('TPU job name %s', self._master_job)
    self._iterations_per_loop_var = _create_or_get_iterations_per_loop()
    if self._should_initialize_tpu:
      self._finalize_ops = [tpu.shutdown_system(job=self._master_job)]
    else:
      self._finalize_ops = []

    summary_writer_init_ops = contrib_summary.summary_writer_initializer_op()
    self._init_ops.extend(summary_writer_init_ops)
    # Get all the writer resources from the initializer, so we know what to
    # flush.
    for op in summary_writer_init_ops:
      self._finalize_ops.append(contrib_summary.flush(writer=op.inputs[0]))

  def _run_infeed(self, queue_ctx, session):
    logging.info('Starting infeed thread controller.')
    if self._initial_infeed_sleep_secs:
      logging.info('Infeed thread sleeping for %d seconds.',
                   self._initial_infeed_sleep_secs)
      time.sleep(self._initial_infeed_sleep_secs)
      logging.info('Infeed thread starting after sleep')

    with self._rendezvous.catch_errors(source='infeed', session=session):
      if self._run_infeed_loop_on_coordinator:
        for count, steps in enumerate(queue_ctx.read_iteration_counts()):
          for i in xrange(steps):
            logging.debug('Infeed enqueue for iteration (%d, %d)', count, i)
            session.run(self._enqueue_ops)
      else:
        for _ in queue_ctx.read_iteration_counts():
          session.run(self._enqueue_ops)
      logging.info('Infeed thread finished, shutting down.')

  def _run_outfeed(self, queue_ctx, session):
    logging.info('Starting outfeed thread controller.')
    status_logger = PeriodicLogger(seconds=60)
    with self._rendezvous.catch_errors(source='outfeed', session=session):
      for count, steps in enumerate(queue_ctx.read_iteration_counts()):
        step_counter = 0
        for i in xrange(steps):
          logging.debug('Outfeed dequeue for iteration (%d, %d)', count, i)
          if step_counter % self._outfeed_every_n_steps == 0:
            session.run(self._dequeue_ops)
          step_counter += 1
          status_logger.log('Outfeed finished for iteration (%d, %d)', count, i)
      logging.info('Outfeed thread finished, shutting down.')

  def _create_infeed_controller(self, name, target, args):
    return _OpQueueContext(name=name, target=target, args=args)

  def _assertCompilationSucceeded(self, result, coord):
    proto = tpu_compilation_result.CompilationResultProto()
    proto.ParseFromString(result)
    if proto.status_error_message:
      logging.error('Compilation failed: {}'.format(proto.status_error_message))
      coord.request_stop()
    else:
      logging.info('Compilation succeeded')

  def after_create_session(self, session, coord):
    if self._should_initialize_tpu:
      logging.info('Init TPU system')
      start = time.time()
      with ops.Graph().as_default():
        with tf_session.Session(
            self._master, config=self._session_config) as sess:
          sess.run(
              tpu.initialize_system(
                  job=self._master_job,
                  embedding_config=self._embedding_layer_config))
      logging.info('Initialized TPU in %d seconds', time.time() - start)

    session.run(self._init_ops,
                options=config_pb2.RunOptions(timeout_in_ms=5 * 60 * 1000))

    if os.environ.get('TPU_SPLIT_COMPILE_AND_EXECUTE', '') == '1':
      logging.info('Compiling user program: this may take a while...')
      self._assertCompilationSucceeded(session.run(self._tpu_compile_op), coord)

    self._infeed_controller = self._create_infeed_controller(
        name='InfeedController', target=self._run_infeed, args=(session,))

    self._outfeed_controller = _OpQueueContext(
        name='OutfeedController', target=self._run_outfeed, args=(session,))

    # Enable the worker watchdog to terminate workers on coordinator exit.
    watchdog_timeout = int(os.environ.get('TF_TPU_WATCHDOG_TIMEOUT', '0'))
    if watchdog_timeout > 0:
      session_support.start_worker_watchdog(session,
                                            shutdown_timeout=watchdog_timeout)

  def before_run(self, run_context):
    iterations = run_context.session.run(self._iterations_per_loop_var)

    logging.info('Enqueue next (%d) batch(es) of data to infeed.', iterations)
    self._infeed_controller.send_next_batch_signal(iterations)

    logging.info('Dequeue next (%d) batch(es) of data from outfeed.',
                 iterations)
    self._outfeed_controller.send_next_batch_signal(iterations)

  def end(self, session):
    logging.info('Stop infeed thread controller')
    self._infeed_controller.join()
    self._rendezvous.record_done('infeed')

    logging.info('Stop output thread controller')
    self._outfeed_controller.join()
    self._rendezvous.record_done('outfeed')

    logging.info('Shutdown TPU system.')
    session.run(self._finalize_ops)


class TPUInfeedOutfeedSessionHookForPrediction(TPUInfeedOutfeedSessionHook):

  def __init__(self, ctx, enqueue_ops, dequeue_ops, tpu_compile_op,
               rendezvous=None, master=None, session_config=None):
    super(TPUInfeedOutfeedSessionHookForPrediction, self).__init__(
        ctx,
        enqueue_ops,
        dequeue_ops,
        tpu_compile_op=tpu_compile_op,
        run_infeed_loop_on_coordinator=False,
        rendezvous=rendezvous,
        master=master,
        session_config=session_config)

  def _create_infeed_controller(self, name, target, args):
    return _OpSignalOnceQueueContext(name=name, target=target, args=args)


class _TPUStopAtStepHook(session_run_hook.SessionRunHook):
  """Hook that requests stop at a specified step.

  This hook is similar to the `session_run_hook._StopAfterNEvalsHook` with
  following differences for TPU training:

  1. This hook sets the variable for `iterations_per_loop`, which is used by
     `TPUInfeedOutfeedSessionHook` to control the iterations for infeed/outfeed.
     If the `iterations_per_loop` value is specified as time in seconds, the
     number of iterations per `Session.run` will be estimated automatically
     based on per iteration runtime.

     As the hook execution order is not guaranteed, the variable update is
     handled in `after_create_session` and `after_run` as
     `TPUInfeedOutfeedSessionHook` reads the variable value in `before_run`.

  2. For each training loop (session.run), the global step could be increased
     multiple times on TPU. The global step tensor value will be explicitly read
     again in `after_run` to ensure the latest value is retrieved to avoid race
     condition.
  """

  def __init__(self,
               iterations_per_loop_counter,
               num_steps=None,
               final_step=None):
    """Initializes a `TPUStopAtStepHook`.

    Args:
      iterations_per_loop_counter: A namedtuple of [`value',`unit`] that
        represents the number of 'iterations count' or 'time in seconds' to run
        optimizer per loop, based on the `unit` specified, `count` or `seconds`
        respectively.
      num_steps: Number of steps to execute.
      final_step: Step after which to stop.

    Raises:
      ValueError: If one of the arguments is invalid.
    """
    if num_steps is None and final_step is None:
      raise ValueError('One of `num_steps` or `final_step` must be specified.')
    if num_steps is not None and final_step is not None:
      raise ValueError(
          'Only one of `num_steps` or `final_step` can be specified.')
    self._iterations_per_loop_counter = iterations_per_loop_counter
    if self._iterations_per_loop_counter.unit not in ['seconds', 'count']:
      raise ValueError(
          'Only `count` or `seconds` are accepted as the '
          '`iterations_per_loop_counter.unit')
    self._num_steps = num_steps
    self._final_step = final_step
    self._next_iteration_count = 1
    self._iteration_count_estimator = None
    if self._iterations_per_loop_counter.unit == 'seconds':
      self._iteration_count_estimator = (
          iteration_count_estimator.IterationCountEstimator())
    self._start_time = time.time()

  def _next_iterations(self, global_step, final_step):
    """Computes the next iterations count.

    The next iterations count is computed by choosing the smaller of the
    remaining step count (`final_step` - `global_step`) and the estimated
    iterations count returned by the estimator.

    Args:
      global_step: The current step.
      final_step: Step after which to stop.

    Returns:
      The number of iterations count to run per loop.
    """
    remaining_steps = final_step - global_step

    if self._iteration_count_estimator is not None:
      estimated_iterations = self._iteration_count_estimator.get(
          self._iterations_per_loop_counter.value)
    else:
      estimated_iterations = self._iterations_per_loop_counter.value

    self._next_iteration_count = min(remaining_steps, estimated_iterations)
    return self._next_iteration_count

  def begin(self):
    """Initializes variables.

    Initializes the global step and iterations per loop variables.

    Raises:
      RuntimeError: An error occurred if global step variable does not exist.
    """
    self._global_step_tensor = training_util.get_global_step()
    if self._global_step_tensor is None:
      raise RuntimeError('Global step should be created.')

    self._iterations_per_loop_var = _create_or_get_iterations_per_loop()

  def after_create_session(self, session, coord):
    """Computes and updates the first time iterations count.

    The iterations are computed by choosing the smaller of the (`final step` -
    `global step`), and the initial estimated iterations returned by the
    estimator (by default is 1).

    Args:
      session: A TensorFlow Session that has been created.
      coord: A Coordinator object which keeps track of all threads.
    """
    global_step = session.run(self._global_step_tensor)
    if self._final_step is None:
      self._final_step = global_step + self._num_steps

    iterations = self._next_iterations(global_step, self._final_step)
    self._iterations_per_loop_var.load(iterations, session=session)

  def before_run(self, run_context):
    """Reset the timer."""
    if self._iteration_count_estimator is not None:
      self._start_time = time.time()

  def after_run(self, run_context, run_values):
    """Computes the next iterations per loop value or terminates.

    Computes the elapsed time to run the last optimizer loop and if the
    `IterationCountEstimator` is used, records the elapsed time and iterations
    count. If the final step count has been reached, terminates. Otherwise,
    computes and updates the number of iterations to run the optimizer per loop.

    Args:
      run_context: A `SessionRunContext` object.
      run_values: A SessionRunValues object.
    """
    if self._iteration_count_estimator is not None:
      elapsed_time = time.time() - self._start_time
      logging.info("ElapsedTime: %.3f", elapsed_time)
      self._iteration_count_estimator.update(elapsed_time,
                                             self._next_iteration_count)

    # Global step cannot be retrieved via SessionRunArgs and before_run due to
    # race condition.
    global_step = run_context.session.run(self._global_step_tensor)
    if global_step >= self._final_step:
      run_context.request_stop()
    else:
      iterations = self._next_iterations(global_step, self._final_step)
      self._iterations_per_loop_var.load(
          iterations, session=run_context.session)


class _SetEvalIterationsHook(session_run_hook.SessionRunHook):
  """Hook that requests stop at a specified step."""

  def __init__(self, num_steps):
    """Initializes a `_SetEvalIterationsHook`.

    Args:
      num_steps: Number of steps to execute.
    """
    self._num_steps = num_steps

  def begin(self):
    self._iterations_per_loop_var = _create_or_get_iterations_per_loop()

  def after_create_session(self, session, coord):
    self._iterations_per_loop_var.load(self._num_steps, session=session)


class _StoppingPredictHook(session_run_hook.SessionRunHook):
  """Hook that requests stop according to the stopping signal in prediction."""

  def __init__(self, scalar_stopping_signal):
    self._scalar_stopping_signal = scalar_stopping_signal

  def begin(self):
    self._iterations_per_loop_var = _create_or_get_iterations_per_loop()

  def after_create_session(self, session, coord):
    # This is not necessary as we do not run infeed enqueue and outfeed dequeue
    # in side threads for prediction model. But it makes the
    # TPUInfeedOutfeedSessionHook prints nice message.
    self._iterations_per_loop_var.load(1, session=session)

  def before_run(self, run_context):
    return session_run_hook.SessionRunArgs(self._scalar_stopping_signal)

  def after_run(self, run_context, run_values):
    _ = run_context
    scalar_stopping_signal = run_values.results
    if _StopSignals.should_stop(scalar_stopping_signal):
      # NOTE(xiejw): In prediction, stopping signals are inserted for each
      # batch. And we append one more batch to signal the system it should stop.
      # The data flow might look like
      #
      #  batch   0: images, labels, stop = 0  (user provided)
      #  batch   1: images, labels, stop = 0  (user provided)
      #  ...
      #  batch  99: images, labels, stop = 0  (user provided)
      #  batch 100: images, labels, stop = 1  (TPUEstimator appended)
      #
      # where the final batch (id = 100) is appended by TPUEstimator, so we
      # should drop it before returning the predictions to user.
      # To achieve that, we throw the OutOfRangeError in after_run. Once
      # Monitored Session sees this error in SessionRunHook.after_run, the
      # "current" prediction, i.e., batch with id=100, will be discarded
      # immediately
      raise errors.OutOfRangeError(None, None, 'Stopped by stopping signal.')


def generate_per_core_enqueue_ops_fn_for_host(
    ctx, input_fn, inputs_structure_recorder, host_device, host_id):
  """Generates infeed enqueue ops for per-core input_fn on a single host."""
  captured_infeed_queue = _CapturedObject()
  tpu_ordinal_function_impl = ctx.tpu_ordinal_function(host_id)

  def enqueue_ops_fn():
    """A fn returns enqueue_ops."""
    num_cores_per_host = ctx.num_of_cores_per_host
    per_host_sharded_inputs = []
    for core_ordinal in range(num_cores_per_host):
      with ops.name_scope('ordinal_%d' % (core_ordinal)):
        user_context = tpu_context.TPUContext(
            internal_ctx=ctx,
            input_device=host_device,
            invocation_index=host_id * ctx.num_of_cores_per_host + core_ordinal)
        inputs = _Inputs.from_input_fn(input_fn(user_context))
        if inputs.is_dataset:
          raise TypeError(
              '`input_fn` returning `Dataset`  is not yet supported in '
              'per-Core input pipeline deployment yet. Please set '
              'TPUConfig.per_host_input_for_training to True or return '
              '`features` and `labels` from `input_fn`')
        features, labels = inputs.features_and_labels()

        inputs_structure_recorder.validate_and_record_structure(
            features, labels)
        flattened_inputs = (
            inputs_structure_recorder.flatten_features_and_labels(
                features, labels))
        per_host_sharded_inputs.append(flattened_inputs)

    infeed_queue = tpu_feed.InfeedQueue(
        number_of_tuple_elements=len(per_host_sharded_inputs[0]))
    captured_infeed_queue.capture(infeed_queue)

    per_host_enqueue_ops = infeed_queue.generate_enqueue_ops(
        per_host_sharded_inputs, tpu_ordinal_function=tpu_ordinal_function_impl)
    return per_host_enqueue_ops

  return enqueue_ops_fn, captured_infeed_queue


def generate_per_host_enqueue_ops_fn_for_host(
    ctx, input_fn, inputs_structure_recorder, batch_axis, device, host_id):
  """Generates infeed enqueue ops for per-host input_fn on a single host."""
  captured_infeed_queue = _CapturedObject()

  dataset_initializer = None

  with ops.device(device):
    user_context = tpu_context.TPUContext(
        internal_ctx=ctx, input_device=device, invocation_index=host_id)
    inputs = _Inputs.from_input_fn(input_fn(user_context))

    is_dataset = inputs.is_dataset
    if ctx.mode == model_fn_lib.ModeKeys.PREDICT:
      if not is_dataset:
        raise TypeError(
            'For mode PREDICT, `input_fn` must return `Dataset` instead of '
            '`features` and `labels`.')
      if batch_axis is not None:
        raise TypeError('For mode PREDICT, batch_axis is not supported yet.')
      inputs = _InputsWithStoppingSignals(
          dataset=inputs.dataset,
          batch_size=ctx.batch_size_for_input_fn,
          add_padding=True)

    if is_dataset:
      dataset_initializer = inputs.dataset_initializer()

    tpu_ordinal_function_impl = ctx.tpu_ordinal_function(host_id)

  def enqueue_ops_fn():
    """A Fn returning the TPU infeed enqueue ops.

    By providing as a Fn, it can be invoked inside the tf.while_loop such that
    the input pipeline for multiple iterations can be executed by one
    Session.run call.

    Returns:
      list of dict of ops.
    """
    with ops.device(device):
      num_of_replicas_per_host = ctx.num_of_replicas_per_host
      # Convert user input to features and labels.  If the user returns a
      # dataset, it is initialized and the features and labels extracted via
      # `dataset.iterator.get_next()`
      features, labels = inputs.features_and_labels()
      signals = inputs.signals()

      features, labels, enqueue_datas_list = (
          _tpu_estimator_embedding.split_inputs(
              ctx, features, labels,
              num_cores_per_batch=num_of_replicas_per_host))

      inputs_structure_recorder.validate_and_record_structure(features, labels)
      unsharded_tensor_list = (
          inputs_structure_recorder.flatten_features_and_labels(
              features, labels, signals))

      infeed_queue = tpu_feed.InfeedQueue(
          tuple_types=[t.dtype for t in unsharded_tensor_list],
          tuple_shapes=[t.shape for t in unsharded_tensor_list],
          shard_dimensions=batch_axis)
      captured_infeed_queue.capture(infeed_queue)
      infeed_queue.set_number_of_shards(num_of_replicas_per_host)
      per_host_enqueue_ops = (
          infeed_queue.split_inputs_and_generate_enqueue_ops(
              unsharded_tensor_list,
              placement_function=lambda x: device,
              tpu_ordinal_function=tpu_ordinal_function_impl))

      if ctx.embedding_config:
        per_host_enqueue_ops.extend(
            ctx.embedding_config.tpu_embedding.generate_enqueue_ops(
                enqueue_datas_list))

      if signals is None:
        return per_host_enqueue_ops
      else:
        return {
            'ops': per_host_enqueue_ops,
            'signals': signals,
        }

  return enqueue_ops_fn, captured_infeed_queue, dataset_initializer


def generate_per_host_v2_enqueue_ops_fn_for_host(
    ctx, input_fn, inputs_structure_recorder, device, host_id):
  """Generates infeed enqueue ops for per-host input_fn on a single host."""
  captured_infeed_queue = _CapturedObject()
  dataset_initializer = None

  with ops.device(device):
    user_context = tpu_context.TPUContext(
        internal_ctx=ctx, input_device=device, invocation_index=host_id)
    inputs = _Inputs.from_input_fn(input_fn(user_context))

    is_dataset = inputs.is_dataset
    if not is_dataset:
      raise TypeError('`input_fn` must return a `Dataset` for the PER_HOST_V2 '
                      'input pipeline configuration.')

    # Be aware that when num_cores_per_replica > num_cores_per_host,
    # ctx.num_of_replicas_per_host is 0.
    if ctx.mode == model_fn_lib.ModeKeys.PREDICT:
      inputs = _InputsWithStoppingSignals(
          dataset=inputs.dataset,
          batch_size=ctx.batch_size_for_input_fn,
          add_padding=True,
          num_invocations_per_step=max(1, ctx.num_of_replicas_per_host))

    dataset_initializer = inputs.dataset_initializer()

    tpu_ordinal_function_impl = ctx.tpu_ordinal_function(host_id)

    def device_function_impl(shard_id):
      if ctx.device_assignment is not None:
        # Find the replica_id of the host's logical core 0.
        # The current host_id is guaranteed to contain the logical core 0,
        # even when num_cores_per_replica > num_cores_per_host -- the function
        # caller makes sure that this host_id will must be receiving data (calls
        # input_fn).
        replica_id = ctx.device_assignment.lookup_replicas(
            task_id=host_id, logical_core=0)[shard_id]
        return ctx.tpu_host_placement_function(replica_id=replica_id)
      else:
        return None

  def enqueue_ops_fn():
    """Generates the per_host enqueue ops."""
    control_deps = []
    per_host_sharded_inputs = []
    enqueue_datas_list = []
    # Be aware that when num_cores_per_replica > num_cores_per_host,
    # ctx.num_of_replicas_per_host is 0.
    num_replicas_per_host = max(1, ctx.num_of_replicas_per_host)
    cached_signals = None
    with ops.device(device):
      if not inputs.is_dataset:
        raise TypeError('`input_fn` must return a `Dataset` for this mode.')
      for host in range(num_replicas_per_host):
        # Use control dependencies to ensure a deterministic ordering.
        with ops.control_dependencies(control_deps):
          features, labels = inputs.features_and_labels()  # Calls get_next()
          signals = inputs.signals()

          # All the replicas share the replica 0's stopping signal.
          # This avoids inconsistent state among different model replcias.
          if cached_signals:
            signals['stopping'] = cached_signals['stopping']
          else:
            cached_signals = signals

        features, labels, enqueue_data = (
            _tpu_estimator_embedding.split_inputs(ctx, features, labels))
        if len(enqueue_data) != 1:
          raise RuntimeError(
            'Missing or extra enqueue_data for host {}. len(enqueue_data) = {}.'
              .format(host, len(enqueue_data)))
        enqueue_datas_list.append(enqueue_data[0])

        inputs_structure_recorder.validate_and_record_structure(
            features, labels)
        flattened_inputs = (
            inputs_structure_recorder.flatten_features_and_labels(
                features, labels, signals))
        control_deps.extend(flattened_inputs)
        per_host_sharded_inputs.append(flattened_inputs)

      if inputs_structure_recorder.flattened_input_dims:
        input_partition_dims = inputs_structure_recorder.flattened_input_dims
        if signals:
          input_partition_dims += [None] * len(signals)
        # pylint: disable=protected-access
        infeed_queue = tpu_feed._PartitionedInfeedQueue(
            number_of_tuple_elements=len(per_host_sharded_inputs[0]),
            host_id=host_id,
            input_partition_dims=input_partition_dims,
            device_assignment=ctx.device_assignment)
        per_host_enqueue_ops = infeed_queue.generate_enqueue_ops(
            per_host_sharded_inputs)
      else:
        infeed_queue = tpu_feed.InfeedQueue(
            number_of_tuple_elements=len(per_host_sharded_inputs[0]))
        per_host_enqueue_ops = infeed_queue.generate_enqueue_ops(
            per_host_sharded_inputs,
            tpu_ordinal_function=tpu_ordinal_function_impl,
            placement_function=device_function_impl)

      captured_infeed_queue.capture(infeed_queue)

    if ctx.embedding_config:
      per_host_enqueue_ops.extend(
          ctx.embedding_config.tpu_embedding.generate_enqueue_ops(
              enqueue_datas_list))

    if signals is None:
      return per_host_enqueue_ops
    else:
      return {
          'ops': per_host_enqueue_ops,
          'signals': signals,
      }

  return enqueue_ops_fn, captured_infeed_queue, dataset_initializer


def generate_broadcast_enqueue_ops_fn(ctx, input_fn, inputs_structure_recorder,
                                      num_hosts):
  """Generates infeed enqueue ops for one input_fn on all the hosts."""
  captured_infeed_queue = _CapturedObject()
  dataset_initializer = None
  device_0 = ctx.tpu_host_placement_function(host_id=0)
  with ops.device(device_0):
    user_context = tpu_context.TPUContext(
        internal_ctx=ctx, input_device=device_0, invocation_index=0)
    inputs = _Inputs.from_input_fn(input_fn(user_context))

    is_dataset = inputs.is_dataset
    if ctx.mode == model_fn_lib.ModeKeys.PREDICT:
      if not is_dataset:
        raise TypeError(
            'For mode PREDICT, `input_fn` must return `Dataset` instead of '
            '`features` and `labels`.')

      inputs = _InputsWithStoppingSignals(
          dataset=inputs.dataset,
          batch_size=ctx.batch_size_for_input_fn,
          add_padding=True)

    if is_dataset:
      dataset_initializer = inputs.dataset_initializer()
    num_replicas_per_host = ctx.num_of_replicas_per_host

  def tpu_ordinal_function_impl(shard_id):
    if ctx.device_assignment:
      return ctx.device_assignment.tpu_ordinal(replica=shard_id)
    else:
      return shard_id % num_replicas_per_host

  def device_function_impl(shard_id):
    # shard_id ranges from 0 to num_of_replicas_per_host - 1.
    # A shard is a replica inside a host.
    # In broadcast mode (generate_broadcast_enqueue_ops_fn), the enqueue ops
    # are always executed on the first host. Thus shard_id equals to replica_id.
    return ctx.tpu_host_placement_function(replica_id=shard_id)

  def enqueue_ops_fn():
    """Generates enqueue ops for all the hosts."""
    broadcasted_inputs = []
    flattened_inputs = None  # Cache result from input_fn.
    signals = None
    num_replicas = ctx.num_replicas
    core_id = 0
    for host_id in xrange(num_hosts):
      with ops.device(ctx.tpu_host_placement_function(host_id=host_id)):
        for _ in xrange(ctx.num_of_replicas_per_host):
          # Note: input_fn is only called once at host 0 for the first replica.
          # The features and labels returned from that invocation are
          # broadcasted to other replicas(including the replicas on other
          # hosts).
          if flattened_inputs is None:
            features, labels = inputs.features_and_labels()  # Calls get_next()
            signals = inputs.signals()

            inputs_structure_recorder.validate_and_record_structure(
                features, labels)
            flattened_inputs = (
                inputs_structure_recorder.flatten_features_and_labels(
                    features, labels, signals))
            if (ctx.config.tpu_config.eval_training_input_configuration is
                tpu_config.InputPipelineConfig.SLICED):
              input_slices = [
                  array_ops.split(x, num_replicas) for x in flattened_inputs
              ]
          if (ctx.config.tpu_config.eval_training_input_configuration is
              tpu_config.InputPipelineConfig.SLICED):
            # for each core, slice out the flattened_inputs for each core.
            broadcasted_inputs.append([x[core_id] for x in input_slices])
            core_id += 1
          else:
            broadcasted_inputs.append(flattened_inputs)

    infeed_queue = tpu_feed.InfeedQueue(
        number_of_tuple_elements=len(broadcasted_inputs[0]))
    captured_infeed_queue.capture(infeed_queue)
    enqueue_ops = infeed_queue.generate_enqueue_ops(
        broadcasted_inputs,
        tpu_ordinal_function=tpu_ordinal_function_impl,
        placement_function=device_function_impl)

    if signals is None:
      return enqueue_ops
    else:
      return {
          'ops': enqueue_ops,
          'signals': signals,
      }

  return enqueue_ops_fn, captured_infeed_queue, dataset_initializer


class TensorPacker(object):
  """Pack and unpack small tensors into a big one for efficiency."""

  def __init__(self, small_feature_dim_size,
               minimum_num_small_features_to_group):
    self._small_feature_dim_size = small_feature_dim_size
    self._minimum_num_small_features_to_group = (
        minimum_num_small_features_to_group)

  def maybe_concatenate_features(self, features):
    """If there are enough small tensors, concat them for performance."""
    self._small_feature_names = {}
    self._small_feature_sizes = {}
    feature_names = _extract_key_names(features)
    if feature_names:  # Not a single tensor.
      # First pass: see if it is worth concatenating the small features.
      for name in feature_names:
        tensor = features[name]
        # We do not handle nested inputs here.
        if not isinstance(tensor, ops.Tensor):
          return
        shape = tensor.get_shape().as_list()
        dtype = tensor.dtype
        if (len(shape) == 2 and shape[1] is not None and
            shape[1] <= self._small_feature_dim_size):
          logging.info('Found small feature: %s %s', name, shape)
          if tensor.dtype not in self._small_feature_names:
            self._small_feature_names[dtype] = []
            self._small_feature_sizes[dtype] = []
          self._small_feature_names[dtype].append(name)
          self._small_feature_sizes[dtype].append(shape[1])

      dtypes_ = list(self._small_feature_names.keys())
      for dtype in dtypes_:
        # If we could find 5 (or more) [batch_size, 1] dense features,
        # we will group them.
        if (len(self._small_feature_names[dtype]) <
            self._minimum_num_small_features_to_group):
          self._small_feature_names.pop(dtype)  # reset
          self._small_feature_sizes.pop(dtype)  # reset

      # Second pass: separate small features out
      small_feature_tensors = {}
      for dtype in self._small_feature_names:
        small_feature_tensors[dtype] = []
        for name in self._small_feature_names[dtype]:
          small_feature_tensors[dtype].append(features.pop(name))

      # Add the concat Tensor to features with a special key.
      for dtype in self._small_feature_names:
        key = self._get_small_feature_key(dtype)
        if key in features:
          raise ValueError('{} is reserved as feature key for concatenated'
                           'small features.')
        features[key] = (array_ops.concat(small_feature_tensors[dtype], axis=1))

  def maybe_split_features(self, maybe_concatenated_features):
    for dtype in self._small_feature_names:
      key = self._get_small_feature_key(dtype)
      concatenated_small_features = maybe_concatenated_features.pop(key)
      splits = array_ops.split(
          concatenated_small_features, self._small_feature_sizes[dtype], axis=1)
      for name, split in zip(self._small_feature_names[dtype], splits):
        maybe_concatenated_features[name] = split

  def _get_small_feature_key(self, dtype):
    return _TENSOR_PACKER_CONCATENATED_SMALL_FEATURES_KEY + '_' + str(dtype)


class _InputPipeline(object):
  """`_InputPipeline` handles invoking `input_fn` and piping to infeed queue.

  `_InputPipeline` abstracts the per-core/per-host `input_fn` invocation from
  call site.  To be precise, based on the configuration in
  `_InternalTPUContext`,  it invokes `input_fn` for all cores (usually
  multi-host TPU training) or for one host (usually for single-host TPU
  evaluation), and sends all `features` and `labels` returned by `input_fn` to
  TPU infeed. For per-core invocation, `features` and `labels` are piped to
  infeed directly, one tuple for each core. For per-host invocation,  `features`
  and `labels` are split at host (with respect to `batch_axis`) and piped to all
  cores accordingly.

  In addition, flatten/unflatten are handled by `_InputPipeline` also.  Model
  inputs returned by the `input_fn` can have one of the following forms:
  1. features
  2. (features, labels)
  3. ((arbitrarily nested structure of features), labels)

  Internally, form 1 is reformed to `(features, None)` as features and labels
  are passed separately to underlying methods. For TPU training, TPUEstimator
  may expect multiple `features` and `labels` tuples one for each core.

  TPUEstimator allows various different structures for inputs (namely `features`
  and `labels`).  Both `features` and `labels` can be any nested sturcture
  supported by TF nest (namely, dict, tuples, namedtuples or any nested
  structure of such of Tensors).  `labels` could be `None` as well.

  These are flattened before they are passed to the infeed/outfeed library
  as that expectes flattend lists.
  """

  class InputsStructureRecorder(object):
    """The recorder to record inputs structure."""

    def __init__(self, input_partition_dims=None):
      # Holds the structure of inputs
      self._feature_structure = {}
      self._flattened_input_dims = None

      if input_partition_dims:
        # This should have been validated in TPUConfig.
        assert len(input_partition_dims) <= 2, 'must have 1 or 2 elements.'
        if len(input_partition_dims) == 2:
          self._feature_dims, self._label_dims = input_partition_dims
        else:
          self._feature_dims = input_partition_dims[0]
          self._label_dims = None

        assert self._feature_dims is not None, ('input_partition_dims[0] must '
                                                'not be None')
      else:
        self._feature_dims = None
        self._label_dims = None

      # Internal state.
      self._initialized = False

    @property
    def flattened_input_dims(self):
      assert self._initialized, 'InputsStructureRecorder is not initialized.'
      return self._flattened_input_dims

    def has_labels(self):
      return 'labels' in self._feature_structure

    def _flatten_input_dims(self, features, labels, feature_dims, label_dims):
      """Flatten input dims with the same order as flattened input tensors."""

      try:
        flattened_input_dims = data_nest.flatten_up_to(features, feature_dims)
      except TypeError as e:
        raise ValueError(
            'TPUConfig.input_partition_dims[0] mismatched the structure of'
            ' features. input_partition_dims[0]: {}, features {}. {}'.format(
                feature_dims, features, e))

      if labels is not None:
        if label_dims is not None:
          try:
            flattened_input_dims.extend(
                data_nest.flatten_up_to(labels, self._label_dims))
          except TypeError as e:
            raise ValueError(
                'TPUConfig.input_partition_dims[1] mismatched the structure of'
                ' labels. input_partition_dims[1]: {}, labels: {}. {}'.format(
                    label_dims, labels, e))
        else:
          num_label_tensors = len(data_nest.flatten(labels))
          flattened_input_dims.extend([None] * num_label_tensors)
      return flattened_input_dims

    def validate_and_record_structure(self, features, labels):
      """Validates and records the structure of `features` and `labels`."""
      # Extract structure.
      feature_names = _extract_key_names(features)
      label_names = _extract_key_names(labels)

      if not self._initialized:
        # Record structure.
        self._initialized = True
        if self._feature_dims is not None:
          feature_dims_names = _extract_key_names(self._feature_dims)
          if feature_dims_names != feature_names:
            raise ValueError(
                'TPUConfig.input_partition_dims[0] mismatched feature'
                ' keys. Expected {}, got {}'.format(feature_names,
                                                    feature_dims_names))
          label_dims_names = _extract_key_names(self._label_dims)
          if self._label_dims is not None and label_dims_names != label_names:
            raise ValueError(
                'TPUConfig.input_partition_dims[1] mismatched label'
                ' keys. Expected {}, got {}'.format(label_names,
                                                    label_dims_names))
          self._flattened_input_dims = self._flatten_input_dims(
              features, labels, self._feature_dims, self._label_dims)

    def flatten_features_and_labels(self, features, labels, signals=None):
      """Flattens the `features` and `labels` to a single tensor list."""
      self.tensor_packer = TensorPacker(
          _TENSOR_PACKER_SMALL_FEATURE_DIM_SIZE,
          _TENSOR_PACKER_MINIMUM_NUM_SMALL_FEATURES_TO_GROUP)
      self.tensor_packer.maybe_concatenate_features(features)
      self._feature_structure['features'] = features
      if labels is not None:
        self._feature_structure['labels'] = labels
      if signals is not None:
        self._feature_structure['signals'] = signals
      return data_nest.flatten(self._feature_structure)

    def unflatten_features_and_labels(self, flattened_inputs):
      """Restores the flattened inputs to original features and labels form.

      Args:
        flattened_inputs: Flattened inputs for each shard.

      Returns:
        A tuple of (`features`, `labels`), where `labels` could be None.
        Each one, if present, should have identical structure (single tensor vs
        dict) as the one returned by input_fn.

      Raises:
        ValueError: If the number of expected tensors from `flattened_inputs`
          mismatches the recorded structure.
      """

      unflattened_inputs = data_nest.pack_sequence_as(self._feature_structure,
                                                      flattened_inputs)
      features = unflattened_inputs['features']
      self.tensor_packer.maybe_split_features(features)
      return _Inputs(
          features,
          unflattened_inputs.get('labels'),
          signals=unflattened_inputs.get('signals'))

  def __init__(self, input_fn, batch_axis, ctx):
    """Constructor.

    Args:
      input_fn: input fn for train or eval.
      batch_axis: A python tuple of int values describing how each tensor
        produced by the Estimator `input_fn` should be split across the TPU
        compute shards.
      ctx: A `_InternalTPUContext` instance with mode.

    Raises:
      ValueError: If both `sharded_features` and `num_cores` are `None`.
    """
    self._inputs_structure_recorder = _InputPipeline.InputsStructureRecorder(
        ctx.input_partition_dims)

    self._sharded_per_core = ctx.is_input_sharded_per_core()
    self._input_fn = input_fn
    self._infeed_queue = None
    self._ctx = ctx
    self._batch_axis = batch_axis

  def generate_infeed_enqueue_ops_and_dequeue_fn(self):
    """Generates infeed enqueue ops and dequeue_fn."""
    # While tf.while_loop is called, the body function, which invokes
    # `enqueue_fn` passed in, is called to construct the graph. So, input_fn
    # structure is recorded.
    enqueue_ops, all_hooks, run_infeed_loop_on_coordinator = (
        self._invoke_input_fn_and_record_structure())

    self._validate_input_pipeline()

    def dequeue_fn():
      """dequeue_fn is used by TPU to retrieve the tensors."""
      # In the model-parallel case, both the host-side and device-side
      # computations must agree on the core on which infeed takes place. We
      # choose to perform infeed on logical core 0 of each replica.
      values = self._infeed_queue.generate_dequeue_op(tpu_device=0)
      # The unflatten process uses the structure information recorded above.
      return self._inputs_structure_recorder.unflatten_features_and_labels(
          values)

    return (enqueue_ops, dequeue_fn, all_hooks, run_infeed_loop_on_coordinator)

  def _invoke_input_fn_and_record_structure(self):
    """Deploys the input pipeline and record input structure."""
    enqueue_ops = []
    infeed_queues = []
    all_dataset_initializers = []
    num_hosts = self._ctx.num_hosts
    tpu_host_placement_fn = self._ctx.tpu_host_placement_function

    run_infeed_loop_on_coordinator = True

    if self._sharded_per_core:
      # Per-Core input pipeline deployment.
      # Invoke input pipeline for each core and placed on the corresponding
      # host.
      for host_id in range(num_hosts):
        host_device = tpu_host_placement_fn(host_id=host_id)
        with ops.device(host_device):
          with ops.name_scope('input_pipeline_task%d' % (host_id)):
            enqueue_ops_fn, captured_infeed_queue = (
                generate_per_core_enqueue_ops_fn_for_host(
                    self._ctx, self._input_fn, self._inputs_structure_recorder,
                    host_device, host_id))

            if _WRAP_INPUT_FN_INTO_WHILE_LOOP:
              run_infeed_loop_on_coordinator = False
              enqueue_ops.append(
                  _wrap_computation_in_while_loop(
                      device=host_device, op_fn=enqueue_ops_fn))
            else:
              enqueue_ops.append(enqueue_ops_fn())
            # Infeed_queue_getter must be called after enqueue_ops_fn is called.
            infeed_queues.append(captured_infeed_queue.get())

    elif self._ctx.is_input_broadcast_with_iterators():
      # Only calls input_fn in host 0.
      host_device = tpu_host_placement_fn(host_id=0)
      enqueue_ops_fn, captured_infeed_queue, dataset_initializer = (
          generate_broadcast_enqueue_ops_fn(self._ctx, self._input_fn,
                                            self._inputs_structure_recorder,
                                            num_hosts))
      if dataset_initializer:
        all_dataset_initializers.append(dataset_initializer)
        run_infeed_loop_on_coordinator = False
        wrap_fn = (
            _wrap_computation_in_while_loop
            if self._ctx.mode != model_fn_lib.ModeKeys.PREDICT else
            _wrap_computation_in_while_loop_with_stopping_signals)
        enqueue_ops.append(wrap_fn(device=host_device, op_fn=enqueue_ops_fn))
      else:
        enqueue_ops.append(enqueue_ops_fn())
      infeed_queues.append(captured_infeed_queue.get())

    else:
      # This branch handles two senarios:
      #       num_cores_per_replica > num_cores_per_host
      #   and num_cores_per_replica <= num_cores_per_host
      # First, get the set of host_ids, by iterating replicas.
      # We only want and will get the set of *unique* host_ids
      # *that will call input_fn*. For each replica, we only call the input_fn
      # from the CPU host that contains logical core 0.
      host_device_ids = set()
      for replica_id in xrange(self._ctx.num_replicas):
        host_device, _ = self._ctx.device_for_replica(replica_id)
        # TODO(lehou): Get host_id in a better way.
        host_id = int(host_device.split('/task:')[1].split('/device:')[0])
        host_device_ids.add(host_id)

      for host_id in host_device_ids:
        host_device = tpu_host_placement_fn(host_id=host_id)
        with ops.device(host_device):
          with ops.name_scope('input_pipeline_task%d' % (host_id)):
            if self._ctx.is_input_per_host_with_iterators():
              enqueue_ops_fn, captured_infeed_queue, dataset_initializer = (
                  generate_per_host_v2_enqueue_ops_fn_for_host(
                      self._ctx, self._input_fn,
                      self._inputs_structure_recorder, host_device, host_id))
            else:
              enqueue_ops_fn, captured_infeed_queue, dataset_initializer = (
                  generate_per_host_enqueue_ops_fn_for_host(
                      self._ctx, self._input_fn,
                      self._inputs_structure_recorder, self._batch_axis,
                      host_device, host_id))

            # NOTE(xiejw): We dispatch here based on the return type of the
            # users `input_fn`.
            #
            # 1. If input_fn returns a Dataset instance, we initialize the
            # iterator outside of tf.while_loop, and call the iterator.get_next
            # inside tf.while_loop.  This should be always safe.
            #
            # 2. If input_fn returns (features, labels), it is too late to wrap
            # them inside tf.while_loop, as resource initialization cannot be
            # handled in TF control flow properly. In this case, we will use
            # python loop to enqueue the data into TPU system.  This may be
            # slow compared to the previous case.
            if dataset_initializer:
              all_dataset_initializers.append(dataset_initializer)
              run_infeed_loop_on_coordinator = False
              wrap_fn = (
                  _wrap_computation_in_while_loop
                  if self._ctx.mode != model_fn_lib.ModeKeys.PREDICT else
                  _wrap_computation_in_while_loop_with_stopping_signals)
              enqueue_ops.append(
                  wrap_fn(device=host_device, op_fn=enqueue_ops_fn))
            else:
              enqueue_ops.append(enqueue_ops_fn())
            infeed_queues.append(captured_infeed_queue.get())

    # infeed_queue is used to generate dequeue ops. The only thing it uses for
    # dequeue is dtypes and types. So, any one can be used. Here, grab the
    # first one.
    self._infeed_queue = infeed_queues[0]
    return enqueue_ops, [
        util_lib.MultiHostDatasetInitializerHook(all_dataset_initializers)
    ], run_infeed_loop_on_coordinator

  def _validate_input_pipeline(self):
    """Validates the input pipeline.

    Perform some sanity checks to log user friendly information. We should
    error out to give users better error message. But, if
    _WRAP_INPUT_FN_INTO_WHILE_LOOP is False (legacy behavior), we cannot break
    user code, so, log a warning.

    Raises:
      RuntimeError: If the validation failed.
    """
    if ops.get_default_graph().get_collection(ops.GraphKeys.QUEUE_RUNNERS):
      err_msg = ('Input pipeline contains one or more QueueRunners. '
                 'It could be slow and not scalable. Please consider '
                 'converting your input pipeline to use `tf.data` instead (see '
                 'https://www.tensorflow.org/guide/datasets for '
                 'instructions.')
      if _WRAP_INPUT_FN_INTO_WHILE_LOOP:
        raise RuntimeError(err_msg)
      else:
        logging.warn(err_msg)


def call_computation(computation_inputs,
                     computation,
                     batch_config=None):
  """Call computation.

  Args:
    computation_inputs: A tensor or dict of tensors, the inputs to the
      computation.
    computation: A Python function that takes no inputs and builds computation
      graph. If `computation` returns m outputs, this function will return a
      list of m Tensors.
    batch_config: A BatchConfig named tuple specifying the batching
      configuration to use for inference batching.

  Returns:
    A list of output tensors.
  """
  # Using `TPUPartitionedCall` makes it possible to target a different
  # TPU core with every `Session.run()` call. Note that the entire inference
  # graph executes on a single core, and that invocations of this graph
  # will round-robin among the cores attached to a host.
  def tpu_partitioned_call(partition_inputs):

    # capture_resource_var_by_value enables variables to be mirrored on TPU
    # to avoid fetching from CPU, since variables do not change during
    # inference.
    @function.Defun(capture_resource_var_by_value=False)
    def tpu_subgraph():
      return computation(partition_inputs)

    return tpu_functional.TPUPartitionedCall(
        args=tpu_subgraph.captured_inputs,
        device_ordinal=tpu_ops.tpu_ordinal_selector(),
        Tout=[o.type for o in tpu_subgraph.definition.signature.output_arg],
        f=tpu_subgraph)

  # Not using Batching Function but use TPUPartitionedCall/all cores.
  if not batch_config:
    return tpu_partitioned_call(computation_inputs)

  # Use Batching Function and TPUPartitionedCall/all cores.
  # Note that BatchingFunction requires a list of tensors and doesn't support
  # a dict of tensors. So we preserve the structure by deterministically
  # flattening the dict before batching and then recomposing it after batching
  # to feed into the computation.
  ordered_inputs_list = nest.flatten(computation_inputs)

  @batch_ops.batch_function(
      num_batch_threads=batch_config.num_batch_threads,
      max_batch_size=batch_config.max_batch_size,
      batch_timeout_micros=batch_config.batch_timeout_micros,
      allowed_batch_sizes=batch_config.allowed_batch_sizes,
      max_enqueued_batches=batch_config.max_enqueued_batches,
      autograph=False)
  def batched_tpu_computation(*tensor_args):
    """Recompose the input feature dict and calls the TPU computation."""
    computation_feature_input = nest.pack_sequence_as(computation_inputs,
                                                      tensor_args)
    return tpu_partitioned_call(computation_feature_input)

  return batched_tpu_computation(*ordered_inputs_list)


class _ModelFnWrapper(object):
  """A `model_fn` wrapper.

  This makes calling model_fn on CPU and TPU easier and more consistent and
  performs necessary check and mutation required by TPU training and evaluation.

  In addition, this wrapper manages converting the `model_fn` to a single TPU
  train and eval step.
  """

  def __init__(self, model_fn, config, params, ctx):
    self._model_fn = model_fn
    self._config = config
    self._params = params
    self._ctx = ctx

  def call_without_tpu(self, features, labels, is_export_mode):
    return self._call_model_fn(features, labels, is_export_mode=is_export_mode)

  def _add_embedding_features(self, features, hook_dummy_table_variables):
    """Add embedding features, optionally add hook to intercept gradient."""
    if self._ctx.embedding_config:
      tpu_embedding_ = self._ctx.embedding_config.tpu_embedding
      embedding_activations = tpu_embedding_.get_activations()
      if hook_dummy_table_variables:
        new_embedding_activations = (
            tpu_embedding_gradient.hook_dummy_table_variables_to_activations(
                tpu_embedding_, embedding_activations,
                self._ctx.embedding_config.dummy_table_variables))
        features.update(new_embedding_activations)
      else:
        features.update(embedding_activations)

  def convert_to_single_tpu_train_step(self, dequeue_fn):
    """Converts user provided model_fn` as a single train step on TPU.

    The user provided `model_fn` takes input tuple
    (features, labels) and produces the EstimatorSpec with train_op and loss for
    train `mode`. This usually represents a single train computation on CPU.

    For TPU training, a train (computation) step is first wrapped in a
    tf.while_loop control flow to repeat for many times and then replicated to
    all TPU shards. Besides the input should be taken from TPU infeed rather
    than input pipeline (input_fn) directly. To fit TPU loop and replicate
    pattern, the original train computation should be reformed, which is the
    returned `train_step`.

    Args:
      dequeue_fn: The function to retrieve inputs, features and labels, from TPU
        infeed dequeue channel.

    Returns:
      A tuple of train_fn, host_calls, and captured scaffold_fn. The train_fn
      representing the train step for TPU.
    """

    host_call = _OutfeedHostCall(
        self._ctx, outfeed_every_n_steps=self._config.tpu_config
        .experimental_host_call_every_n_steps)
    captured_scaffold_fn = _CapturedObject()
    captured_training_hooks = _CapturedObject()

    def train_step(step):
      """Training step function for use inside a while loop."""
      inputs = dequeue_fn()
      features, labels = inputs.features_and_labels()
      self._add_embedding_features(features, True)

      estimator_spec = self._verify_estimator_spec(
          self._call_model_fn(features, labels))
      loss, train_op = estimator_spec.loss, estimator_spec.train_op

      if tensor_tracer.TensorTracer.is_enabled():
        tt = tensor_tracer.TensorTracer()
        loss = tt.trace_tpu(ops.get_default_graph(), loss, train_op,
                            self._ctx.num_replicas)
        tracer_host_call = tt.host_call_deps_and_fn()
      else:
        tracer_host_call = {}

      if isinstance(estimator_spec, model_fn_lib._TPUEstimatorSpec):  # pylint: disable=protected-access
        captured_scaffold_fn.capture(estimator_spec.scaffold_fn)
      else:
        captured_scaffold_fn.capture(None)

      captured_training_hooks.capture(estimator_spec.training_hooks)

      if self._ctx.embedding_config is None:
        apply_sparse_grads = []
      else:
        tpu_embedding_ = self._ctx.embedding_config.tpu_embedding
        gradients = (
            tpu_embedding_gradient.get_gradients_through_dummy_table_variables(
                tpu_embedding_)
        )
        grad_multiplier = self._ctx.embedding_config.get_grad_multiplier()
        if grad_multiplier is not None:
          scaled_gradients = collections.OrderedDict(
              (k, v * grad_multiplier) for k, v in six.iteritems(gradients))
        else:
          scaled_gradients = gradients
        apply_sparse_grads = [
            tpu_embedding_.generate_send_gradients_op(scaled_gradients,
                                                      training.get_global_step())
        ]

      # We must run train_op to update the variables prior to running the
      # outfeed.
      with ops.control_dependencies([train_op] + apply_sparse_grads):
        host_call_outfeed_ops = []
        host_call_fn, host_call_args = None, []

        if (isinstance(estimator_spec, model_fn_lib._TPUEstimatorSpec)  # pylint: disable=protected-access
            and estimator_spec.host_call is not None):
          host_call_fn, host_call_args = estimator_spec.host_call

        if host_call_fn:
          # Ignore dummy hostcalls (no arguments)
          if host_call_args:
            tracer_host_call.update({'host_call': estimator_spec.host_call})
            host_call.record(tracer_host_call)
            host_call_outfeed_ops = host_call.create_enqueue_op(step)
          elif tracer_host_call:
            host_call.record(tracer_host_call)
            host_call_outfeed_ops = host_call.create_enqueue_op(step)
        else:
          # Create a host call for the loss to track execution progress
          # Without this, we don't have any indication of the state of the
          # TPU program.
          tracer_host_call.update({
              'host_call': (lambda loss_t: loss_t,
                            [array_ops.reshape(loss, [1])])
          })
          host_call.record(tracer_host_call)
          host_call_outfeed_ops = host_call.create_enqueue_op(step)

        with ops.control_dependencies(host_call_outfeed_ops):
          return array_ops.identity(loss)

    return (train_step, host_call, captured_scaffold_fn,
            captured_training_hooks)

  def convert_to_single_tpu_eval_step(self, dequeue_fn):
    """Converts user provided model_fn` as a single eval step on TPU.

    Similar to training, the user provided `model_fn` takes input tuple
    (features, labels) and produces the TPUEstimatorSpec with eval_metrics for
    eval `mode`. This usually represents a single evaluation computation on CPU.

    For TPU evaluation, a eval (computation) step is first wrapped in a
    tf.while_loop control flow to repeat for many times and then replicated to
    all TPU shards. Besides the input and output are slightly different. Input,
    features and labels, should be taken from TPU infeed rather than input
    pipeline (input_fn) directly. Output is managed in two stages.  First, the
    model outputs as the result of evaluation computation, usually model logits,
    should be transferred from TPU system to CPU. Then, all model outputs are
    concatenated first on CPU and sent to the metric_fn for metrics computation.
    To fit TPU evaluation pattern, the original eval computation should be
    reformed, which is the returned `eval_step`.

    Args:
      dequeue_fn: The function to retrieve inputs, features and labels, from TPU
        infeed dequeue channel.

    Returns:
      A tuple of eval_fn, host_calls, and captured scaffold_fn. The eval_fn
      representing the eval step for TPU.
    """
    host_calls = _OutfeedHostCall(self._ctx)
    captured_scaffold_fn = _CapturedObject()
    captured_eval_hooks = _CapturedObject()

    def eval_step(total_loss):
      """Evaluation step function for use inside a while loop."""
      inputs = dequeue_fn()
      features, labels = inputs.features_and_labels()
      self._add_embedding_features(features, False)

      tpu_estimator_spec = self._call_model_fn(features, labels)
      if not isinstance(tpu_estimator_spec, model_fn_lib._TPUEstimatorSpec):  # pylint: disable=protected-access
        raise RuntimeError(
            'estimator_spec used by TPU evaluation must have type'
            '`TPUEstimatorSpec`. Got {}'.format(type(tpu_estimator_spec)))

      loss = tpu_estimator_spec.loss
      captured_scaffold_fn.capture(tpu_estimator_spec.scaffold_fn)
      captured_eval_hooks.capture(tpu_estimator_spec.evaluation_hooks)

      to_record = {}
      if tpu_estimator_spec.eval_metrics:
        to_record['eval_metrics'] = tpu_estimator_spec.eval_metrics
      if tpu_estimator_spec.host_call is not None:
        # We assume that evaluate won't update global step, so we don't wrap
        # this host_call.
        to_record['host_call'] = tpu_estimator_spec.host_call
      host_calls.record(to_record)

      with ops.control_dependencies(host_calls.create_enqueue_op()):
        return math_ops.add(total_loss, loss)

    return eval_step, host_calls, captured_scaffold_fn, captured_eval_hooks

  def convert_to_single_tpu_predict_step(self, dequeue_fn):
    """Converts user provided model_fn` as a single predict step on TPU.

    Args:
      dequeue_fn: The function to retrieve inputs, features and labels, from TPU
        infeed dequeue channel.

    Returns:
      A tuple of predict_fn, host_calls, and captured scaffold_fn. The
      predict_fn representing the predict step for TPU.
    """
    host_calls = _OutfeedHostCall(self._ctx)
    captured_scaffold_fn = _CapturedObject()
    captured_predict_hooks = _CapturedObject()

    def predict_step(unused_scalar_stopping_signal):
      """Evaluation step function for use inside a while loop."""
      inputs = dequeue_fn()
      features, labels = inputs.features_and_labels()
      stopping_signals = inputs.signals()

      assert stopping_signals is not None, (
          'Internal Error: `signals` is missing.')

      tpu_estimator_spec = self._call_model_fn(
          features, labels, is_export_mode=False)
      if not isinstance(tpu_estimator_spec, model_fn_lib._TPUEstimatorSpec):  # pylint: disable=protected-access
        raise RuntimeError(
            'estimator_spec used by TPU prediction must have type'
            '`TPUEstimatorSpec`. Got {}'.format(type(tpu_estimator_spec)))

      self._verify_tpu_spec_predictions(tpu_estimator_spec.predictions)

      captured_scaffold_fn.capture(tpu_estimator_spec.scaffold_fn)
      captured_predict_hooks.capture(tpu_estimator_spec.prediction_hooks)
      to_record = {}
      identity_fn = lambda **kwargs: kwargs
      to_record['predictions'] = [identity_fn, tpu_estimator_spec.predictions]
      to_record['signals'] = [identity_fn, stopping_signals]
      if tpu_estimator_spec.host_call is not None:
        to_record['host_call'] = tpu_estimator_spec.host_call
      host_calls.record(to_record)

      with ops.control_dependencies(host_calls.create_enqueue_op()):
        return _StopSignals.as_scalar_stopping_signal(stopping_signals)

    return (predict_step, host_calls, captured_scaffold_fn,
            captured_predict_hooks)

  def _verify_tpu_spec_predictions(self, predictions):
    """Validates TPUEstimatorSpec.predictions dict."""
    # TODO(xiejw): Adds validation for prediction dictionrary.
    # TODO(xiejw): Adds support for single tensor as predictions.
    if not isinstance(predictions, dict):
      raise TypeError('TPUEstimatorSpec.predictions must be dict of Tensors.')

    for (key, tensor) in predictions.items():
      if tensor.shape.dims[0].value is None:
        raise ValueError(
            'The tensor with key ({}) in TPUEstimatorSpec.predictions has '
            'dynamic shape (should be static). Tensor: {}'.format(key, tensor))
    return predictions

  def _validate_model_features_and_labels(self, features, labels,
                                          is_export_mode):
    """Validates that the features and labels for the model function are valid.

    A valid features/labels object is the one with:
    - Type: A tensor or any nested structure of tensors supported by TF nest,
        namely nested dictionary, tuple, namedtuple, or sequence of tensors.
    - Static shape if is_export_mode is False.

    Args:
      features: the features that would be input to the model function.
      labels: the labels that would be input to the model function.
      is_export_mode: boolean value specifying if in export mode.

    Raises:
      TypeError: If features/labels are not of the correct type.
      ValueError: If features/labels have dynamic shape.
    """

    def validate(obj, obj_name):
      """Helper validate function."""
      if is_export_mode or self._ctx.is_running_on_cpu(is_export_mode):
        return
      if isinstance(obj, ops.Tensor):
        if not obj.get_shape().is_fully_defined():
          raise ValueError(
              'The {} to the model returned by input_fn must have static shape.'
              ' Tensor: {}'.format(obj_name, obj))
      else:
        for tensor in data_nest.flatten(obj):
          if not tensor.get_shape().is_fully_defined():
            raise ValueError(
                ('The {} to the model returned by input_fn must have static '
                 'shape. Tensor: {}').format(obj_name, tensor))

    validate(features, 'features')
    if labels is not None:
      validate(labels, 'labels')

  def _call_model_fn(self, features, labels, is_export_mode=False):
    """Calls the model_fn with required parameters."""
    self._validate_model_features_and_labels(features, labels, is_export_mode)
    model_fn_args = function_utils.fn_args(self._model_fn)
    kwargs = {}

    # Makes deep copy with `config` and params` in case user mutates them.
    config = copy.deepcopy(self._config)
    params = copy.deepcopy(self._params)

    if 'labels' in model_fn_args:
      kwargs['labels'] = labels
    elif labels is not None:
      raise ValueError(
          'model_fn does not take labels, but input_fn returns labels.')
    if 'mode' in model_fn_args:
      kwargs['mode'] = self._ctx.mode
    if 'config' in model_fn_args:
      kwargs['config'] = config
    if 'params' in model_fn_args:
      kwargs['params'] = params

    if 'params' not in model_fn_args:
      raise ValueError('model_fn ({}) does not include params argument, '
                       'required by TPUEstimator to pass batch size as '
                       'params[\'batch_size\']'.format(self._model_fn))

    if is_export_mode:
      batch_size_for_model_fn = None
    else:
      batch_size_for_model_fn = self._ctx.batch_size_for_model_fn

    if batch_size_for_model_fn is not None:
      _add_item_to_params(params, _BATCH_SIZE_KEY, batch_size_for_model_fn)

    running_on_cpu = self._ctx.is_running_on_cpu(is_export_mode)
    # In export mode, params['use_tpu'] has already been set based on mode
    # (i.e. True for _REWRITE_FOR_INFERENCE_MODE, False otherwise).
    if not is_export_mode:
      _add_item_to_params(params, _USE_TPU_KEY, not running_on_cpu)

    if not running_on_cpu:
      user_context = tpu_context.TPUContext(
          internal_ctx=self._ctx, call_from_input_fn=False)
      _add_item_to_params(params, _CTX_KEY, user_context)

    estimator_spec = self._model_fn(features=features, **kwargs)
    if (running_on_cpu and
        isinstance(estimator_spec, model_fn_lib._TPUEstimatorSpec)):  # pylint: disable=protected-access
      # The estimator_spec will be passed to `Estimator` directly, which expects
      # type `EstimatorSpec`. As we are running on the CPU, escape
      # the TPUInferenceContext.
      graph_context = ops.get_default_graph()._get_control_flow_context()
      try:
        if isinstance(graph_context, tpu._TPUInferenceContext):
          ops.get_default_graph()._set_control_flow_context(
              graph_context.outer_context)
        return estimator_spec.as_estimator_spec()
      finally:
        ops.get_default_graph()._set_control_flow_context(
            graph_context)
    else:
      return estimator_spec

  def _verify_estimator_spec(self, estimator_spec):
    """Validates the estimator_spec."""
    if isinstance(estimator_spec, model_fn_lib._TPUEstimatorSpec):  # pylint: disable=protected-access
      return estimator_spec

    err_msg = '{} returned by EstimatorSpec is not supported in TPUEstimator.'
    if estimator_spec.training_chief_hooks:
      raise ValueError(
          err_msg.format('training_chief_hooks') + 'If you want' +
          ' to pass training hooks, please pass via training_hooks.')

    if estimator_spec.scaffold:
      logging.warning('EstimatorSpec.Scaffold is ignored by TPU train/eval. '
                      'Please use TPUEstimatorSpec.')
    return estimator_spec


class _OutfeedHostCall(object):
  """Support for `eval_metrics` and `host_call` in TPUEstimatorSpec."""

  def __init__(self, ctx, outfeed_every_n_steps=1):
    self._ctx = ctx
    self._names = []
    # All of these are dictionaries of lists keyed on the name.
    self._host_fns = {}
    self._tensor_keys = collections.defaultdict(list)
    self._tensors = collections.defaultdict(list)
    self._tensor_dtypes = collections.defaultdict(list)
    self._tensor_shapes = collections.defaultdict(list)
    self._outfeed_every_n_steps = outfeed_every_n_steps

  @staticmethod
  def validate(host_calls):
    """Validates the `eval_metrics` and `host_call` in `TPUEstimatorSpec`."""

    for name, host_call in host_calls.items():
      if not isinstance(host_call, (tuple, list)):
        raise ValueError('{} should be tuple or list'.format(name))
      if len(host_call) != 2:
        raise ValueError('{} should have two elements.'.format(name))
      if not callable(host_call[0]):
        raise TypeError('{}[0] should be callable.'.format(name))
      if not isinstance(host_call[1], (tuple, list, dict)):
        raise ValueError('{}[1] should be tuple or list, or dict.'.format(name))

      if isinstance(host_call[1], (tuple, list)):
        fullargspec = tf_inspect.getfullargspec(host_call[0])
        fn_args = function_utils.fn_args(host_call[0])
        # wrapped_hostcall_with_global_step uses varargs, so we allow that.
        if fullargspec.varargs is None and len(host_call[1]) != len(fn_args):
          raise RuntimeError(
              'In TPUEstimatorSpec.{}, length of tensors {} does not match '
              'method args of the function, which takes {}.'.format(
                  name, len(host_call[1]), len(fn_args)))

  @staticmethod
  def create_cpu_hostcall(host_calls):
    """Runs on the host_call on CPU instead of TPU when use_tpu=False."""

    _OutfeedHostCall.validate(host_calls)
    ret = {}
    for name, host_call in host_calls.items():
      host_fn, tensors = host_call
      if isinstance(tensors, (tuple, list)):
        ret[name] = host_fn(*tensors)
      else:
        # Must be dict.
        try:
          ret[name] = host_fn(**tensors)
        except TypeError as e:
          logging.warning(
              'Exception while calling %s: %s. It is likely the tensors '
              '(%s[1]) do not match the '
              'function\'s arguments', name, e, name)
          raise
    return ret

  def record(self, host_calls):
    """Records the host_call structure."""

    for name, host_call in host_calls.items():
      host_fn, tensor_list_or_dict = host_call
      self._names.append(name)
      self._host_fns[name] = host_fn

      if isinstance(tensor_list_or_dict, dict):
        for (key, tensor) in six.iteritems(tensor_list_or_dict):
          self._tensor_keys[name].append(key)
          self._tensors[name].append(tensor)
          self._tensor_dtypes[name].append(tensor.dtype)
          self._tensor_shapes[name].append(tensor.shape)
      else:
        # List or tuple.
        self._tensor_keys[name] = None
        for tensor in tensor_list_or_dict:
          self._tensors[name].append(tensor)
          self._tensor_dtypes[name].append(tensor.dtype)
          self._tensor_shapes[name].append(tensor.shape)

  def create_enqueue_op(self, step=None):
    """Create the op to enqueue the recorded host_calls.

    Returns:
      A list of enqueue ops, which is empty if there are no host calls.
    """
    if not self._names:
      return []

    tensors = []
    # TODO(jhseu): Consider deduping tensors.
    for name in self._names:
      tensors.extend(self._tensors[name])

    if self._outfeed_every_n_steps > 1 and step is None:
      raise ValueError('If outfeed is requested every n steps, you must pass '
                       'a tensor whose value is the step number within the '
                       'current training loop.')
    with ops.device(tpu.core(0)):
      if self._outfeed_every_n_steps == 1:
        return [tpu_ops.outfeed_enqueue_tuple(tensors)]
      else:
        return [control_flow_ops.cond(
            math_ops.equal(math_ops.mod(step, self._outfeed_every_n_steps), 0),
            lambda: tpu_ops.outfeed_enqueue_tuple(tensors),
            lambda: control_flow_ops.no_op())]


  def create_tpu_hostcall(self):
    """Sends the tensors through outfeed and runs the host_fn on CPU.

    The tensors are concatenated along dimension 0 to form a global tensor
    across all shards. The concatenated function is passed to the host_fn and
    executed on the first host.

    Returns:
      A dictionary mapping name to the return type of the host_call by that
      name.

    Raises:
      RuntimeError: If outfeed tensor is scalar.
    """
    if not self._names:
      return {}

    ret = {}
    # For each i, dequeue_ops[i] is a list containing the tensors from all
    # shards. This list is concatenated later.
    dequeue_ops = []
    tensor_dtypes = []
    tensor_shapes = []
    for name in self._names:
      for _ in self._tensors[name]:
        dequeue_ops.append([])
      for dtype in self._tensor_dtypes[name]:
        tensor_dtypes.append(dtype)
      for shape in self._tensor_shapes[name]:
        tensor_shapes.append(shape)

    # Outfeed ops execute on each replica's first logical core. Note: we must
    # constraint it such that we have at most one outfeed dequeue and enqueue
    # per replica.
    for i in xrange(self._ctx.num_replicas):
      host_device, ordinal_id = self._ctx.device_for_replica(i)
      with ops.device(host_device):
        outfeed_tensors = tpu_ops.outfeed_dequeue_tuple(
            dtypes=tensor_dtypes,
            shapes=tensor_shapes,
            device_ordinal=ordinal_id)
        for j, item in enumerate(outfeed_tensors):
          dequeue_ops[j].append(item)

    # Deconstruct dequeue ops.
    flat_dequeue_ops = []
    for l in dequeue_ops:
      flat_dequeue_ops.extend(l)

    dequeue_ops_by_name = {}
    pos = 0
    for name in self._names:
      dequeue_ops_by_name[name] = dequeue_ops[pos:pos +
                                              len(self._tensors[name])]
      pos += len(self._tensors[name])

    def _call_host_fn(fn, *args, **kw):
      context = CatchInvalidHostcallFunctions()
      context.Enter()
      result = fn(*args, **kw)
      context.Exit()
      context.ExitResult(result)
      return result

    # It is assumed evaluation always happens on single host TPU system. So,
    # place all ops on tpu host if possible.
    #
    # TODO(jhseu): Evaluate whether this is right for summaries.
    with ops.device(self._ctx.tpu_host_placement_function(replica_id=0)):
      for name in self._names:
        dequeue_ops = dequeue_ops_by_name[name]
        for i, item in enumerate(dequeue_ops):
          # TODO(xiejw): Make the specification of the outfeed combinaton
          # function more explicit and well-documented.  We may want to give the
          # user the option of concatenating along any axis.
          if (self._ctx.config.tpu_config.per_host_input_for_training is
              tpu_config.InputPipelineConfig.BROADCAST):
            # If the infeed is in BROADCAST mode (each core recieving the same
            # input), then we assume that the cores also produce identical
            # copies of the same output, and we simply take the output from
            # the first core.  This mode is used by Mesh-TensorFlow.
            with ops.control_dependencies(dequeue_ops[i]):
              dequeue_ops[i] = array_ops.identity(dequeue_ops[i][0])
          else:
            if dequeue_ops[i][0].shape.ndims == 0:
              raise RuntimeError(
                  'All tensors outfed from TPU should preserve batch size '
                  'dimension, but got scalar {}'.format(dequeue_ops[i][0]))
            # Assume that the input has been batch-split and that axis 0 of the
            # output tensors represents the batch size.  Concatenate along
            # the axis 0 to re-combine the batch.
            dequeue_ops[i] = array_ops.concat(dequeue_ops[i], axis=0)

        if self._tensor_keys[name] is not None:
          # The user-provided eval_metrics[1] is a dict.
          dequeue_ops = dict(zip(self._tensor_keys[name], dequeue_ops))
          try:
            ret[name] = _call_host_fn(self._host_fns[name], **dequeue_ops)
          except TypeError as e:
            logging.warning(
                'Exception while calling %s: %s. It is likely the tensors '
                '(%s[1]) do not match the '
                'function\'s arguments', name, e, name)
            raise
        else:
          ret[name] = _call_host_fn(self._host_fns[name], *dequeue_ops)

    # force all dequeue operations to be run if not consumed by the host calls
    ret['__force_dequeue'] = control_flow_ops.group(*flat_dequeue_ops)
    return ret


class _OutfeedHostCallHook(session_run_hook.SessionRunHook):
  """Hook to run host calls when use_tpu=False."""

  def __init__(self, tensors):
    self._tensors = tensors

  def begin(self):
    # We duplicate this code from the TPUInfeedOutfeedSessionHook rather than
    # create a separate hook to guarantee execution order, because summaries
    # need to be initialized before the outfeed thread starts.
    # TODO(jhseu): Make a wrapper hook instead?
    self._init_ops = contrib_summary.summary_writer_initializer_op()
    # Get all the writer resources from the initializer, so we know what to
    # flush.
    self._finalize_ops = []
    for op in self._init_ops:
      self._finalize_ops.append(contrib_summary.flush(writer=op.inputs[0]))

  def after_create_session(self, session, coord):
    session.run(self._init_ops)

  def before_run(self, run_context):
    return basic_session_run_hooks.SessionRunArgs(self._tensors)

  def end(self, session):
    session.run(self._finalize_ops)


class ExamplesPerSecondHook(basic_session_run_hooks.StepCounterHook):
  """Calculate and report global_step/sec and examples/sec during runtime."""

  def __init__(self,
               batch_size,
               every_n_steps=100,
               every_n_secs=None,
               output_dir=None,
               summary_writer=None):
    self._batch_size = batch_size
    super(ExamplesPerSecondHook, self).__init__(
        every_n_steps=every_n_steps,
        every_n_secs=every_n_secs,
        output_dir=output_dir,
        summary_writer=summary_writer)

  def _log_and_record(self, elapsed_steps, elapsed_time, global_step):
    global_step_per_sec = elapsed_steps / elapsed_time
    examples_per_sec = self._batch_size * global_step_per_sec
    if self._summary_writer is not None:
      global_step_summary = Summary(value=[
          Summary.Value(tag='global_step/sec', simple_value=global_step_per_sec)
      ])
      example_summary = Summary(value=[
          Summary.Value(tag='examples/sec', simple_value=examples_per_sec)
      ])
      self._summary_writer.add_summary(global_step_summary, global_step)
      self._summary_writer.add_summary(example_summary, global_step)
    logging.info('global_step/sec: %g', global_step_per_sec)
    logging.info('examples/sec: %g', examples_per_sec)


class InstallSignalHandlerHook(session_run_hook.SessionRunHook):
  """Change SIGINT (CTRL^C) handler to force quit the process.

  The default behavior often results in hanging processes.
  The original handler is restored after training/evaluation.
  """

  def __init__(self):
    self._signal_fn = signal.getsignal(signal.SIGINT)

  def before_run(self, run_context):
    signal.signal(signal.SIGINT, signal.SIG_DFL)

  def end(self, session):
    signal.signal(signal.SIGINT, self._signal_fn)


class ExportSavedModelApiVersion(enum.Enum):
  V1 = 1
  V2 = 2


class BatchConfig(
    collections.namedtuple('BatchConfig', [
        'num_batch_threads', 'max_batch_size', 'batch_timeout_micros',
        'allowed_batch_sizes', 'max_enqueued_batches'
    ])):
  """Class to handle config inputs into the batching function."""

  def __new__(cls,
              num_batch_threads,
              max_batch_size,
              batch_timeout_micros,
              allowed_batch_sizes,
              max_enqueued_batches=10):
    """Creates an BatchConfig instance.

    Args:
     num_batch_threads: Number of scheduling threads for processing batches of
       work. Determines the number of batches processed in parallel.
      max_batch_size: Batch sizes will never be bigger than this.
      batch_timeout_micros: Maximum number of microseconds to wait before
        outputting an incomplete batch.
      allowed_batch_sizes: Optional list of allowed batch sizes. If left empty,
        does nothing. Otherwise, supplies a list of batch sizes, causing the op
        to pad batches up to one of those sizes. The entries must increase
        monotonically, and the final entry must equal max_batch_size.
      max_enqueued_batches: The maximum depth of the batch queue. Defaults to
        10.

    Returns:
      An BatchConfig instance.
    """
    return super(BatchConfig, cls).__new__(
        cls,
        num_batch_threads=num_batch_threads,
        max_batch_size=max_batch_size,
        batch_timeout_micros=batch_timeout_micros,
        allowed_batch_sizes=allowed_batch_sizes,
        max_enqueued_batches=max_enqueued_batches)


@estimator_export(v1=['estimator.tpu.TPUEstimator'])
class TPUEstimator(estimator_lib.Estimator):
  """Estimator with TPU support.

  TPUEstimator also supports training on CPU and GPU. You don't need to define
  a separate `tf.estimator.Estimator`.

  TPUEstimator handles many of the details of running on TPU devices, such as
  replicating inputs and models for each core, and returning to host
  periodically to run hooks.

  TPUEstimator transforms a global batch size in params to a per-shard batch
  size when calling the `input_fn` and `model_fn`. Users should specify
  global batch size in constructor, and then get the batch size for each shard
  in `input_fn` and `model_fn` by `params['batch_size']`.

  - For training, `model_fn` gets per-core batch size; `input_fn` may get
    per-core or per-host batch size depending on `per_host_input_for_training`
    in `TPUConfig` (See docstring for TPUConfig for details).

  - For evaluation and prediction, `model_fn` gets per-core batch size and
    `input_fn` get per-host batch size.

  Evaluation
  ==========

  `model_fn` should return `TPUEstimatorSpec`, which expects the `eval_metrics`
  for TPU evaluation. If eval_on_tpu is False, the evaluation will execute on
  CPU or GPU; in this case the following discussion on TPU evaluation does not
  apply.

  `TPUEstimatorSpec.eval_metrics` is a tuple of `metric_fn` and `tensors`, where
  `tensors` could be a list of any nested structure of `Tensor`s (See
  `TPUEstimatorSpec` for details).  `metric_fn` takes the `tensors` and returns
  a dict from metric string name to the result of calling a metric function,
  namely a `(metric_tensor, update_op)` tuple.

  One can set `use_tpu` to `False` for testing. All training, evaluation, and
  predict will be executed on CPU. `input_fn` and `model_fn` will receive
  `train_batch_size` or `eval_batch_size` unmodified as `params['batch_size']`.

  Current limitations:
  --------------------

  1. TPU evaluation only works on a single host (one TPU worker) except
     BROADCAST mode.

  2. `input_fn` for evaluation should **NOT** raise an end-of-input exception
     (`OutOfRangeError` or `StopIteration`). And all evaluation steps and all
     batches should have the same size.

  Example (MNIST):
  ----------------

  ```
  # The metric Fn which runs on CPU.
  def metric_fn(labels, logits):
    predictions = tf.argmax(logits, 1)
    return {
      'accuracy': tf.compat.v1.metrics.precision(
          labels=labels, predictions=predictions),
    }

  # Your model Fn which runs on TPU (eval_metrics is list in this example)
  def model_fn(features, labels, mode, config, params):
    ...
    logits = ...

    if mode = tf.estimator.ModeKeys.EVAL:
      return tpu_estimator.TPUEstimatorSpec(
          mode=mode,
          loss=loss,
          eval_metrics=(metric_fn, [labels, logits]))

  # or specify the eval_metrics tensors as dict.
  def model_fn(features, labels, mode, config, params):
    ...
    final_layer_output = ...

    if mode = tf.estimator.ModeKeys.EVAL:
      return tpu_estimator.TPUEstimatorSpec(
          mode=mode,
          loss=loss,
          eval_metrics=(metric_fn, {
              'labels': labels,
              'logits': final_layer_output,
          }))
  ```

  Prediction
  ==========

  Prediction on TPU is an experimental feature to support large batch inference.
  It is not designed for latency-critical system. In addition, due to some
  usability issues, for prediction with small dataset, CPU `.predict`, i.e.,
  creating a new `TPUEstimator` instance with `use_tpu=False`, might be more
  convenient.

  Note: In contrast to TPU training/evaluation, the `input_fn` for prediction
  *should* raise an end-of-input exception (`OutOfRangeError` or
  `StopIteration`), which serves as the stopping signal to `TPUEstimator`. To be
  precise, the ops created by `input_fn` produce one batch of the data.
  The `predict()` API processes one batch at a time. When reaching the end of
  the data source, an end-of-input exception should be raised by one of these
  operations. The user usually does not need to do this manually. As long as the
  dataset is not repeated forever, the `tf.data` API will raise an end-of-input
  exception automatically after the last batch has been produced.

  Note: Estimator.predict returns a Python generator. Please consume all the
  data from the generator so that TPUEstimator can shutdown the TPU system
  properly for user.

  Current limitations:
  --------------------
  1. TPU prediction only works on a single host (one TPU worker).

  2. `input_fn` must return a `Dataset` instance rather than `features`. In
  fact, .train() and .evaluate() also support Dataset as return value.

  Example (MNIST):
  ----------------
  ```
  height = 32
  width = 32
  total_examples = 100

  def predict_input_fn(params):
    batch_size = params['batch_size']

    images = tf.random.uniform(
        [total_examples, height, width, 3], minval=-1, maxval=1)

    dataset = tf.data.Dataset.from_tensor_slices(images)
    dataset = dataset.map(lambda images: {'image': images})

    dataset = dataset.batch(batch_size)
    return dataset

  def model_fn(features, labels, params, mode):
     # Generate predictions, called 'output', from features['image']

    if mode == tf.estimator.ModeKeys.PREDICT:
      return tf.contrib.tpu.TPUEstimatorSpec(
          mode=mode,
          predictions={
              'predictions': output,
              'is_padding': features['is_padding']
          })

  tpu_est = TPUEstimator(
      model_fn=model_fn,
      ...,
      predict_batch_size=16)

  # Fully consume the generator so that TPUEstimator can shutdown the TPU
  # system.
  for item in tpu_est.predict(input_fn=input_fn):
    # Filter out item if the `is_padding` is 1.
    # Process the 'predictions'
  ```

  Exporting
  =========

  `export_saved_model` exports 2 metagraphs, one with `saved_model.SERVING`, and
  another with `saved_model.SERVING` and `saved_model.TPU` tags. At serving
  time, these tags are used to select the appropriate metagraph to load.

  Before running the graph on TPU, the TPU system needs to be initialized. If
  TensorFlow Serving model-server is used, this is done automatically. If not,
  please use `session.run(tpu.initialize_system())`.

  There are two versions of the API: ExportSavedModelApiVersion.V1 and V2.

  In V1, the exported CPU graph is `model_fn` as it is. The exported TPU graph
  wraps `tpu.rewrite()` and `TPUPartitionedCallOp` around `model_fn` so
  `model_fn` is on TPU by default. To place ops on CPU,
  `tpu.outside_compilation(host_call, logits)` can be used.

  Example:
  ----------------

  ```
  def model_fn(features, labels, mode, config, params):
    ...
    logits = ...
    export_outputs = {
      'logits': export_output_lib.PredictOutput(
        {'logits': logits})
    }

    def host_call(logits):
      class_ids = math_ops.argmax(logits)
      classes = string_ops.as_string(class_ids)
      export_outputs['classes'] =
        export_output_lib.ClassificationOutput(classes=classes)

    tpu.outside_compilation(host_call, logits)

    ...
  ```

  In V2, `export_saved_model()` sets up `params['use_tpu']` flag to let the user
  know if the code is exporting to TPU (or not). When `params['use_tpu']` is
  `True`, users need to call `tpu.rewrite()`, `TPUPartitionedCallOp` and/or
  `batch_function()`. Alternatively use `inference_on_tpu()` which is a
  convenience wrapper of the three.

  ```
    def model_fn(features, labels, mode, config, params):
      ...
      # This could be some pre-processing on CPU like calls to input layer with
      # embedding columns.
      x2 = features['x'] * 2

      def computation(input_tensor):
        return layers.dense(
            input_tensor, 1, kernel_initializer=init_ops.zeros_initializer())

      inputs = [x2]
      if params['use_tpu']:
        predictions = array_ops.identity(
            tpu_estimator.inference_on_tpu(computation, inputs,
            num_batch_threads=1, max_batch_size=2, batch_timeout_micros=100),
            name='predictions')
      else:
        predictions = array_ops.identity(
            computation(*inputs), name='predictions')
      key = signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY
      export_outputs = {
          key: export_lib.PredictOutput({'prediction': predictions})
      }
      ...
  ```

  TIP: V2 is recommended as it is more flexible (eg: batching, etc).
  """

  def __init__(self,
               model_fn=None,
               model_dir=None,
               config=None,
               params=None,
               use_tpu=True,
               train_batch_size=None,
               eval_batch_size=None,
               predict_batch_size=None,
               batch_axis=None,
               eval_on_tpu=True,
               export_to_tpu=True,
               export_to_cpu=True,
               warm_start_from=None,
               embedding_config_spec=None,
               export_saved_model_api_version=ExportSavedModelApiVersion.V1):
    """Constructs an `TPUEstimator` instance.

    Args:
      model_fn: Model function as required by `Estimator` which returns
        EstimatorSpec or TPUEstimatorSpec. `training_hooks`, 'evaluation_hooks',
        and `prediction_hooks` must not capure any TPU Tensor inside the
        model_fn.
      model_dir: Directory to save model parameters, graph and etc. This can
        also be used to load checkpoints from the directory into a estimator to
        continue training a previously saved model. If `None`, the model_dir in
        `config` will be used if set. If both are set, they must be same. If
        both are `None`, a temporary directory will be used.
      config: An `tpu_config.RunConfig` configuration object. Cannot be `None`.
      params: An optional `dict` of hyper parameters that will be passed into
        `input_fn` and `model_fn`.  Keys are names of parameters, values are
        basic python types. There are reserved keys for `TPUEstimator`,
        including 'batch_size'.
      use_tpu: A bool indicating whether TPU support is enabled. Currently, -
        TPU training and evaluation respect this bit, but eval_on_tpu can
        override execution of eval. See below.
      train_batch_size: An int representing the global training batch size.
        TPUEstimator transforms this global batch size to a per-shard batch
        size, as params['batch_size'], when calling `input_fn` and `model_fn`.
        Cannot be `None` if `use_tpu` is `True`. Must be divisible by total
        number of replicas.
      eval_batch_size: An int representing evaluation batch size. Must be
        divisible by total number of replicas.
      predict_batch_size: An int representing the prediction batch size. Must be
        divisible by total number of replicas.
      batch_axis: A python tuple of int values describing how each tensor
        produced by the Estimator `input_fn` should be split across the TPU
        compute shards. For example, if your input_fn produced (images, labels)
        where the images tensor is in `HWCN` format, your shard dimensions would
        be [3, 0], where 3 corresponds to the `N` dimension of your images
        Tensor, and 0 corresponds to the dimension along which to split the
        labels to match up with the corresponding images. If None is supplied,
        and per_host_input_for_training is True, batches will be sharded based
        on the major dimension. If tpu_config.per_host_input_for_training is
        False or `PER_HOST_V2`, batch_axis is ignored.
      eval_on_tpu: If False, evaluation runs on CPU or GPU. In this case, the
        model_fn must return `EstimatorSpec` when called with `mode` as `EVAL`.
      export_to_tpu: If True, `export_saved_model()` exports a metagraph for
        serving on TPU. Note that unsupported export modes such as EVAL will be
        ignored. For those modes, only a CPU model will be exported.
        Currently, export_to_tpu only supports PREDICT.
      export_to_cpu: If True, `export_saved_model()` exports a metagraph for
        serving on CPU.
      warm_start_from: Optional string filepath to a checkpoint or SavedModel to
        warm-start from, or a `tf.estimator.WarmStartSettings` object to fully
        configure warm-starting.  If the string filepath is provided instead of
        a `WarmStartSettings`, then all variables are warm-started, and it is
        assumed that vocabularies and Tensor names are unchanged.
      embedding_config_spec: Optional EmbeddingConfigSpec instance
        to support using TPU embedding.
      export_saved_model_api_version: ExportSavedModelApiVersion, V1 or V2.
        With V1, `export_saved_model()` adds rewrite() and TPUPartitionedCallOp()
        for user; while in v2, user is expected to add rewrite(),
        TPUPartitionedCallOp() etc in their model_fn.
        A helper function `inference_on_tpu` is provided for V2.
        brn_tpu_estimator.py includes examples for both versions
        i.e. TPUEstimatorExportTest and TPUEstimatorExportV2Test.

    Raises:
      ValueError: `params` has reserved keys already.
    """
    if config is None or not isinstance(config, tpu_config.RunConfig):
      raise ValueError(
          '`config` must be provided with type `tpu_config.RunConfig`')

    if params is not None and any(k in params for k in _RESERVED_PARAMS_KEYS):
      raise ValueError('{} are reserved keys but existed in params {}.'.format(
          _RESERVED_PARAMS_KEYS, params))

    if use_tpu:
      # Perform some very basic validations. More validations will be found in
      # _InternalTPUContext.
      if train_batch_size is None:
        raise ValueError('`train_batch_size` cannot be `None`')
      util_lib.check_positive_integer(train_batch_size, 'train_batch_size')

      if (config.tpu_config.per_host_input_for_training is
          tpu_config.InputPipelineConfig.PER_SHARD_V1 and
          config.tpu_config.num_cores_per_replica):
        raise ValueError(
            'Model parallelism only supports per host input for training. '
            'Please adjust TPURunconfig.per_host_input_for_training.')

      if eval_batch_size is not None:
        util_lib.check_positive_integer(eval_batch_size, 'eval_batch_size')

      if predict_batch_size is not None:
        util_lib.check_positive_integer(predict_batch_size,
                                        'predict_batch_size')

      if embedding_config_spec:
        if (config.tpu_config.per_host_input_for_training not in
            (tpu_config.InputPipelineConfig.PER_HOST_V1,
             tpu_config.InputPipelineConfig.PER_HOST_V2)):
          raise ValueError('Only PER_HOST_V1 and PER_HOST_V2 is supported when '
                           'using TPU Embedding; got {}.'.format(
                               config.tpu_config.per_host_input_for_training))
        self._embedding_from_feature_columns = (
            embedding_config_spec.feature_columns is not None)

    if (not (use_tpu and eval_on_tpu) and embedding_config_spec and
        embedding_config_spec.partition_strategy == 'mod'):
      raise ValueError('Mod sharding of embedding tables not supported on '
                       'CPU.')

    # Verifies the model_fn signature according to Estimator framework.
    estimator_lib._verify_model_fn_args(model_fn, params)  # pylint: disable=protected-access
    # We cannot store config and params in this constructor as parent
    # constructor might change them, such as assigning a temp dir for
    # config.model_dir.
    model_function = self._augment_model_fn(model_fn, batch_axis)

    # Overwrite log_step_count_steps to disable TensorLoggingHook and
    # StepCounterHook from being created in Estimator. TPUEstimator already
    # added equivalent hooks in _augment_model_fn above.
    self._log_every_n_steps = config.log_step_count_steps
    config = config.replace(log_step_count_steps=None)

    # Passing non-None params as wrapped model_fn has it.
    params = params or {}
    super(TPUEstimator, self).__init__(
        model_fn=model_function,
        model_dir=model_dir,
        config=config,
        params=params,
        warm_start_from=warm_start_from)
    self._iterations_per_training_loop = util_lib.parse_iterations_per_loop(
        self._config.tpu_config.iterations_per_loop)
    # In absence of an explicit `log_every_n_secs` config, if the
    # `iterations_per_loop` value is specified as time in seconds, enable
    # logging every n secs based on the `iterations_per_loop` value. A trade-off
    # avoiding API change on the current release.
    # TODO(henrytan): add `log_every_n_secs` to RunConfig.
    if self._iterations_per_training_loop.unit == 'seconds':
      self._log_every_n_secs = self._iterations_per_training_loop.value
      self._log_every_n_steps = None
    elif self._iterations_per_training_loop.unit == 'count':
      if self._log_every_n_steps is not None:
        # Each session.run() lasts for iterations_per_loop. We can't log
        # in-between a session.run(), and we can only log after the
        # `iterations_per_loop` steps, so we can only approximate. If a user
        # requests to log every N steps, we actually want to roughly log every
        # N / `iterations_per_loop` steps to match the original intention.
        self._log_every_n_steps = (
            int(math.ceil(float(self._log_every_n_steps) /
                          self._iterations_per_training_loop.value)))
      self._log_every_n_secs = None
    else:
      assert False, ('Invalid TPUConfig `iterations_per_loop` value. '
                     'Indicates a bug in `iterations_per_loop` '
                     'parsing.')

    # All properties passed to _InternalTPUContext are immutable.
    # pylint: disable=protected-access
    self._ctx = tpu_context._get_tpu_context(
        self._config, train_batch_size, eval_batch_size, predict_batch_size,
        use_tpu, eval_on_tpu, embedding_config_spec)

    self._export_to_cpu = export_to_cpu
    self._export_to_tpu = export_to_tpu

    if not isinstance(export_saved_model_api_version,
                      ExportSavedModelApiVersion):
      raise ValueError('export_saved_model_api_version should be of type '
                       'ExportSavedModelApiVersion; got {}.'.format(
                           export_saved_model_api_version))
    self._export_saved_model_api_version = export_saved_model_api_version
    self._is_input_fn_invoked = None

    self._rendezvous = {}

  def _add_meta_graph_for_mode(self,
                               builder,
                               input_receiver_fn_map,
                               checkpoint_path,
                               save_variables=True,
                               mode=model_fn_lib.ModeKeys.PREDICT,
                               export_tags=None,
                               check_variables=True,
                               strip_default_attrs=True):
    if self._export_to_tpu and mode != model_fn_lib.ModeKeys.PREDICT:
      logging.warning('TPUEstimator only handles mode PREDICT for exporting '
                      'when `export_to_tpu` is `True`; Mode {} will be ignored '
                      'for TPU.'.format(mode))

    if not self._export_to_cpu and not self._export_to_tpu:
      raise ValueError('One of export_to_cpu and export_to_tpu must be true.')

    if self._export_to_cpu:
      (super(TPUEstimator, self)._add_meta_graph_for_mode(
          builder,
          input_receiver_fn_map,
          checkpoint_path,
          save_variables,
          mode=mode,
          export_tags=export_tags,
          check_variables=check_variables,
          strip_default_attrs=strip_default_attrs))

    if self._export_to_tpu and mode == model_fn_lib.ModeKeys.PREDICT:
      input_receiver_fn_map = {
          _INFERENCE_ON_TPU_MODE: input_receiver_fn_map[mode]
      }
      export_tags = [tag_constants.SERVING, tag_constants.TPU]
      mode = _INFERENCE_ON_TPU_MODE

      # See b/110052256 for why `check_variables` is `False`.
      if not self._export_to_cpu:
        check_variables = save_variables = True
      else:
        check_variables = save_variables = False
      (super(TPUEstimator, self)._add_meta_graph_for_mode(
          builder,
          input_receiver_fn_map,
          checkpoint_path,
          save_variables=save_variables,
          mode=mode,
          export_tags=export_tags,
          check_variables=check_variables,
          strip_default_attrs=strip_default_attrs))

  def _call_model_fn(self, features, labels, mode, config):
    if mode == _INFERENCE_ON_TPU_MODE:
      context = tpu._TPUInferenceContext('tpu_inference', check_ops=False)
      try:
        context.Enter()
        if self._export_saved_model_api_version == ExportSavedModelApiVersion.V1:
          result = self._call_model_fn_for_inference(features, labels, mode,
                                                     config)
        else:
          result = super(TPUEstimator, self)._call_model_fn(
              features, labels, mode, config)
      finally:
        context.Exit()
      return result
    else:
      return super(TPUEstimator, self)._call_model_fn(features, labels, mode,
                                                      config)

  def _call_model_fn_for_inference(self, features, labels, mode, config):
    """Wraps `_call_model_fn` for `export_saved_model`."""
    if mode != _INFERENCE_ON_TPU_MODE:
      raise ValueError('mode must be {}; '
                       'got {}.'.format(_INFERENCE_ON_TPU_MODE, mode))
    return model_fn_inference_on_tpu(
        self._model_fn,
        features,
        labels,
        config,
        self._params,
        batch_config=None)

  def _create_global_step(self, graph):
    """Creates a global step suitable for TPUs.

    Args:
      graph: The graph in which to create the global step.

    Returns:
      A global step `Tensor`.

    Raises:
      ValueError: if the global step tensor is already defined.
    """
    return _create_global_step(graph)

  def _convert_train_steps_to_hooks(self, steps, max_steps):
    with self._ctx.with_mode(model_fn_lib.ModeKeys.TRAIN) as ctx:
      if ctx.is_running_on_cpu():
        return super(TPUEstimator, self)._convert_train_steps_to_hooks(
            steps, max_steps)

    # On TPU.
    if steps is None and max_steps is None:
      raise ValueError(
          'For TPU training, one of `steps` or `max_steps` must be set. '
          'Cannot be both `None`.')

    # Estimator.train has explicit positiveness check.
    if steps is not None:
      util_lib.check_positive_integer(steps, 'Train steps')
    if max_steps is not None:
      util_lib.check_positive_integer(max_steps, 'Train max_steps')

    return [
        _TPUStopAtStepHook(
            self._iterations_per_training_loop, steps, max_steps)
    ]

  def _convert_eval_steps_to_hooks(self, steps):
    with self._ctx.with_mode(model_fn_lib.ModeKeys.EVAL) as ctx:
      if ctx.is_running_on_cpu():
        return super(TPUEstimator, self)._convert_eval_steps_to_hooks(steps)

    if steps is None:
      raise ValueError('Evaluate `steps` must be set on TPU. Cannot be `None`.')

    util_lib.check_positive_integer(steps, 'Eval steps')

    return [
        evaluation._StopAfterNEvalsHook(  # pylint: disable=protected-access
            num_evals=steps),
        _SetEvalIterationsHook(steps)
    ]

  def _call_input_fn(self, input_fn, mode):
    """Calls the input function.

    Args:
      input_fn: The input function.
      mode: ModeKeys

    Returns:
      In TPU mode, returns an input_fn to be called later in model_fn.
      Otherwise, calls the input_fn and returns either fatures or
        (features, labels).

    Raises:
      ValueError: if input_fn takes invalid arguments or does not have `params`.
    """
    input_fn_args = function_utils.fn_args(input_fn)
    config = self.config  # a deep copy.
    kwargs = {}
    if 'params' in input_fn_args:
      kwargs['params'] = self.params  # a deep copy.
    else:
      raise ValueError('input_fn ({}) does not include params argument, '
                       'required by TPUEstimator to pass batch size as '
                       'params["batch_size"]'.format(input_fn))
    if 'config' in input_fn_args:
      kwargs['config'] = config

    if 'mode' in input_fn_args:
      kwargs['mode'] = mode

    # Records the fact input_fn has been invoked.
    self._is_input_fn_invoked = True

    with self._ctx.with_mode(mode) as ctx:
      if (ctx.is_running_on_cpu() and
          ctx.is_input_slice_broadcast_to_all_cores()):
        raise ValueError('Invalid TPUConfig `eval_training_input_configuration`'
                         ' value. SLICED mode only works on use_tpu = True.')
      # Setting the batch size in params first. This helps user to have same
      # input_fn for use_tpu=True/False.
      batch_size_for_input_fn = ctx.batch_size_for_input_fn
      if batch_size_for_input_fn is not None:
        _add_item_to_params(kwargs['params'], _BATCH_SIZE_KEY,
                            batch_size_for_input_fn)

      # For export_saved_model, input_fn is never passed to Estimator. So,
      # `is_export_mode` must be False.
      if ctx.is_running_on_cpu(is_export_mode=False):
        with ops.device('/device:CPU:0'):
          return input_fn(**kwargs)

      # For TPU computation, input_fn should be invoked in a tf.while_loop for
      # performance. While constructing the tf.while_loop, the structure of
      # inputs returned by the `input_fn` needs to be recorded. The structure
      # includes whether features or labels is dict or single Tensor, dict keys,
      # tensor shapes, and dtypes. The recorded structure is used to create the
      # infeed dequeue ops, which must be wrapped and passed as a Fn, called
      # inside the TPU computation, as the TPU computation is wrapped inside a
      # tf.while_loop also. So, we either pass input_fn to model_fn or pass
      # dequeue_fn to model_fn. Here, `input_fn` is passed directly as
      # `features` in `model_fn` signature.
      def _input_fn(ctx):
        _add_item_to_params(kwargs['params'], _CTX_KEY, ctx)
        return input_fn(**kwargs)

      return _input_fn

  def _validate_features_in_predict_input(self, result):
    """Skip the validation.

    For TPUEstimator, we do not need to check the result type. `_InputPipeline`
    has stronger check. Parent class's check generates confusing warning msg.

    Args:
      result: `features` returned by input_fn.
    """
    pass

  def train(self,
            input_fn,
            hooks=None,
            steps=None,
            max_steps=None,
            saving_listeners=None):
    rendezvous = error_handling.ErrorRendezvous(num_sources=3)
    self._rendezvous[model_fn_lib.ModeKeys.TRAIN] = rendezvous
    try:
      return super(TPUEstimator, self).train(
          input_fn=input_fn,
          hooks=hooks,
          steps=steps,
          max_steps=max_steps,
          saving_listeners=saving_listeners)
    except Exception:  # pylint: disable=broad-except
      rendezvous.record_error('training_loop', sys.exc_info())
    finally:
      rendezvous.record_done('training_loop')
      rendezvous.raise_errors()

  def evaluate(self,
               input_fn,
               steps=None,
               hooks=None,
               checkpoint_path=None,
               name=None):
    rendezvous = error_handling.ErrorRendezvous(num_sources=3)
    self._rendezvous[model_fn_lib.ModeKeys.EVAL] = rendezvous
    try:
      return super(TPUEstimator, self).evaluate(
          input_fn,
          steps=steps,
          hooks=hooks,
          checkpoint_path=checkpoint_path,
          name=name)
    except Exception:  # pylint: disable=broad-except
      rendezvous.record_error('evaluation_loop', sys.exc_info())
    finally:
      rendezvous.record_done('evaluation_loop')
      rendezvous.raise_errors()

  def predict(self,
              input_fn,
              predict_keys=None,
              hooks=None,
              checkpoint_path=None,
              yield_single_examples=True):
    rendezvous = error_handling.ErrorRendezvous(num_sources=3)
    self._rendezvous[model_fn_lib.ModeKeys.PREDICT] = rendezvous
    try:
      for result in super(TPUEstimator, self).predict(
          input_fn=input_fn,
          predict_keys=predict_keys,
          hooks=hooks,
          checkpoint_path=checkpoint_path,
          yield_single_examples=yield_single_examples):
        yield result
    except Exception:  # pylint: disable=broad-except
      rendezvous.record_error('prediction_loop', sys.exc_info())
    finally:
      rendezvous.record_done('prediction_loop')
      rendezvous.raise_errors()

    rendezvous.record_done('prediction_loop')
    rendezvous.raise_errors()

  def _augment_model_fn(self, model_fn, batch_axis):
    """Returns a new model_fn, which wraps the TPU support."""

    def _model_fn(features, labels, mode, config, params):
      """A Estimator `model_fn` for TPUEstimator."""

      # `input_fn` is called in `train()`, `evaluate()`, and `predict()`,
      # but not in `export_saved_model()`.
      if self._is_input_fn_invoked:
        is_export_mode = False
      else:
        is_export_mode = True

      # Clear the bit.
      self._is_input_fn_invoked = None

      if is_export_mode:
        if mode == _INFERENCE_ON_TPU_MODE:
          _add_item_to_params(params, _USE_TPU_KEY, True)
          mode = model_fn_lib.ModeKeys.PREDICT
        else:
          _add_item_to_params(params, _USE_TPU_KEY, False)

      with self._ctx.with_mode(mode) as ctx:
        model_fn_wrapper = _ModelFnWrapper(model_fn, config, params, ctx)

        # examples_hook is added to training_hooks for both CPU and TPU
        # execution.
        if (self._log_every_n_steps is not None
            or self._log_every_n_secs is not None):
          examples_hook = ExamplesPerSecondHook(
              ctx.global_batch_size,
              # pylint:disable=g-long-ternary
              output_dir=(self.model_dir
                          if not config or config.save_summary_steps
                          else None),
              # pylint:enable=g-long-ternary
              every_n_steps=self._log_every_n_steps,
              every_n_secs=self._log_every_n_secs)

        if ctx.is_running_on_cpu(is_export_mode=is_export_mode):
          logging.info('Running %s on CPU', mode)
          estimator_spec = model_fn_wrapper.call_without_tpu(
              features, labels, is_export_mode=is_export_mode)
          if (self._log_every_n_steps is not None
              or self._log_every_n_secs is not None):
            estimator_spec = estimator_spec._replace(
                training_hooks=estimator_spec.training_hooks + (examples_hook,))
          return estimator_spec

        assert labels is None, '`labels` passed to `model_fn` must be `None`.'
        # TPUEstimator._call_input_fn passes `input_fn` as features to here.
        assert callable(features), '`input_fn` is not callable.'
        input_fn = features

        tpu_init_ops = []
        if ctx.embedding_config and mode == model_fn_lib.ModeKeys.TRAIN:
          dummy_table_variables, dummy_table_variables_init = (
              tpu_embedding_gradient.create_dummy_table_variables(
                  ctx.embedding_config.tpu_embedding))
          ctx.embedding_config.dummy_table_variables = dummy_table_variables
          tpu_init_ops.append(dummy_table_variables_init)

        input_holders = _InputPipeline(input_fn, batch_axis, ctx)
        enqueue_ops, dequeue_fn, input_hooks, run_infeed_loop_on_coordinator = (
            input_holders.generate_infeed_enqueue_ops_and_dequeue_fn())

        graph = ops.get_default_graph()
        for enqueue_op in enqueue_ops:
          if isinstance(enqueue_op, list):
            graph.get_collection_ref(_TPU_ENQUEUE_OPS).extend(enqueue_op)
          else:
            graph.add_to_collection(_TPU_ENQUEUE_OPS, enqueue_op)

        if mode == model_fn_lib.ModeKeys.TRAIN:
          compile_op, loss, host_call, scaffold_fn, training_hooks = (
              _train_on_tpu_system(ctx, model_fn_wrapper, dequeue_fn))
          if ctx.embedding_config:
            g = ops.get_default_graph()
            table_to_config_dict = (
                ctx.embedding_config.tpu_embedding.table_to_config_dict)
            optimization_parameters = (
                ctx.embedding_config.tpu_embedding.optimization_parameters)
            if self._embedding_from_feature_columns:
              embedding_variable_name_by_table, slot_variable_names_by_table = (
                  _tpu_estimator_embedding.get_full_variable_names(
                      g, table_to_config_dict, optimization_parameters
                  )
              )
            else:
              embedding_variable_name_by_table = None
              slot_variable_names_by_table = None
            embedding_variables_and_ops = (
                ctx.embedding_config.tpu_embedding.create_variables_and_ops(
                    embedding_variable_name_by_table,
                    slot_variable_names_by_table
                ))
            tpu_init_ops.extend(embedding_variables_and_ops.load_ops())
          # scaffold_fn must be called after variables for TPU embedding has
          # been created on CPU, as user might reinitialize those from some
          # checkpoint within scaffold_fn.
          scaffold = _get_scaffold(scaffold_fn)

          host_ops = host_call.create_tpu_hostcall()

          shutdown_hooks = []
          shutdown_mode = os.environ.get('TF_TPU_GRACEFUL_SHUTDOWN_MODE',
                                         'reset_computation')
          if shutdown_mode:
            if shutdown_mode == 'shutdown_worker':
              finalizer_hooks = [
                  session_support.ShutdownLameWorkers(),
              ]
            elif shutdown_mode == 'shutdown_all_workers':
              finalizer_hooks = [
                  session_support.ShutdownAllWorkers(),
              ]
            elif shutdown_mode == 'reset_computation':
              finalizer_hooks = [
                  session_support.ResetComputation(),
              ]
            elif not shutdown_mode:
              finalizer_hooks = []
            else:
              raise ValueError(
                  'Unknown TF_TPU_GRACEFUL_SHUTDOWN_MODE "%s"' % shutdown_mode)

            if finalizer_hooks:
              shutdown_hooks.append(
                  session_support.GracefulShutdownHook(
                      checkpoint_prefix=self.model_dir + '/model.ckpt',
                      on_shutdown_hooks=finalizer_hooks))

          with ops.control_dependencies([loss]):
            global_step = array_ops.identity(training.get_global_step())
          hooks = input_hooks + shutdown_hooks
          hooks.extend([
              TPUInfeedOutfeedSessionHook(
                  ctx,
                  enqueue_ops,
                  host_ops,
                  tpu_compile_op=compile_op,
                  run_infeed_loop_on_coordinator=(
                      run_infeed_loop_on_coordinator),
                  rendezvous=self._rendezvous[mode],
                  master=self._config.master,
                  session_config=self._session_config,
                  tpu_init_ops=tpu_init_ops,
                  outfeed_every_n_steps=self._config.tpu_config
                  .experimental_host_call_every_n_steps),
              InstallSignalHandlerHook()
          ])
          if _check_add_preemption_hook(self._config.cluster):
            hooks.extend(
                [preempted_hook.CloudTPUPreemptedHook(self._config.cluster)])
          if (self._log_every_n_steps is not None
              or self._log_every_n_secs is not None):
            if self._iterations_per_training_loop.unit == 'count':
              examples_hook._set_steps_per_run(  # pylint: disable=protected-access
                  self._iterations_per_training_loop.value)
            hooks.append(training.LoggingTensorHook(
                {
                    'loss': array_ops.identity(loss),
                    'step': global_step,
                },
                every_n_iter=self._log_every_n_steps,
                every_n_secs=self._log_every_n_secs))
            hooks.append(examples_hook)

          if training_hooks:
            hooks.extend(training_hooks)

          chief_hooks = []
          if (self._config.save_checkpoints_secs or
              self._config.save_checkpoints_steps):
            checkpoint_hook = training.CheckpointSaverHook(
                self.model_dir,
                save_secs=self._config.save_checkpoints_secs,
                save_steps=self._config.save_checkpoints_steps,
                scaffold=scaffold)
            if self._iterations_per_training_loop.unit == 'count':
              checkpoint_hook._set_steps_per_run(  # pylint: disable=protected-access
                  self._iterations_per_training_loop.value)
            else:
              # When estimating iterations_per_loop, set steps_per_run to an
              # arbitrarily high number to force checking the global step on
              # every call.
              # TODO(henrytan): refactor SecondOrStepTimer to do this more
              # explicitly.
              checkpoint_hook._set_steps_per_run(  # pylint: disable=protected-access
                  100000)
            chief_hooks.append(checkpoint_hook)

          summary.scalar(model_fn_lib.LOSS_METRIC_KEY, loss)
          with ops.control_dependencies([loss]):
            update_ops = _sync_variables_ops(ctx)
            if ctx.embedding_config:
              update_ops.extend(embedding_variables_and_ops.retrieve_ops())

          # Validate the TPU training graph to catch basic errors
          _validate_tpu_training_graph()

          train_op = control_flow_ops.group(*update_ops)
          graph.add_to_collection(_TPU_TRAIN_OP, train_op)

          return model_fn_lib.EstimatorSpec(
              mode,
              loss=loss,
              training_chief_hooks=chief_hooks,
              training_hooks=hooks,
              train_op=train_op,
              scaffold=scaffold)

        if mode == model_fn_lib.ModeKeys.EVAL:
          compile_op, total_loss, host_calls, scaffold_fn, eval_hooks = (
              _eval_on_tpu_system(ctx, model_fn_wrapper, dequeue_fn))
          if ctx.embedding_config:
            g = ops.get_default_graph()
            table_to_config_dict = (
                ctx.embedding_config.tpu_embedding.table_to_config_dict)
            if self._embedding_from_feature_columns:
              embedding_variable_name_by_table, _ = (
                  _tpu_estimator_embedding.get_full_variable_names(
                      g, table_to_config_dict)
              )
            else:
              embedding_variable_name_by_table = None
            embedding_variables_and_ops = (
                ctx.embedding_config.tpu_embedding.create_variables_and_ops(
                    embedding_variable_name_by_table
                ))
            tpu_init_ops.extend(embedding_variables_and_ops.load_ops())
          # scaffold_fn must be called after variables for TPU embedding has
          # been created on CPU, as user might reinitialize those from some
          # checkpoint within scaffold_fn.
          scaffold = _get_scaffold(scaffold_fn)
          iterations_per_loop_var = _create_or_get_iterations_per_loop()
          mean_loss = math_ops.div(
              total_loss,
              math_ops.cast(iterations_per_loop_var, dtype=total_loss.dtype))

          with ops.control_dependencies([mean_loss]):
            # After TPU evaluation computation is done (the mean_loss tensor),
            # reads all variables back from TPU and updates the eval step
            # counter properly
            internal_ops_to_run = _sync_variables_ops(ctx)
            internal_ops_to_run.append(
                _increase_eval_step_op(iterations_per_loop_var))

          host_call_ret = host_calls.create_tpu_hostcall()
          eval_metric_ops = {}
          eval_update_ops = []

          eval_metrics = host_call_ret.get('eval_metrics', {})
          if eval_metrics:
            # Creates a dummy metric update_op for all metrics. Estimator
            # expects all metrics in `eval_metric_ops` have update_op and calls
            # them one by one. The real metric update_ops are invoked in a
            # separated thread. So, here give Estimator the dummy op for all
            # metrics.
            with ops.control_dependencies(internal_ops_to_run):
              dummy_update_op = control_flow_ops.no_op()

            for k, v in eval_metrics.items():
              eval_metric_ops[k] = (v[0], dummy_update_op)
              eval_update_ops.append(v[1])
          else:
            # If no eval metrics are passed, create an identity node for the
            # loss and add `internal_ops_to_run` to its dependencies. So
            # `internal_ops_to_run` can be executed.
            with ops.control_dependencies(internal_ops_to_run):
              mean_loss = array_ops.identity(mean_loss)

          if 'host_call' not in host_call_ret:
            host_ops = []
          else:
            host_ops = host_call_ret['host_call']
          hooks = [
              TPUInfeedOutfeedSessionHook(
                  ctx,
                  enqueue_ops,
                  eval_update_ops + host_ops,
                  tpu_compile_op=compile_op,
                  run_infeed_loop_on_coordinator=(
                      run_infeed_loop_on_coordinator),
                  rendezvous=self._rendezvous[mode],
                  master=self._config.evaluation_master,
                  session_config=self._session_config,
                  tpu_init_ops=tpu_init_ops)
          ] + input_hooks

          if _check_add_preemption_hook(self._config.cluster):
            hooks.extend(
                [preempted_hook.CloudTPUPreemptedHook(self._config.cluster)])

          if eval_hooks:
            hooks.extend(eval_hooks)

          return model_fn_lib.EstimatorSpec(
              mode,
              loss=mean_loss,
              evaluation_hooks=hooks,
              eval_metric_ops=eval_metric_ops,
              scaffold=scaffold)

        # Predict
        assert mode == model_fn_lib.ModeKeys.PREDICT

        (compile_op, dummy_predict_op, host_calls,
         scaffold_fn, prediction_hooks) = _predict_on_tpu_system(
             ctx, model_fn_wrapper, dequeue_fn)
        scaffold = _get_scaffold(scaffold_fn)
        with ops.control_dependencies([dummy_predict_op]):
          internal_ops_to_run = _sync_variables_ops(ctx)
          with ops.control_dependencies(internal_ops_to_run):
            dummy_predict_op = control_flow_ops.no_op()

        # In train and evaluation, the main TPU program is passed to monitored
        # training session to run. Infeed enqueue and outfeed dequeue are
        # executed in side threads. This is not the configuration for
        # prediction mode.
        #
        # For prediction, the Estimator executes the EstimatorSpec.predictions
        # directly and yield the element (via generator) to call site. So, the
        # outfeed based prediction must be passed to MonitoredSession directly.
        # Other parts of the TPU execution are organized as follows.
        #
        # 1. All outfeed based Tensors must be grouped with predictions Tensors
        #    to form a single invocation. This avoid the issue we might trigger
        #    multiple outfeeds incorrectly. To achieve this, `host_call` is
        #    placed in control_dependencies of `stopping_signals`, and
        #    `stopping_signals` is passed into _StoppingPredictHook, which sets
        #    the `stopping_signals` as SessionRunArgs. MonitoredSession merges
        #    all SessionRunArgs with the fetch in session.run together.
        #
        # 2. The TPU program (dummy_predict_op) and enqueue_ops (infeed Enqueue)
        #    are grouped together. They will be launched once and only once in
        #    side threads and they quit naturally according to the SAME stopping
        #    condition.
        enqueue_ops.append(dummy_predict_op)

        host_call_ret = host_calls.create_tpu_hostcall()
        if 'host_call' not in host_call_ret:
          host_ops = []
        else:
          host_ops = host_call_ret['host_call']

        predictions = host_call_ret['predictions']
        _verify_cross_hosts_transfer_size(
            predictions,
            message=(
                'The estimated size for TPUEstimatorSpec.predictions is too '
                'large.'))
        signals = host_call_ret['signals']

        with ops.control_dependencies(host_ops):
          host_ops = []  # Empty, we do do not need it anymore.
          scalar_stopping_signal = _StopSignals.as_scalar_stopping_signal(
              signals)
          predictions = _PaddingSignals.slice_tensor_or_dict(
              predictions, signals)

        hooks = [
            _StoppingPredictHook(scalar_stopping_signal),
            TPUInfeedOutfeedSessionHookForPrediction(
                ctx, enqueue_ops, host_ops, rendezvous=self._rendezvous[mode],
                tpu_compile_op=compile_op,
                master=self._config.master,
                session_config=self._session_config),
        ] + input_hooks

        if prediction_hooks:
          hooks.extend(prediction_hooks)

        return model_fn_lib.EstimatorSpec(
            mode,
            prediction_hooks=hooks,
            predictions=predictions,
            scaffold=scaffold)

    return _model_fn


def _check_add_preemption_hook(cluster):
  return (tpu_cluster_resolver.is_running_in_gce() and cluster and
          isinstance(cluster, tpu_cluster_resolver.TPUClusterResolver) and
          cluster._cloud_tpu_client.api_available())


def _export_output_to_tensors(export_output):
  """Get a list of `Tensors` used in `export_output`.

  Args:
    export_output: an `ExportOutput` object such as `ClassificationOutput`,
      `RegressionOutput`, or `PredictOutput`.

  Returns:
    a list of tensors used in export_output.

  Raises:
    ValueError: if `export_output` is not one of `ClassificationOutput`,
        `RegressionOutput`, or `PredictOutput`.
  """
  if isinstance(export_output, export_output_lib.ClassificationOutput):
    return [export_output.scores, export_output.classes]
  elif isinstance(export_output, export_output_lib.RegressionOutput):
    return [export_output.value]
  elif isinstance(export_output, export_output_lib.PredictOutput):
    return list(export_output.outputs.values())
  else:
    raise ValueError(
        '`export_output` must be have type `ClassificationOutput`, '
        '`RegressionOutput`, or `PredictOutput`; got {}.'.format(export_output))


def _clone_export_output_with_tensors(export_output, tensors):
  """Clones `export_output` but with new `tensors`.

  Args:
    export_output: an `ExportOutput` object such as `ClassificationOutput`,
      `RegressionOutput`, or `PredictOutput`.
    tensors: a list of `Tensors` used to construct a new `export_output`.

  Returns:
    A dict similar to `export_output` but with `tensors`.

  Raises:
    ValueError: if `export_output` is not one of `ClassificationOutput`,
        `RegressionOutput`, or `PredictOutput`.
  """
  if isinstance(export_output, export_output_lib.ClassificationOutput):
    if len(tensors) != 2:
      raise ValueError('tensors must be of length 2; '
                       'got {}.'.format(len(tensors)))
    return export_output_lib.ClassificationOutput(*tensors)
  elif isinstance(export_output, export_output_lib.RegressionOutput):
    if len(tensors) != 1:
      raise ValueError('tensors must be of length 1; '
                       'got {}'.format(len(tensors)))
    return export_output_lib.RegressionOutput(*tensors)
  elif isinstance(export_output, export_output_lib.PredictOutput):
    return export_output_lib.PredictOutput(
        dict(zip(export_output.outputs.keys(), tensors)))
  else:
    raise ValueError(
        '`export_output` must be have type `ClassificationOutput`, '
        '`RegressionOutput`, or `PredictOutput`; got {}.'.format(export_output))


def _eval_on_tpu_system(ctx, model_fn_wrapper, dequeue_fn):
  """Executes `model_fn_wrapper` multiple times on all TPU shards."""
  iterations_per_loop_var = _create_or_get_iterations_per_loop()

  (single_tpu_eval_step, host_calls, captured_scaffold_fn, captured_eval_hooks
  ) = model_fn_wrapper.convert_to_single_tpu_eval_step(dequeue_fn)

  @tpu_function.on_device_training_loop
  def multi_tpu_eval_steps_on_single_shard(replica_id):
    # `tpu.split_compile_and_shard()` splits and passes input for each
    # replica as an array. As so, correctly reshape the input to be a
    # scalar.
    replica_id = array_ops.reshape(replica_id, [])
    with tpu_context._TPUEstimatorReplicaContext(replica_id):  # pylint: disable=protected-access
      return training_loop.repeat(iterations_per_loop_var, single_tpu_eval_step,
                                  [_ZERO_LOSS])

  # Add input that represents id for each replica in sync so that
  # _TPUEstimatorReplicaContext can be correctly entered during
  # replicated computation.
  replica_id_inputs = []
  replica_id_inputs.append(
      [constant_op.constant(i) for i in range(ctx.num_replicas)])

  (
      compile_op,
      loss,
  ) = tpu.split_compile_and_shard(
      multi_tpu_eval_steps_on_single_shard,
      inputs=replica_id_inputs,
      num_shards=ctx.num_replicas,
      outputs_from_all_shards=False,
      device_assignment=ctx.device_assignment)

  loss = loss[0]
  return (compile_op, loss, host_calls, captured_scaffold_fn,
          captured_eval_hooks.get())


def _train_on_tpu_system(ctx, model_fn_wrapper, dequeue_fn):
  """Executes `model_fn_wrapper` multiple times on all TPU shards."""
  iterations_per_loop_var = _create_or_get_iterations_per_loop()

  (single_tpu_train_step, host_call, captured_scaffold_fn,
   captured_training_hooks) = (
       model_fn_wrapper.convert_to_single_tpu_train_step(dequeue_fn))

  @tpu_function.on_device_training_loop
  def multi_tpu_train_steps_on_single_shard(replica_id):
    # `tpu.split_compile_and_shard()` splits and passes input for each
    # replica as an array. As so, correctly reshape the input to be a
    # scalar.
    replica_id = array_ops.reshape(replica_id, [])
    with tpu_context._TPUEstimatorReplicaContext(replica_id):  # pylint: disable=protected-access
      outputs = training_loop.while_loop(
          lambda i, loss: i < iterations_per_loop_var,
          lambda i, loss: [i + 1, single_tpu_train_step(i)],
          inputs=[0, _INITIAL_LOSS])
      return outputs[1:]

  # Add input that represents id for each replica in sync so that
  # _TPUEstimatorReplicaContext can be correctly entered during
  # replicated computation.
  replica_id_inputs = []
  replica_id_inputs.append(
      [constant_op.constant(i) for i in range(ctx.num_replicas)])

  (compile_op, loss) = tpu.split_compile_and_shard(
      multi_tpu_train_steps_on_single_shard,
      inputs=replica_id_inputs,
      num_shards=ctx.num_replicas,
      outputs_from_all_shards=False,
      device_assignment=ctx.device_assignment)

  loss = loss[0]
  return (compile_op, loss, host_call, captured_scaffold_fn,
          captured_training_hooks.get())


def _predict_on_tpu_system(ctx, model_fn_wrapper, dequeue_fn):
  """Executes `model_fn_wrapper` multiple times on all TPU shards."""
  (single_tpu_predict_step, host_calls, captured_scaffold_fn,
   captured_predict_hooks
  ) = model_fn_wrapper.convert_to_single_tpu_predict_step(dequeue_fn)

  @tpu_function.on_device_training_loop
  def multi_tpu_predict_steps_on_single_shard(replica_id):
    # `tpu.split_compile_and_shard()` splits and passes input for each
    # replica as an array. As so, correctly reshape the input to be a
    # scalar.
    replica_id = array_ops.reshape(replica_id, [])
    with tpu_context._TPUEstimatorReplicaContext(replica_id):  # pylint: disable=protected-access

      def cond(scalar_stopping_signal):
        return math_ops.logical_not(
            _StopSignals.should_stop(scalar_stopping_signal))

      inputs = [_StopSignals.NON_STOPPING_SIGNAL]
      outputs = training_loop.while_loop(
          cond, single_tpu_predict_step, inputs=inputs, name=b'loop')
      return outputs

  # Add input that represents id for each replica in sync so that
  # _TPUEstimatorReplicaContext can be correctly entered during
  # replicated computation.
  replica_id_inputs = []
  replica_id_inputs.append(
      [constant_op.constant(i) for i in range(ctx.num_replicas)])
  (
      compile_op,
      dummy_predict_op,
  ) = tpu.split_compile_and_shard(
      multi_tpu_predict_steps_on_single_shard,
      inputs=replica_id_inputs,
      num_shards=ctx.num_replicas,
      outputs_from_all_shards=False,
      device_assignment=ctx.device_assignment)

  dummy_predict_op = dummy_predict_op[0]
  return (compile_op, dummy_predict_op, host_calls, captured_scaffold_fn,
          captured_predict_hooks.get())


def _wrap_computation_in_while_loop(device, op_fn):
  """Wraps the ops generated by `op_fn` in tf.while_loop."""

  def computation(i):
    with ops.control_dependencies(op_fn()):
      return i + 1

  iterations_per_loop_var = _create_or_get_iterations_per_loop()
  # By setting parallel_iterations=1, the parallel execution in while_loop is
  # basically turned off.
  with ops.device(device):
    iterations = array_ops.identity(iterations_per_loop_var)
    return control_flow_ops.while_loop(
        lambda i: i < iterations,
        computation, [constant_op.constant(0)],
        parallel_iterations=1)


def _wrap_computation_in_while_loop_with_stopping_signals(device, op_fn):
  """Wraps the ops generated by `op_fn` in tf.while_loop."""

  def cond(scalar_stopping_signal):
    return math_ops.logical_not(
        _StopSignals.should_stop(scalar_stopping_signal))

  def computation(unused_scalar_stopping_signal):
    return_value = op_fn()
    execute_ops = return_value['ops']
    signals = return_value['signals']
    with ops.control_dependencies(execute_ops):
      return _StopSignals.as_scalar_stopping_signal(signals)

  # By setting parallel_iterations=1, the parallel execution in while_loop is
  # basically turned off.
  with ops.device(device):
    return control_flow_ops.while_loop(
        cond,
        computation, [_StopSignals.NON_STOPPING_SIGNAL],
        parallel_iterations=1)


def _validate_tpu_training_graph():
  """Validate graph before running distributed training.

  Raises:
    ValueError: If the graph seems invalid for running on device
  """
  if control_flow_util.ENABLE_CONTROL_FLOW_V2:
    return  # b/124241278

  operations = ops.get_default_graph().get_operations()

  # Check if there is atleast one CrossReplicaSum operation in the graph
  # This should be introduced by using the CrossShardOptimizer wrapper
  cross_replica_sum_ops = [
      o for o in operations if o.type == _CROSS_REPLICA_SUM_OP
  ]
  if not cross_replica_sum_ops:
    raise ValueError(
        'CrossShardOptimizer must be used for model training on TPUs.')


class _CapturedObject(object):
  """A placeholder to capture an object.

  This is useful when we need to capture a Python object in the Tensorflow
  control flow body function and use it outside the control flow.
  """

  def __init__(self):
    self._object = None
    self._captured = False

  def capture(self, o):
    if self._captured:
      raise RuntimeError(
          'InternalError: Object can capture only once. Please file bug.')

    self._captured = True
    self._object = o

  def get(self):
    if not self._captured:
      raise RuntimeError(
          'InternalError: Object is not captured properly before `get`. '
          'Please file bug.')
    return self._object


def _get_scaffold(captured_scaffold_fn):
  """Retrieves the Scaffold from `captured_scaffold_fn`."""
  with _CapturingContext(message='Inside scaffold_fn'):
    scaffold_fn = captured_scaffold_fn.get()
    if scaffold_fn:
      scaffold = scaffold_fn()
      if scaffold is None:
        raise ValueError(
            'TPUEstimatorSpec.scaffold_fn returns None, which is not allowed')
    else:
      scaffold = None

  if scaffold:
    wrapped_finalize = scaffold.finalize

    def _finalize():
      with _CapturingContext('Inside Scaffold.finalize'):
        wrapped_finalize()

    scaffold.finalize = _finalize
  return scaffold


class _CapturingContext(control_flow_ops.ControlFlowContext):
  """Tracks references to Tensors defined in TPU replication."""

  def __init__(self, message):
    control_flow_ops.ControlFlowContext.__init__(self)
    self._message = message

  def to_control_flow_context_def(self, context_def, export_scope=None):
    # pylint: disable=useless-super-delegation
    # NOTE(slebedev): the method is required by `ControlFlowContext`.
    super(_CapturingContext, self).to_control_flow_context_def(
        context_def, export_scope)

  def AddOp(self, op):  # pylint: disable=invalid-name
    for c in op.inputs:
      if tpu._TPU_REPLICATE_ATTR in c.op.node_def.attr:  # pylint: disable=protected-access
        raise ValueError('{}: Op {} depends on TPU computation {}, '
                         'which is not allowed.'.format(self._message, op, c))

  def __enter__(self):
    # pylint: disable=protected-access
    self._g = ops.get_default_graph()
    self._old = self._g._get_control_flow_context()
    self._g._set_control_flow_context(self)
    # pylint: enable=protected-access

  def __exit__(self, _, __, ___):  # pylint: disable=invalid-name
    self._g._set_control_flow_context(self._old)  # pylint: disable=protected-access


class _Inputs(object):
  """A data structure representing the input_fn returned values.

  This also supports the returned value from input_fn as `Dataset`.
  """

  def __init__(self, features=None, labels=None, dataset=None, signals=None):
    if dataset is not None and (features is not None or labels is not None or
                                signals is not None):
      raise RuntimeError('Internal Error: Either (features and labels) or '
                         'dataset should be provided, not both. Please file '
                         'bug')

    self._features = features
    self._labels = labels
    self._signals = signals

    self._dataset = dataset
    self._iterator = None

  @staticmethod
  def from_input_fn(return_values):
    """Returns an `_Inputs` instance according to `input_fn` return value."""
    if isinstance(return_values, dataset_ops.DatasetV2):
      dataset = return_values
      return _Inputs(dataset=dataset)

    features, labels = _Inputs._parse_inputs(return_values)
    return _Inputs(features, labels)

  @staticmethod
  def _parse_inputs(return_values):
    if isinstance(return_values, tuple):
      features, labels = return_values
    else:
      features, labels = return_values, None
    return features, labels

  @property
  def is_dataset(self):
    """Returns True if the return value from input_fn is Dataset."""
    return self._dataset is not None

  def dataset_initializer(self):
    """Returns the dataset's initializer.

    The initializer must be run before calling `features_and_labels`.
    """
    self._iterator = dataset_ops.make_initializable_iterator(self._dataset)
    return self._iterator.initializer

  def features_and_labels(self):
    """Gets `features` and `labels`."""
    if self.is_dataset:
      if self._iterator is None:
        raise RuntimeError('Internal error: Must run dataset_initializer '
                           'before calling features_and_labels(). Please file '
                           'a bug!')
      return _Inputs._parse_inputs(self._iterator.get_next())

    return (self._features, self._labels)

  def signals(self):
    return self._signals

  @property
  def dataset(self):
    return self._dataset


class _InputsWithStoppingSignals(_Inputs):
  """Inputs with `_StopSignals` inserted into the dataset."""

  def __init__(self,
               dataset,
               batch_size,
               add_padding=False,
               num_invocations_per_step=1):

    assert dataset is not None
    user_provided_dataset = dataset.map(
        _InputsWithStoppingSignals.insert_stopping_signal(
            stop=False, batch_size=batch_size, add_padding=add_padding))
    if num_invocations_per_step == 1:
      final_batch_dataset = dataset.take(1).map(
          _InputsWithStoppingSignals.insert_stopping_signal(
              stop=True, batch_size=batch_size, add_padding=add_padding))
    else:
      # We append (2 * num_invocations_per_step - 1) batches for exhausting the
      # user_provided_dataset and stop properly.
      # For example, if num_invocations_per_step is 2, we append 3 additional
      # padding batches: b1, b2, b3.
      # If user_provided_dataset contains two batches: a1, a2
      # Step 1: [a1, a2]
      # Step 2: [b1, b2] -> STOP
      # If user_provided_dataset contains three batches: a1, a2, a3.
      # The training loops:
      # Step 1: [a1, a2]
      # Step 2: [a3, b1]
      # Step 3: [b2, b3] -> STOP.
      final_batch_dataset = dataset.take(1).map(
          _InputsWithStoppingSignals.insert_stopping_signal(
              stop=True, batch_size=batch_size, add_padding=add_padding))
      final_batch_dataset = final_batch_dataset.repeat(
          2 * num_invocations_per_step - 1)

      def _set_mask(data_dict):
        signals = data_dict['signals']
        signals['padding_mask'] = array_ops.ones_like(signals['padding_mask'])
        data_dict['signals'] = signals
        return data_dict

      # Mask out the extra batch.
      final_batch_dataset = final_batch_dataset.map(_set_mask)

    dataset = user_provided_dataset.concatenate(final_batch_dataset).prefetch(2)

    super(_InputsWithStoppingSignals, self).__init__(dataset=dataset)
    self._current_inputs = None

  def features_and_labels(self):
    if self._current_inputs is not None:
      raise RuntimeError(
          'Internal Error: The previous inputs have not been properly '
          'consumed. First call features_and_labels, then call signals.')

    inputs_with_signals = self._iterator.get_next()
    features = inputs_with_signals['features']
    labels = inputs_with_signals.get('labels')

    self._current_inputs = inputs_with_signals
    return features, labels

  def signals(self):
    """Returns the `Signals` from `_Inputs`."""
    if self._current_inputs is None:
      raise RuntimeError(
          'Internal Error: The current inputs have not been properly '
          'generated. First call features_and_labels, then call signals.')
    signals = self._current_inputs['signals']
    self._current_inputs = None
    return signals

  @staticmethod
  def insert_stopping_signal(stop, batch_size, add_padding=False):
    """Inserts stopping_signal into dataset via _map_fn.

    Here we change the data structure in the dataset, such that the return value
    is a dictionary now and `features`, `labels`, and `signals` are three
    distinguished keys in that dict. This provides a better structure, which
    eases the process to decompose the inputs (see `features_and_labels`).

    Args:
      stop: bool, state of current stopping signals.
      batch_size: int, batch size.
      add_padding: bool, whether to pad the tensor to full batch size.

    Returns:
      A map_fn passed to dataset.map API.
    """

    def _map_fn(*args):
      """The map fn to insert signals."""
      if len(args) == 1:
        # Unpack the single Tensor/dict argument as features. This is required
        # for the input_fn returns no labels.
        args = args[0]
      features, labels = _Inputs._parse_inputs(args)
      new_input_dict = {}

      if add_padding:
        padding_mask, features, labels = (
            _PaddingSignals.pad_features_and_labels(features, labels,
                                                    batch_size))

        new_input_dict['features'] = features
        if labels is not None:
          new_input_dict['labels'] = labels

      else:
        new_input_dict['features'] = features
        if labels is not None:
          new_input_dict['labels'] = labels
        padding_mask = None

      new_input_dict['signals'] = _StopSignals(
          stop=stop, batch_size=batch_size,
          padding_mask=padding_mask).as_dict()

      return new_input_dict

    return _map_fn


class _StopSignals(object):
  """Signals class holding all logic to handle TPU stopping condition."""

  NON_STOPPING_SIGNAL = False
  STOPPING_SIGNAL = True

  def __init__(self, stop, batch_size, padding_mask=None):
    self._stop = stop
    self._batch_size = batch_size
    self._padding_mask = padding_mask

  def as_dict(self):
    """Returns the signals as Python dict."""
    shape = [self._batch_size, 1]
    dtype = dtypes.bool

    if self._stop:
      stopping = array_ops.ones(shape=shape, dtype=dtype)
    else:
      stopping = array_ops.zeros(shape=shape, dtype=dtype)

    signals = {'stopping': stopping}
    if self._padding_mask is not None:
      signals['padding_mask'] = self._padding_mask
    return signals

  @staticmethod
  def as_scalar_stopping_signal(signals):
    return array_ops.identity(signals['stopping'][0][0])

  @staticmethod
  def should_stop(scalar_stopping_signal):
    """Detects whether scalar_stopping_signal indicates stopping."""
    if isinstance(scalar_stopping_signal, ops.Tensor):
      # STOPPING_SIGNAL is a constant True. Here, the logical_and is just the TF
      # way to express the bool check whether scalar_stopping_signal is True.
      return math_ops.logical_and(scalar_stopping_signal,
                                  _StopSignals.STOPPING_SIGNAL)
    else:
      # For non Tensor case, it is used in SessionRunHook. So, we cannot modify
      # the graph anymore. Here, we use pure Python.
      return bool(scalar_stopping_signal)


class _PaddingSignals(object):
  """Signals class holding all logic to handle padding."""

  @staticmethod
  def pad_features_and_labels(features, labels, batch_size):
    """Pads out the batch dimension of features and labels."""
    real_batch_size = array_ops.shape(
        _PaddingSignals._find_any_tensor(features))[0]

    batch_size_tensor = constant_op.constant(batch_size, dtypes.int32)

    check_greater = check_ops.assert_greater_equal(
        batch_size_tensor,
        real_batch_size,
        data=(batch_size_tensor, real_batch_size),
        message='The real batch size should not be greater than batch_size.')

    with ops.control_dependencies([check_greater]):
      missing_count = batch_size_tensor - real_batch_size

    def pad_single_tensor(tensor):
      """Pads out the batch dimension of a tensor to the complete batch_size."""
      rank = len(tensor.shape)
      assert rank > 0
      padding = array_ops.stack([[0, missing_count]] + [[0, 0]] * (rank - 1))
      padded_shape = (batch_size,) + tuple(tensor.shape[1:])
      padded_tensor = array_ops.pad(tensor, padding)
      padded_tensor.set_shape(padded_shape)
      return padded_tensor

    def nest_pad(tensor_or_dict):
      return nest.map_structure(pad_single_tensor, tensor_or_dict)

    features = nest_pad(features)
    if labels is not None:
      labels = nest_pad(labels)

    padding_mask = _PaddingSignals._padding_mask(real_batch_size, missing_count,
                                                 batch_size)

    return padding_mask, features, labels

  @staticmethod
  def slice_tensor_or_dict(tensor_or_dict, signals):
    """Slice the real Tensors according to padding mask in signals."""

    padding_mask = signals['padding_mask']
    batch_size = array_ops.shape(padding_mask)[0]

    def verify_batch_size(tensor):
      check_batch_size = math_ops.equal(batch_size, tensor.shape[0])
      with ops.control_dependencies([check_batch_size]):
        return array_ops.identity(tensor)

    def slice_single_tensor(tensor):
      rank = len(tensor.shape)
      assert rank > 0
      real_batch_size = batch_size - math_ops.reduce_sum(padding_mask)
      return verify_batch_size(tensor)[0:real_batch_size]

    # As we split the Tensors to all TPU cores and concat them back, it is
    # important to ensure the real data is placed before padded ones, i.e.,
    # order is preserved. By that, the sliced padding mask should have all 0's.
    # If this assertion failed, # the slice logic here would not hold.
    sliced_padding_mask = slice_single_tensor(padding_mask)
    assert_padding_mask = math_ops.equal(
        math_ops.reduce_sum(sliced_padding_mask), 0)

    with ops.control_dependencies([assert_padding_mask]):
      should_stop = _StopSignals.should_stop(
          _StopSignals.as_scalar_stopping_signal(signals))

    is_full_batch = math_ops.equal(math_ops.reduce_sum(padding_mask), 0)

    def slice_fn(tensor):
      # If the current batch is full batch or part of stopping signals, we do
      # not need to slice to save performance.
      return control_flow_ops.cond(
          math_ops.logical_or(should_stop, is_full_batch),
          (lambda: verify_batch_size(tensor)),
          (lambda: slice_single_tensor(tensor)))

    return nest.map_structure(slice_fn, tensor_or_dict)

  @staticmethod
  def _find_any_tensor(batch_features):
    tensors = [
        x for x in nest.flatten(batch_features) if isinstance(x, ops.Tensor)
    ]
    if not tensors:
      raise ValueError('Cannot find any Tensor in features dict.')
    return tensors[0]

  @staticmethod
  def _padding_mask(real_batch_size, missing_count, batch_size):
    padding_mask = array_ops.concat([
        array_ops.zeros((real_batch_size,), dtype=dtypes.int32),
        array_ops.ones((missing_count,), dtype=dtypes.int32)
    ],
                                    axis=0)
    padding_mask.set_shape((batch_size,))
    return padding_mask


def _verify_cross_hosts_transfer_size(tensor_dict, message):
  total_size = 0
  tensor_structure = {}
  for key, tensor in tensor_dict.items():
    shape = tensor.shape
    size = np.product(shape) * tensor.dtype.size
    tensor_structure[key] = shape
    total_size += size
  if total_size >= _ONE_GIGABYTE:
    raise ValueError(
        '{} The transfer size is larger than the protobuf limit. Please '
        'consider to use Tensors with smaller shapes or reduce batch '
        'size. Given:\n'
        '{}'.format(
            message, '\n'.join([
                ' -- Key: {}, Shape: {}'.format(k, v)
                for k, v in tensor_structure.items()
            ])))


def _add_item_to_params(params, key, value):
  """Adds a new item into `params`."""
  if hasattr(params, 'set_hparam'):
    # For HParams, we need to use special API.
    if key in params:
      params.set_hparam(key, value)
    else:
      params.add_hparam(key, value)
  else:
    # Now params is Python dict.
    params[key] = value


def export_estimator_savedmodel(estimator,
                                export_dir_base,
                                serving_input_receiver_fn,
                                assets_extra=None,
                                as_text=False,
                                checkpoint_path=None):
  """Export `Estimator` trained model for TPU inference.

  Args:
    estimator: `Estimator` with which model has been trained.
    export_dir_base: A string containing a directory in which to create
      timestamped subdirectories containing exported SavedModels.
    serving_input_receiver_fn: A function that takes no argument and returns a
      `ServingInputReceiver` or `TensorServingInputReceiver`.
    assets_extra: A dict specifying how to populate the assets.extra directory
      within the exported SavedModel, or `None` if no extra assets are needed.
    as_text: whether to write the SavedModel proto in text format.
    checkpoint_path: The checkpoint path to export.  If `None` (the default),
      the most recent checkpoint found within the model directory is chosen.

  Returns:
    The string path to the exported directory.
  """
  # `TPUEstimator` requires `tpu_config.RunConfig`, so we cannot use
  # `estimator.config`.
  config = tpu_config.RunConfig(model_dir=estimator.model_dir)
  est = TPUEstimator(
      estimator._model_fn,  # pylint: disable=protected-access
      config=config,
      params=estimator.params,
      use_tpu=True,
      train_batch_size=2048,  # Does not matter.
      eval_batch_size=2048,  # Does not matter.
  )
  return est.export_saved_model(export_dir_base, serving_input_receiver_fn,
                                assets_extra, as_text, checkpoint_path)


def model_fn_inference_on_tpu(model_fn,
                              features,
                              labels=None,
                              config=None,
                              params=None,
                              batch_config=None):
  """Convenience wrapper for export_saved_model API v2 for a model_fn.

  It attempts to execute the entire model function on the TPU for prediction.
  Note that this does not support features which are SparseTensors. If you have
  SparseTensor features, consider partitioning your model function further and
  use inference_on_tpu.

  Args:
    model_fn: the model_fn for which we want to inference on TPU.
    features: a tensor or dict of tensors, serves as the feature inputs to the
      model.
    labels: a tensor or dict of tensors, serves as the labels inputs to the
      model.
    config: auxiliary config to the Estimator.
    params: hparams that we want to pass to the model_fn.
    batch_config: a named tuple to wrap the inference batching configuration
      inputs.

  Returns:
    An EstimatorSpec containing the outputs in export_outputs and predictions.
  """
  computation, capture = _build_computation_for_inference(
      model_fn, labels, config, params)
  tensors = call_computation(
      features,
      computation,
      batch_config=batch_config)
  estimator_spec, export_outputs_dict, predictions_dict, none_indices = (
      capture.get())
  predictions_list = tensors[:len(predictions_dict)]
  export_outputs_list_without_none = tensors[len(predictions_dict):]

  # Reinsert `None`s which we've taken out in
  # `_build_computation_for_inference()`.
  export_outputs_list = []
  while none_indices or export_outputs_list_without_none:
    if none_indices and none_indices[0] == len(export_outputs_list):
      export_outputs_list.append(None)
      none_indices.pop(0)
    else:
      export_outputs_list.append(export_outputs_list_without_none.pop(0))

  # Reconstruct `export_outputs` with updated tensors.
  new_export_outputs_dict = nest.pack_sequence_as(export_outputs_dict,
                                                  export_outputs_list)
  export_outputs = estimator_spec.export_outputs
  new_export_outputs = collections.OrderedDict(
      (k, _clone_export_output_with_tensors(export_outputs[k], v))
      for k, v in six.iteritems(new_export_outputs_dict))
  # Reconstruct `predictions` with updated tensors.
  new_predictions = nest.pack_sequence_as(predictions_dict, predictions_list)
  if (len(new_predictions) == 1 and
      _KEY_WHEN_PREDICTIONS_IS_A_TENSOR in new_predictions):
    new_predictions = new_predictions[_KEY_WHEN_PREDICTIONS_IS_A_TENSOR]

  return estimator_spec._replace(
      export_outputs=new_export_outputs, predictions=new_predictions)


def _build_computation_for_inference(model_fn,
                                     labels,
                                     config,
                                     params):
  """Builds the computation with calls the model_fn for inference."""
  capture = _CapturedObject()

  def computation(computation_input):
    """Computation to be passed to `TPUPartitionedCall()`."""
    tpu_computation, tpu_capture = _build_tpu_computation_for_inference(
        model_fn, computation_input, labels, config, params)

    tensors_on_cpu = tpu.rewrite(tpu_computation)
    tpu.prune_unconnected_ops_from_xla(ops.get_default_graph())

    (estimator_spec, export_outputs_dict, export_outputs_list,
     predictions_dict) = (
         tpu_capture.get())
    predictions_list = tensors_on_cpu[:len(predictions_dict)]
    export_outputs_tpu_on_cpu_list = tensors_on_cpu[len(predictions_dict):]

    # Reconstruct tensors used in export_outputs, with TPU tensors replaced
    # with their CPU counterpart returned from `rewrite_for_inference()`.
    # `function.Defun()` does not like `None`s in return values, so we leave
    # `None`s out but record their positions for later reconstruction.
    export_outputs_list_without_none = []
    none_indices = []
    for i, t in enumerate(export_outputs_list):
      if t is None:
        none_indices.append(i)
      else:
        export_outputs_list_without_none.append(
            export_outputs_tpu_on_cpu_list.pop(0))

    capture.capture(
        (estimator_spec, export_outputs_dict, predictions_dict, none_indices))
    return predictions_list + export_outputs_list_without_none

  return computation, capture


def _build_tpu_computation_for_inference(model_fn, features, labels, config,
                                         params):
  """Builds the TPU computation for inference on TPU."""
  capture = _CapturedObject()

  def computation():
    """Compute tpu tensors used in export_outputs.

    Passed to rewrite_for_inference so that model_fn will be called under
    the rewriting contexts. Only tpu tensors are returned, but export_outputs
    and scaffold are captured.

    Returns:
       A list of Tensors used in export_outputs and not marked for
       outside_compilation.
    """
    # We should only call model fn once and it should be inside `computation`
    # so that building the graph will happen under `rewrite_for_inference`.

    model_fn_args = function_utils.fn_args(model_fn)
    kwargs = {}
    # Makes deep copy with `config` and params` in case user mutates them.
    if 'labels' in model_fn_args:
      kwargs['labels'] = labels
    if 'mode' in model_fn_args:
      kwargs['mode'] = model_fn_lib.ModeKeys.PREDICT
    if 'config' in model_fn_args:
      kwargs['config'] = config
    if 'params' in model_fn_args:
      kwargs['params'] = params
    estimator_spec = model_fn(features, **kwargs)

    # We pick the TPU tensors out from `export_output` and later return them
    # from `computation` for rewriting.
    export_outputs_dict = collections.OrderedDict(
        (k, _export_output_to_tensors(v))
        for k, v in six.iteritems(estimator_spec.export_outputs))
    export_outputs_list = nest.flatten(export_outputs_dict)
    export_outputs_tpu_list = [t for t in export_outputs_list if t is not None]

    if isinstance(estimator_spec.predictions, dict):
      predictions_dict = collections.OrderedDict(
          (k, v) for k, v in six.iteritems(estimator_spec.predictions))
    else:
      predictions_dict = {
          _KEY_WHEN_PREDICTIONS_IS_A_TENSOR: estimator_spec.predictions
      }
    predictions_list = nest.flatten(predictions_dict)

    # We cannot return everything we want through the return values, so
    # capture the rest here for later use.
    capture.capture((estimator_spec, export_outputs_dict, export_outputs_list,
                     predictions_dict))
    return predictions_list + export_outputs_tpu_list

  return computation, capture


def inference_on_tpu(computation,
                     inputs_to_tpu,
                     num_batch_threads,
                     max_batch_size,
                     batch_timeout_micros,
                     allowed_batch_sizes=None,
                     max_enqueued_batches=10):
  """Convenient wrapper for export_saved_model API v2 to wrap TPU computation.

  It puts computation on TPU, add batching around it and round robin computation
  between TPU cores.

  See tpu_estimator_test.py for an example.

  Args:
    computation: computation to be put on TPU, which takes inputs_to_tpu as
      arguments.
    inputs_to_tpu: a list of tensors as input to computation.
    num_batch_threads: Number of scheduling threads for processing batches of
      work. Determines the number of batches processed in parallel.
    max_batch_size: Batch sizes will never be bigger than this.
    batch_timeout_micros: Maximum number of microseconds to wait before
      outputting an incomplete batch.
    allowed_batch_sizes: Optional list of allowed batch sizes. If left empty,
      does nothing. Otherwise, supplies a list of batch sizes, causing the op to
      pad batches up to one of those sizes. The entries must increase
      monotonically, and the final entry must equal max_batch_size.
    max_enqueued_batches: The maximum depth of the batch queue. Defaults to 10.

  Returns:
    The unbatched computation output Tensors.
  """

  @batch_ops.batch_function(num_batch_threads, max_batch_size,
                            batch_timeout_micros, allowed_batch_sizes,
                            max_enqueued_batches)
  def batched_tpu_computation(*args):

    @function.Defun(capture_resource_var_by_value=False)
    def tpu_computation():
      return tpu.rewrite(computation, args)

    return tpu_functional.TPUPartitionedCall(
        args=tpu_computation.captured_inputs,
        device_ordinal=tpu_ops.tpu_ordinal_selector(),
        Tout=[o.type for o in tpu_computation.definition.signature.output_arg],
        f=tpu_computation)

  return batched_tpu_computation(*inputs_to_tpu)
