# coding=utf-8
# Copyright 2022 Google LLC.
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

"""Evaluation loop for Pax model."""

import collections
import contextlib
import functools
import os
import sys
import time
import typing
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from absl import flags
from absl import logging
from clu import platform
import jax
from jax.experimental import maps
from jax.experimental import multihost_utils
import numpy as np
from paxml import base_experiment
from paxml import base_metrics
from paxml import checkpoint_pb2
from paxml import io_utils
from paxml import metric_tracker_utils as trk_utils
from paxml import metric_utils
from paxml import seqio_input
from paxml import summary_utils
from paxml import tasks_lib
from paxml import trainer_lib
from paxml import tuning_lib
from praxis import base_hyperparams
from praxis import base_input
from praxis import base_layer
from praxis import optimizer_prefix_vectorization
from praxis import py_utils
from praxis import pytypes
from praxis import train_states
import tensorflow.compat.v2 as tf
import tensorflow_datasets as tfds

from paxml import checkpoints  # mapped to internal

CheckpointType = checkpoint_pb2.CheckpointType
WeightedScalars = pytypes.WeightedScalars
WeightedScalarsList = pytypes.WeightedScalarsList
Metrics = pytypes.Metrics
NestedMap = py_utils.NestedMap
JTensor = pytypes.JTensor
NestedJTensor = pytypes.NestedJTensor
NestedPartitionSpec = pytypes.NestedPartitionSpec
PRNGKey = pytypes.PRNGKey
TrainState = train_states.TrainState
SummaryWriter = tf.summary.SummaryWriter
instantiate = base_hyperparams.instantiate
PMAP_PARALLEL_AXIS_NAME = base_layer.PMAP_PARALLEL_AXIS_NAME
NO_PREFIX_KEY = optimizer_prefix_vectorization.NO_PREFIX_KEY


def _is_vectorized(states: train_states.TrainState) -> bool:
  """Determines whether it is a vectorized model."""
  if not states.opt_states:
    raise ValueError(
        'cannot decide if it is vectorized model without opt_states')
  return NO_PREFIX_KEY in states.opt_states[0]


def has_ema(task_p: tasks_lib.SingleTask.HParams) -> bool:
  """Determines whether ema is used or not."""
  return task_p.train.learner.optimizer.ema_decay > 0.


def extract_ema(
    model_states: train_states.TrainState) -> train_states.TrainState:
  """Finds the ema state from optimizer states."""
  if len(model_states.opt_states) != 1:
    raise ValueError('EMA currently only supports a single learner (got '
                     f'`{len(model_states.opt_states)}`).')
  is_vectorized = _is_vectorized(model_states)
  if not is_vectorized:
    for v in model_states.opt_states[0]:
      if isinstance(v, dict) and 'ema' in v:
        return TrainState(step=model_states.step, mdl_vars=v.ema, opt_states={})
  else:
    ret = None
    # For vectorized model, the structure looks like this:
    # opt_states: [{'no_prefix': ({'count': '', 'ema': {'params': {'ctcloss':
    # It is a list of dictionaries. The key corresponds to the #stages.
    # Here the ema is constructed by combining the ema state from all those
    # dictionaries. Each parameter belongs to one dictionary and is labelled as
    # masked node in others.
    for item in model_states.opt_states[0].values():
      if isinstance(item, tuple):
        for v in item:
          if isinstance(v, dict) and 'ema' in v:
            if ret is None:
              ret = v.ema
            else:
              ret = jax.tree_map(
                  lambda x, y: y if py_utils.is_optax_masked_node(x) else x,
                  ret,
                  v.ema,
                  is_leaf=py_utils.is_optax_masked_node)
    if ret is not None:
      return TrainState(step=model_states.step, mdl_vars=ret, opt_states={})
  raise ValueError('Could not find EMA states in `%r`.' %
                   model_states.opt_states)


def trim_opt_states(
    model_states: train_states.TrainState) -> train_states.TrainState:
  """Trim the optimizer states from a TrainState instance."""
  return train_states.TrainState(
      step=model_states.step, mdl_vars=model_states.mdl_vars, opt_states={})


def run_eval_one_step(eval_inputs: NestedJTensor,
                      eval_step: Callable[[NestedJTensor], Any],
                      reshard_inputs: Optional[bool] = False):
  """Runs eval on entire batch of eval inputs or for one step.

  Args:
    eval_inputs: `NestedJTensor` of eval inputs.
    eval_step: The eval step which evaluates the model on eval inputs.
    reshard_inputs: Whether to reshard inputs (in pmap) or not.

  Returns:
    Tuple of eval loss, mean metrics and eval summaries.
  """
  if reshard_inputs:
    eval_inputs = tf.nest.map_structure(py_utils.reshard, eval_inputs)
  _, loss, weighted_scalars, per_example_output, summary_tensors = eval_step(
      eval_inputs)
  return loss, weighted_scalars, per_example_output, summary_tensors


def run_eval_loop_over_test_splits(
    num_steps: List[int],
    eval_step: Callable[[NestedJTensor], Any],
    summary_writers: List[SummaryWriter],
    step: int,
    model_inputs: List[base_input.BaseInput],
    eval_inputs_pspecs=None,
    eval_inputs_shape=None,
    global_mesh=None,
    reshard_inputs: Optional[bool] = False,
    create_gda_for_inputs: bool = False
) -> Tuple[List[Dict[str, float]],  # eval metrics.
           List[Optional[Dict[str, float]]],  # eval scoring metrics.
           List[int]  # performed eval steps.
          ]:
  """Run evaluation in a loop over a list of test sets.

  Args:
    num_steps: A list of steps for each test split to evaluate on.
    eval_step: The eval step function which to call to evaluate the model.
    summary_writers: The summary writer objects to log summaries.
    step: The step at which we are evaling the model.
    model_inputs: List of BaseInput instances.
    eval_inputs_pspecs: PartitionSpec for eval inputs.
    eval_inputs_shape: Global shape of eval inputs
    global_mesh: Device mesh used by pjit.
    reshard_inputs: Whether to reshard inputs.
    create_gda_for_inputs: Whether to create GDAs for model inputs.

  Returns:
    A tuple of (a list of eval metrics,
                a list of optional scoring metrics (seqio)
                a list of integer as performed evaluation steps).
      Items from each list are aligned with the `model_inputs`.
  """
  # If reshard_inputs = True, meaning this is called from pmap, hence we need to
  # unreplicate metrics for reporting.
  eval_metrics_list = []
  eval_scoring_metrics_list = []
  num_eval_steps = []
  for split, num_split_steps in enumerate(num_steps):
    logging.info('Starting eval data split=%d (%s) with num_steps=%d',
                 split, model_inputs[split].hparams.name, num_split_steps)
    # Reset loss and summary tensors for each test split.
    loss = []
    summary_tensors = {}
    metrics = collections.defaultdict(list)
    step_num = 0
    per_example_scores = []
    # Use num_split_steps < 0 to indicate running all of the input until
    # out of range.
    while num_split_steps < 0 or step_num < num_split_steps:
      step_num += 1
      try:
        eval_inputs = model_inputs[split].get_next_padded()
      except (tf.errors.OutOfRangeError, StopIteration):
        if num_split_steps > 0:
          raise
        logging.info('Exhausted eval data split=%d after %d steps', split,
                     step_num - 1)
        model_inputs[split].reset()
        break

      if global_mesh and create_gda_for_inputs:
        py_utils.assert_same_shape_and_dtype(
            eval_inputs_shape,
            tf.nest.map_structure(py_utils.get_global_input_shape_dtype,
                                  eval_inputs))
        eval_inputs = py_utils.create_gda(eval_inputs, eval_inputs_shape,
                                          global_mesh, eval_inputs_pspecs)
      # TODO(bencaine): Rename eval_metrics here weighted scalars?
      (eval_loss, eval_metrics, per_example_output,
       eval_summary_tensors) = run_eval_one_step(
           eval_inputs, eval_step, reshard_inputs=reshard_inputs)
      eval_loss = py_utils.maybe_unreplicate_for_fully_replicated(eval_loss)
      eval_metrics = py_utils.maybe_unreplicate_for_fully_replicated(
          eval_metrics)
      per_example_output = py_utils.maybe_unreplicate_for_fully_replicated(
          per_example_output)
      eval_summary_tensors = py_utils.maybe_unreplicate_for_fully_replicated(
          eval_summary_tensors)
      per_example_scores.append(per_example_output)
      loss += [eval_loss]
      eval_summary_tensors = summary_utils.flatten_summary_dict(
          eval_summary_tensors)
      for k, v in eval_summary_tensors:
        if k in summary_tensors:
          summary_tensors[k] += [v]
        else:
          summary_tensors[k] = [v]
      for k in eval_metrics:
        metrics[k].append(eval_metrics[k])

    eval_scoring_metrics = None
    if seqio_input.should_process_outputs(model_inputs[split]):
      eval_scoring_metrics = seqio_input.process_outputs(
          model_inputs[split], per_example_scores, summary_writers[split],
          seqio_input.MetricType.SCORE, step)

    loss = np.array(loss)
    for k in summary_tensors:
      summary_tensors[k] = np.array([np.asarray(t) for t in summary_tensors[k]])
    loss = np.mean(loss, axis=0)
    logging.info('step_i: %d, eval test split %s loss: %s', step, split, loss)
    for key, values in metrics.items():
      # `metric_utils.as_float` computes the average from a list of weighted
      # scalars.
      weighted_average = metric_utils.as_float(values)
      sum_metric_weights = np.sum(np.stack([v[1] for v in values]))
      logging.info('  %s=%f (weight=%f)', key, weighted_average,
                   sum_metric_weights.item())
    summary_utils.write_summary_entry(summary_writers[split], step, loss,
                                      metrics, summary_tensors)
    eval_metrics_list.append(metric_utils.as_float_dict(metrics))
    eval_scoring_metrics_list.append(eval_scoring_metrics)
    num_eval_steps.append(step_num)
  return (eval_metrics_list, eval_scoring_metrics_list, num_eval_steps)


def evaluate(experiment_config: base_experiment.BaseExperiment,
             job_log_dir: Optional[str],
             maybe_use_persistence_checkpointing: bool,
             early_stopping_fn: Optional[trainer_lib.EarlyStoppingFn] = None,
             use_orbax: bool = False) -> None:
  """Runs the evaluation loop on the entire eval data set.

  Args:
    experiment_config: an instance of BaseExperiment for the experiment to
      evaluate.
    job_log_dir: The directory for the job logs.
    maybe_use_persistence_checkpointing: If set, it will try to use
      persistence-based checkpointing if suitable.
    early_stopping_fn: An optional callable object for reporting eval metrics
      and determining whether to early stop current training. The callable
      object has signature: (metrics, running_mode, ckpt_step, is_final_ckpt) ->
      should_stop_early.
    use_orbax: Enables checkpointing backed by Orbax.
  """
  task_p = experiment_config.task()
  task_p = typing.cast(tasks_lib.SingleTask.HParams, task_p)
  model_p = task_p.model
  eval_input_p = [v for v in experiment_config.datasets() if not v.is_training]
  if not eval_input_p:
    logging.info('No eval datasets defined. Returning early.')
    return
  for inp in eval_input_p:
    if inp.num_infeed_hosts == 0:
      inp.num_infeed_hosts = jax.process_count()
    inp.infeed_host_index = jax.process_index()

  if model_p.mesh_shape is not None:
    checkpoint_type = checkpoints.retrieve_checkpoint_type(
        maybe_use_persistence_checkpointing, task_p)
    evaluate_spmd_model(
        task_p,
        eval_input_p,
        job_log_dir,
        checkpoint_type,
        early_stopping_fn,
        use_orbax=use_orbax)
  else:
    evaluate_pmap_model(
        task_p,
        eval_input_p,
        job_log_dir,
        early_stopping_fn,
        use_orbax=use_orbax)


class _PmapEvalRunner:
  """A runner class that runs evaluate with pmap.

  Example usage:

    (replicated_model_states, train_state_global_shapes,
     prng_key) = _PmapEvalRunner.get_model_states(
        jax_task, prng_key, sample_inputs, checkpoint_dir, use_ema,
        track_metric)

    runner = _PmapEvalRunner(task_p, eval_input_params, jax_task, prng_key)
    metrics_list, eval_scoring_metrics_list, num_eval_steps = (
        runner.run_one_step(
            replicated_model_states, sample_inputs, eval_summary_writers))
  """

  def __init__(self, task_p: tasks_lib.SingleTask.HParams,
               eval_input_p: Sequence[base_input.BaseInput.HParams],
               jax_task: tasks_lib.SingleTask, pmap_prng_key: PRNGKey):
    self._eval_input_p = eval_input_p
    self._task_p = task_p
    if not self._eval_input_p:
      return
    self._jax_task = jax_task
    self._eval_input_pipelines = [
        instantiate(input_p) for input_p in eval_input_p
    ]
    trainer_lib.check_unique_names(self._eval_input_pipelines)
    self._eval_num_steps = [
        -1 if p.reset_for_eval else p.eval_loop_num_batches
        for p in eval_input_p
    ]
    self._run_pmap(pmap_prng_key)

  @classmethod
  def get_model_states(
      cls,
      jax_task: tasks_lib.SingleTask,
      prng_key: PRNGKey,
      sample_inputs: NestedJTensor,
      checkpoint_dir: str,
      checkpoint_step: Optional[int],
      use_ema: bool,
      track_metric: bool,
      use_orbax: bool = False
  ) -> Tuple[train_states.TrainState, train_states.TrainState, PRNGKey]:
    """Returns the (replicated) model states."""
    prng_key, init_key = jax.random.split(prng_key)

    # Restore flax checkpoints still required bak variables in TrainState
    var_weight_hparams = jax_task.model.abstract_init_with_metadata(
        init_key, sample_inputs, do_eval=True)
    # Note: `discard_opt_states` is not supported when restoring pmap
    # checkpoints. We must restore the entire checkpoint and then trim the
    # unrelevant states.
    global_shapes = jax_task.create_train_state_unpadded_shapes(
        var_weight_hparams)
    # Pmap does not use GDA, and so global_mesh and mesh_axes are None.
    if py_utils.pmap_use_tensorstore():
      model_states = tasks_lib.restore_pmap_from_tensorstore(
          global_shapes,
          checkpoint_dir,
          step=checkpoint_step,
          use_orbax=use_orbax)
    else:
      model_states = checkpoints.restore_checkpoint(
          global_shapes,
          checkpoint_dir,
          step=checkpoint_step,
          use_orbax=use_orbax)
    if model_states is None:
      model_states = trainer_lib.initialize_model_state(
          jax_task,
          init_key,
          sample_inputs,
          discard_opt_states=not use_ema,
          is_eval=True)
    elif not use_ema and not track_metric:
      model_states = trim_opt_states(model_states)
    if use_ema:
      model_states = extract_ema(model_states)
    replicated_model_states = trainer_lib.replicate_model_state(model_states)
    del model_states  # Unused at that point.
    logging.info('replicated_model_states: %s',
                 jax.tree_map(lambda x: x.shape, replicated_model_states))
    # From now on, different replicas should use different random seeds.
    # Here, each process will have its unique prng_key.
    # prng_key will be further split so that each core on a host will get
    # different prng_key.
    prng_key = jax.random.fold_in(prng_key, jax.process_index())
    logging.info('root prng_key: %s', prng_key)
    return replicated_model_states, global_shapes, prng_key

  def _run_pmap(self, prng_key: PRNGKey):
    """Calls pmap on the eval one step function."""
    if not self._eval_input_p:
      return

    def eval_step(mdl_states, prng_key, inputs):
      return trainer_lib.eval_step_single_learner(
          self._jax_task,
          mdl_states,
          prng_key,
          inputs,
          fprop_dtype=self._jax_task.model.fprop_dtype)

    num_devices = jax.local_device_count()
    prng_key, eval_key = jax.random.split(prng_key)
    self._eval_prng_seed = jax.random.split(eval_key, num=num_devices)
    logging.info('eval prng_seed: %s', self._eval_prng_seed)

    self._pmap_eval_step = jax.pmap(
        eval_step, axis_name=PMAP_PARALLEL_AXIS_NAME)

  def run_one_step(
      self,
      replicated_model_states: train_states.TrainState,
      eval_summary_writers: List[SummaryWriter],
  ) -> Tuple[List[Dict[str, float]],  # eval metrics list.
             List[Optional[Dict[str, float]]],  # seqio metrics list.
             List[int]  # actual eval steps.
            ]:
    """Runs evaluate for one step for all test splits."""
    if not self._eval_input_p:
      return [], [], []
    step_i = int(
        py_utils.maybe_unreplicate_for_fully_replicated(
            replicated_model_states.step))

    def eval_step_fn(inputs):
      # TODO(pax): shall we eval all sub-models during eval?
      return self._pmap_eval_step(replicated_model_states, self._eval_prng_seed,
                                  inputs)

    # Run the eval loop.
    return run_eval_loop_over_test_splits(
        self._eval_num_steps,
        eval_step_fn,
        eval_summary_writers,
        step_i,
        self._eval_input_pipelines,
        reshard_inputs=True)


def evaluate_pmap_model(task_p: tasks_lib.SingleTask.HParams,
                        eval_input_p: Sequence[base_input.BaseInput.HParams],
                        job_log_dir: str,
                        early_stopping_fn: Optional[
                            trainer_lib.EarlyStoppingFn] = None,
                        use_orbax: bool = False) -> None:
  """Runs the evaluation loop on the entire test dataset for PMAP model.

  Args:
    task_p: Params for the task encapsulating the data parallel model.
    eval_input_p: List of params for the eval data input pipelines.
    job_log_dir: Directory for the job logs.
    early_stopping_fn: An optional callable object for reporting metrics and
      determining whether to early stop current training. The callable object
      has signature: (metrics, running_mode, ckpt_step, is_final_ckpt) ->
      should_stop_early.
    use_orbax: Enables checkpointing backed by Orbax.
  """
  logging.info('Using pmap for data parallelism.')

  if not eval_input_p:
    return

  checkpoint_dir = os.path.join(job_log_dir, 'checkpoints')
  checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(checkpoint_dir)
  jax_task = instantiate(task_p)
  use_ema = has_ema(task_p)

  # TODO(pax-dev): Investigate if we can use model input specs
  # instead of instantiating this input pipeline.
  sample_model_inputs = instantiate(eval_input_p[0]).get_next_padded()

  prng_key = jax.random.PRNGKey(1234)
  (replicated_model_states, train_state_global_shapes,
   prng_key) = _PmapEvalRunner.get_model_states(
       jax_task,
       prng_key,
       sample_model_inputs,
       checkpoint_dir,
       checkpoint_step=checkpoint_step,
       use_ema=use_ema,
       track_metric=False,
       use_orbax=use_orbax)

  runner = _PmapEvalRunner(task_p, eval_input_p, jax_task, prng_key)
  logging.info('Evaluation loop starting...')
  summary_base_dir = os.path.join(job_log_dir, 'summaries')
  summary_eval_dirs = [
      os.path.join(summary_base_dir, f'eval_test_{p.name}')
      for p in eval_input_p
  ]

  last_checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(
      checkpoint_dir)
  with contextlib.ExitStack() as exit_stack:
    eval_summary_writers = [
        exit_stack.enter_context(summary_utils.get_summary_writer(d))
        for d in summary_eval_dirs
    ]

    while True:
      with py_utils.timeit() as eval_period:
        eval_metrics_list, eval_scoring_metrics_list, num_eval_steps = (
            runner.run_one_step(replicated_model_states, eval_summary_writers))

      eval_metrics = tuning_lib.EvalMetrics(
          input_p=eval_input_p,
          metrics_list=eval_metrics_list,
          scoring_metrics_list=eval_scoring_metrics_list,
          steps_per_sec=sum(num_eval_steps) / eval_period.elapsed)

      # If the last check point evaluated matches max train steps, exit.
      if last_checkpoint_step is not None:
        exceeded_ckpt = last_checkpoint_step + task_p.train.save_interval_steps
        is_last_ckpt = exceeded_ckpt > task_p.train.num_train_steps
        if tuning_lib.should_early_stop(
            early_stopping_fn,
            last_checkpoint_step,
            is_last_ckpt,
            eval_metrics=eval_metrics):
          logging.info(
              'Evaluation is early stopped at checkpoint step %d by the'
              'tuner, while the num_train_steps is %d', last_checkpoint_step,
              task_p.train.num_train_steps)
          break
        if is_last_ckpt:
          break
      # Release replicated_model_states.
      del replicated_model_states
      new_checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(
          checkpoint_dir)
      while new_checkpoint_step == last_checkpoint_step:
        logging.info('Sleep before checking for new latest checkpoint.')
        time.sleep(60)
        new_checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(
            checkpoint_dir)
      # There must be a new checkpoint here.
      logging.info('Found new checkpoint at step: %d', new_checkpoint_step)
      if py_utils.pmap_use_tensorstore():
        model_states = tasks_lib.restore_pmap_from_tensorstore(
            train_state_global_shapes,
            checkpoint_dir,
            step=new_checkpoint_step,
            use_orbax=use_orbax)
      else:
        model_states = checkpoints.restore_checkpoint(
            train_state_global_shapes,
            checkpoint_dir,
            step=new_checkpoint_step,
            use_orbax=use_orbax)
      if use_ema:
        model_states = extract_ema(model_states)
      else:
        model_states = trim_opt_states(model_states)
      replicated_model_states = trainer_lib.replicate_model_state(model_states)
      del model_states  # Unused at that point.
      last_checkpoint_step = new_checkpoint_step


class _SpmdEvalRunner:
  """A runner class that runs evaluate with spmd.

  Example usage:

    (partitioned_train_state, partitioned_specs, train_state_global_shapes,
     step_fn, inputs_partition_specs, sample_inputs) = (
        _SpmdEvalRunner.get_model_states_and_step_fn(
            jax_task, init_key, eval_input_params, checkpoint_dir,
            checkpoint_type))
    runner = _SpmdEvalRunner(
        task_p, sample_input_params, jax_task, global_mesh,
        init_key, partitioned_specs)
    eval_metrics_list, eval_scoring_metrics_list, num_eval_steps = (
        runner.run_one_step(partitioned_train_state, eval_summary_writers,
                            eval_key, create_gda_for_inputs))
  """

  def __init__(self,
               task_p: tasks_lib.SingleTask.HParams,
               eval_input_p: Sequence[base_input.BaseInput.HParams],
               jax_task: tasks_lib.SingleTask,
               global_mesh: maps.Mesh,
               init_key: PRNGKey,
               partitioned_specs: train_states.TrainState,
               use_ema: bool = False):
    self._eval_input_p = eval_input_p
    if not self._eval_input_p:
      return
    self._jax_task = jax_task
    self._eval_input_pipelines = [
        instantiate(input_p) for input_p in eval_input_p
    ]
    trainer_lib.check_unique_names(self._eval_input_pipelines)
    self._eval_num_steps = [
        -1 if p.reset_for_eval else p.eval_loop_num_batches
        for p in eval_input_p
    ]
    self._task_p = task_p

    # TODO(pax-dev): Investigate if we can use model input specs
    # instead of instantiating this input pipeline.
    # setup input_shape.
    sample_model_inputs = instantiate(self._eval_input_p[0]).get_next_padded()
    self._inputs_shape = tf.nest.map_structure(
        py_utils.get_global_input_shape_dtype, sample_model_inputs)
    self.global_mesh = global_mesh

    # Will be populated by self.run_pjit() below.
    self._eval_step = None
    self._inputs_partition_specs = None
    if use_ema:
      partitioned_specs = trim_opt_states(partitioned_specs)
    self._run_pjit(init_key, partitioned_specs)

  @classmethod
  def get_model_states_and_step_fn(
      cls,
      jax_task: tasks_lib.SingleTask,
      global_mesh: maps.Mesh,
      init_key: PRNGKey,
      sample_inputs: NestedJTensor,
      checkpoint_dir: str,
      checkpoint_type: CheckpointType,
      checkpoint_step: Optional[int] = None,
      use_ema: bool = False,
      is_decode: bool = False,
      use_orbax: bool = False,
  ) -> Tuple[train_states.TrainState, Optional[train_states.TrainState],
             train_states.TrainState, Any, NestedPartitionSpec, NestedJTensor]:
    """Gets a partitioned model states and the step function."""
    with global_mesh:
      var_weight_hparams = jax_task.model.abstract_init_with_metadata(
          init_key, sample_inputs, do_eval=True)
      train_state_global_shapes = (
          jax_task.create_train_state_padded_shapes(
              var_weight_hparams, discard_opt_states=not use_ema))
      partitioned_specs = jax_task.create_train_state_partition_specs(
          var_weight_hparams, discard_opt_states=not use_ema)
      partitioned_train_state = checkpoints.restore_checkpoint(
          train_state_global_shapes,
          checkpoint_dir,
          global_mesh=global_mesh,
          checkpoint_type=checkpoint_type,
          step=checkpoint_step,
          state_specs=partitioned_specs,
          use_orbax=use_orbax)
      py_utils.sync_global_devices(f'checkpointer:restored:{checkpoint_dir}')

      inputs_shape = tf.nest.map_structure(
          py_utils.get_global_input_shape_dtype, sample_inputs)
      init_key, step_key = jax.random.split(init_key)
      if is_decode:
        step_fn, inputs_partition_specs = (
            trainer_lib.get_partitioned_spmd_model_decode_fn(
                jax_task, init_key, trim_opt_states(partitioned_specs),
                inputs_shape))
      else:
        step_fn, inputs_partition_specs = (
            trainer_lib.get_partitioned_spmd_model_step_fn(
                jax_task,
                step_key,
                trainer_lib.train_state_for_eval_step(partitioned_specs),
                sample_inputs,
                is_eval=True))

      if (jax.config.jax_parallel_functions_output_gda and
          checkpoint_type != CheckpointType.CHECKPOINT_PERSISTENCE):
        sample_inputs = py_utils.create_gda(sample_inputs, inputs_shape,
                                            global_mesh, inputs_partition_specs)

      if partitioned_train_state is None:
        _, partitioned_train_state = (
            trainer_lib.initialize_partitioned_model_states(
                jax_task,
                step_key,
                sample_inputs,
                global_mesh=global_mesh,
                # Note: We currently enforce that the checkpoint to reload via
                # init_checkpoint_rules are in the same format as the checkpoint
                # solution used by the experiment.
                checkpoint_type=checkpoint_type,
                state_specs=partitioned_specs,
                discard_opt_states=True))
      if use_ema:
        partitioned_train_state = extract_ema(partitioned_train_state)
    return (partitioned_train_state, partitioned_specs,
            train_state_global_shapes, step_fn, inputs_partition_specs,
            sample_inputs)

  def _run_pjit(self, init_key: PRNGKey,
                partitioned_specs: train_states.TrainState) -> None:
    """Run pjit on the single step evaluation function."""
    if not self._eval_input_p:
      return
    with self.global_mesh:
      eval_step, inputs_partition_specs = (
          trainer_lib.get_partitioned_spmd_model_step_fn(
              self._jax_task,
              init_key,
              trainer_lib.train_state_for_eval_step(partitioned_specs),
              self._inputs_shape,
              is_eval=True))
      self._eval_step = eval_step
      self._inputs_partition_specs = inputs_partition_specs

  def run_one_step(
      self, partitioned_train_state: train_states.TrainState,
      eval_summary_writers: List[SummaryWriter], eval_key: PRNGKey,
      use_gda: bool
  ) -> Tuple[List[Dict[str, float]],  # eval metrics list.
             List[Optional[Dict[str, float]]],  # eval scoring metrics list.
             List[int]  # performed eval steps.
            ]:
    """Runs evaluate for one step. Requires calling run_pjit() prior."""
    if not self._eval_input_p:
      return [], [], []
    step_i = int(
        py_utils.maybe_unreplicate_for_fully_replicated(
            partitioned_train_state.step))
    eval_step_fn = functools.partial(
        self._eval_step,
        trainer_lib.train_state_for_eval_step(partitioned_train_state),
        eval_key)
    # Run the eval loop.
    with self.global_mesh:
      return run_eval_loop_over_test_splits(
          self._eval_num_steps,
          eval_step_fn,
          eval_summary_writers,
          step_i,
          self._eval_input_pipelines,
          self._inputs_partition_specs,
          self._inputs_shape,
          self.global_mesh,
          reshard_inputs=False,
          create_gda_for_inputs=use_gda)


def evaluate_spmd_model(task_p: tasks_lib.SingleTask.HParams,
                        eval_input_p: Sequence[base_input.BaseInput.HParams],
                        job_log_dir: Optional[str],
                        checkpoint_type: CheckpointType,
                        early_stopping_fn: Optional[
                            trainer_lib.EarlyStoppingFn] = None,
                        use_orbax: bool = False) -> None:
  """Runs the evaluation loop on the entire test dataset for SPMD model.

  Args:
    task_p: Params of the task encapsulating an SPMD model.
    eval_input_p: List of Params for the eval data pipelines.
    job_log_dir: Directory for the job logs.
    checkpoint_type: Type of model checkpointing method to use.
    early_stopping_fn: An optional callable object for reporting metrics
    early_stopping_fn: An optional callable object for reporting metrics and
      determining whether to early stop current training. The callable object
      has signature: (metrics, running_mode, ckpt_step, is_final_ckpt) ->
      should_stop_early.
    use_orbax: Enables checkpointing backed by Orbax.
  """
  logging.info('Using SPMD sharding for model parallelism.')

  if not eval_input_p:
    return

  checkpoint_dir = os.path.join(job_log_dir, 'checkpoints')
  checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(checkpoint_dir)

  model_p = task_p.model
  device_mesh = py_utils.create_device_mesh(model_p.ici_mesh_shape,
                                            model_p.dcn_mesh_shape)
  global_mesh = maps.Mesh(device_mesh, model_p.mesh_axis_names)
  jax_task = instantiate(task_p)

  use_ema = has_ema(task_p)

  # TODO(bf-jax): Retrieve the seeds from the model definition instead.
  prng_key = jax.random.PRNGKey(1234)
  prng_key, init_key = jax.random.split(prng_key)
  # We do not fold in jax.process_index in contrast to the pmap version and
  # use a single global key instead to rely on pjit to split for different
  # replicas.
  logging.info('root prng_key: %s', prng_key)
  _, eval_key = jax.random.split(prng_key)
  logging.info('eval prng_key: %s', eval_key)

  # TODO(pax-dev): Investigate if we can use model input specs
  # instead of instantiating this input pipeline.
  sample_inputs = instantiate(eval_input_p[0]).get_next_padded()

  (partitioned_train_state, partitioned_specs, train_state_global_shapes, _, _,
   sample_inputs) = _SpmdEvalRunner.get_model_states_and_step_fn(
       jax_task,
       global_mesh,
       init_key,
       sample_inputs,
       checkpoint_dir,
       checkpoint_type,
       checkpoint_step=checkpoint_step,
       use_ema=use_ema,
       use_orbax=use_orbax)
  logging.info('partitioned_train_state: %s',
               jax.tree_map(lambda x: x.shape, partitioned_train_state))

  eval_input_p = [
      trainer_lib.adjust_input_params_for_small_batch(inp, global_mesh)
      for inp in eval_input_p
  ]

  runner = _SpmdEvalRunner(task_p, eval_input_p, jax_task, global_mesh,
                           init_key, partitioned_specs)
  logging.info('Evaluation loop starting...')
  summary_base_dir = os.path.join(job_log_dir, 'summaries')
  summary_eval_dirs = [
      os.path.join(summary_base_dir, f'eval_test_{p.name}')
      for p in eval_input_p
  ]
  last_checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(
      checkpoint_dir)

  create_gda_for_inputs = (
      jax.config.jax_parallel_functions_output_gda and
      checkpoint_type != CheckpointType.CHECKPOINT_PERSISTENCE)
  with contextlib.ExitStack() as exit_stack:
    eval_summary_writers = [
        exit_stack.enter_context(summary_utils.get_summary_writer(d))
        for d in summary_eval_dirs
    ]
    while True:
      with py_utils.timeit() as eval_period:
        eval_metrics_list, eval_scoring_metrics_list, num_eval_steps = (
            runner.run_one_step(partitioned_train_state, eval_summary_writers,
                                eval_key, create_gda_for_inputs))

      eval_metrics = tuning_lib.EvalMetrics(
          input_p=eval_input_p,
          metrics_list=eval_metrics_list,
          scoring_metrics_list=eval_scoring_metrics_list,
          steps_per_sec=sum(num_eval_steps) / eval_period.elapsed)

      # If the last check point evaluated matches max train steps, exit.
      if last_checkpoint_step is not None:
        exceeded_ckpt = last_checkpoint_step + task_p.train.save_interval_steps
        is_last_ckpt = exceeded_ckpt > task_p.train.num_train_steps
        if tuning_lib.should_early_stop(
            early_stopping_fn,
            last_checkpoint_step,
            is_last_ckpt,
            eval_metrics=eval_metrics):
          logging.info(
              'Evaluation is early stopped at checkpoint step %d by the'
              'tuner, while the num_train_steps is %d', last_checkpoint_step,
              task_p.train.num_train_steps)
          break
        if is_last_ckpt:
          break
      new_checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(
          checkpoint_dir)
      while new_checkpoint_step == last_checkpoint_step:
        logging.info('Sleep before checking for new latest checkpoint.')
        time.sleep(60)
        new_checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(
            checkpoint_dir)
      # There must be a new checkpoint here.
      logging.info('Found new checkpoint at step: %d', new_checkpoint_step)
      partitioned_train_state = checkpoints.restore_checkpoint(
          train_state_global_shapes,
          checkpoint_dir,
          global_mesh=runner.global_mesh,
          checkpoint_type=checkpoint_type,
          state_specs=partitioned_specs,
          step=new_checkpoint_step,
          use_orbax=use_orbax)
      if use_ema:
        partitioned_train_state = extract_ema(partitioned_train_state)
      py_utils.sync_global_devices(f'checkpointer:restored:{checkpoint_dir}')
      last_checkpoint_step = new_checkpoint_step


def decode(experiment_config: base_experiment.BaseExperiment,
           job_log_dir: Optional[str],
           maybe_use_persistence_checkpointing: bool,
           restore_checkpoint_dir: Optional[str],
           restore_checkpoint_step: Optional[int],
           continuous_decode: bool,
           run_eval: Optional[bool] = False,
           early_stopping_fn: Optional[trainer_lib.EarlyStoppingFn] = None,
           use_orbax: bool = False) -> None:
  """Runs decoding on the decoder datasets.

  Args:
    experiment_config: an instance of BaseExperiment for the experiment to
      decode.
    job_log_dir: The directory for the job logs.
    maybe_use_persistence_checkpointing: If set, it will try to use
      persistence-based checkpointing if suitable.
    restore_checkpoint_dir: The directory from which to restore checkpoint.
    restore_checkpoint_step: If set, the checkpoint step to restore. If unset,
      try to restore from the latest checkpoint if any.
    continuous_decode: whether to continuously decode on the latest ckpt.
    run_eval: whether to run evaluate() (i.e. to obtain scoring based metrics)
      as well.
    early_stopping_fn: An optional callable object for reporting metrics and
      determining whether to early stop current training. The callable object
      has signature: (metrics, running_mode, ckpt_step, is_final_ckpt) ->
      should_stop_early.
    use_orbax: Enables checkpointing backed by Orbax.
  """
  if continuous_decode and restore_checkpoint_dir:
    raise ValueError('restore_checkpoint_{dir,step} only supported with '
                     'decode once, i.e. it requires continuous_decode=False.')

  restore_checkpoint_dir = restore_checkpoint_dir or os.path.join(
      job_log_dir, 'checkpoints')

  if continuous_decode:
    logging.info('running continuous_decode from %s', restore_checkpoint_dir)
  else:
    logging.info('running decode_once restored from %s', restore_checkpoint_dir)

  if restore_checkpoint_step is None:
    restore_checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(
        restore_checkpoint_dir)
    # TODO(pax-team): Enforce that a checkpoint exists / a checkpoint step was
    # retrieved.

  task_p = experiment_config.task()
  task_p = typing.cast(tasks_lib.SingleTask.HParams, task_p)
  model_p = task_p.model
  decoder_inputs = experiment_config.decoder_datasets()
  eval_inputs = [v for v in experiment_config.datasets() if not v.is_training]
  if not run_eval:
    eval_inputs = []
  if not decoder_inputs and not eval_inputs:
    logging.info('No input datasets defined.')
    return

  for inp in (decoder_inputs + eval_inputs):
    if inp.num_infeed_hosts == 0:
      inp.num_infeed_hosts = jax.process_count()
    inp.infeed_host_index = jax.process_index()

  if model_p.mesh_shape is not None:
    checkpoint_type = checkpoints.retrieve_checkpoint_type(
        maybe_use_persistence_checkpointing, task_p)
    decode_spmd_model(
        task_p,
        decoder_inputs,
        eval_inputs,
        job_log_dir,
        checkpoint_type,
        restore_checkpoint_dir,
        restore_checkpoint_step,
        continuous_decode,
        early_stopping_fn,
        use_orbax=use_orbax)
  else:
    decode_pmap_model(
        task_p,
        decoder_inputs,
        eval_inputs,
        job_log_dir,
        restore_checkpoint_dir,
        restore_checkpoint_step,
        continuous_decode,
        early_stopping_fn,
        use_orbax=use_orbax)


def _get_dir_names(
    input_p: Sequence[base_input.BaseInput.HParams]) -> Sequence[str]:
  """Returns a list of same length for parent dir names for each dataset."""
  return [p.name for p in input_p]


def _get_filename(step: base_layer.JTensorOrPartitionSpec) -> str:
  """Returns a filename for the given step."""
  step_num = py_utils.maybe_unreplicate_for_fully_replicated(step)
  return f'decoder_out_{step_num}_shard_{jax.process_index()}'


def _can_load_decode_outs(basedir: str, pname: str, step: int) -> bool:
  """Returns whether we can load the decoder outputs already."""
  success = np.array([0], dtype=np.int32)
  if jax.process_index() == 0:
    try:
      outputs = io_utils.load_outputs(basedir, pname, step)
      success[0] = len(outputs)
    except Exception:  # pylint: disable=broad-except
      pass
  out = multihost_utils.broadcast_one_to_all(success)
  return out[0] > 0


def _merge_clu_metrics(metrics: Metrics, updated_metrics: Metrics) -> Metrics:
  """Merges existing eval metrics with updated metric data."""
  if metrics:
    if set(metrics.keys()) != set(updated_metrics.keys()):
      raise ValueError('metrics and updated_metrics keys don`t match. '
                       f'metrics keys: {metrics.keys()} '
                       f'updated_metrics keys: {updated_metrics.keys()}')

    for key in metrics:
      metrics[key] = metrics[key].merge(updated_metrics[key])
  else:
    metrics = updated_metrics
  return metrics


def decode_pmap_model(task_p: tasks_lib.SingleTask.HParams,
                      input_p: Sequence[base_input.BaseInput.HParams],
                      eval_input_p: Sequence[base_input.BaseInput.HParams],
                      job_log_dir: Optional[str],
                      restore_checkpoint_dir: str,
                      restore_checkpoint_step: Optional[int],
                      continuous_decode: bool,
                      early_stopping_fn: Optional[
                          trainer_lib.EarlyStoppingFn] = None,
                      use_orbax: bool = False) -> None:
  """Runs the decoding on the entire decoder datasets for a PMAP model.

  Args:
    task_p: Params of the task encapsulating a the data parallel model.
    input_p: List of input params to be decoded.
    eval_input_p: List of input params to be evaluated.
    job_log_dir: Directory for the job logs.
    restore_checkpoint_dir: The directory from which to restore checkpoint.
    restore_checkpoint_step: The checkpoint step to restore. If unset, the
      decoded model will be randomly initialized.
    continuous_decode: whether to continuously decode on the latest ckpt.
    early_stopping_fn: An optional callable object for reporting metrics and
      determining whether to early stop current training. The callable object
      has signature: (metrics, running_mode, ckpt_step, is_final_ckpt) ->
      should_stop_early.
    use_orbax: Enables checkpointing backed by Orbax.
  """
  jax_task = instantiate(task_p)
  use_ema = has_ema(task_p)
  track_metric = bool(task_p.track_decoder_metric)

  # TODO(shafey): Retrieve the seeds from the model definition instead.
  prng_key = jax.random.PRNGKey(1234)
  prng_key, eval_key = jax.random.split(prng_key, 2)
  # _PmapEvalRunner requires drawing a sample input for restoring checkpoints.
  # We assume that either eval_input or decoder_input can be used to retrieve
  # all the model variable shapes.
  # TODO(zhangqiaorjc): If we can no longer assume variable shapes will be the
  # same regardless of which eval_input or decoder_input we use to draw the
  # sample inputs, we need to revisit the design here.
  sample_input_p = input_p[0] if input_p else eval_input_p[0]

  # Either decoder or eval inputs is not empty.
  assert list(input_p) + list(eval_input_p)
  # _PmapEvalRunner requires drawing a sample input for restoring checkpoints.
  # We assume that either eval_input or decoder_input can be used to retrieve
  # all the model variable shapes.
  # TODO(zhangqiaorjc): If we can no longer assume variable shapes will be the
  # same regardless of which eval_input or decoder_input we use to draw the
  # sample inputs, we need to revisit the design here.
  sample_input_p = input_p[0] if input_p else eval_input_p[0]
  # TODO(pax-dev): Investigate if we can use model input specs
  # instead of instantiating this input pipeline.
  inputs_sample = instantiate(sample_input_p).get_next_padded()

  eval_runner = _PmapEvalRunner(task_p, eval_input_p, jax_task, eval_key)
  trainer_lib.write_post_init_model_hparams_file(
      jax_task.model,
      jax_task.model.abstract_init_with_metadata(
          prng_key, inputs_sample, do_eval=True),
      os.path.join(job_log_dir, 'decoder_out'))

  (replicated_model_states, train_state_global_shapes,
   prng_key) = _PmapEvalRunner.get_model_states(
       jax_task,
       prng_key,
       inputs_sample,
       restore_checkpoint_dir,
       checkpoint_step=restore_checkpoint_step,
       use_ema=use_ema,
       track_metric=track_metric)
  prng_key, decode_key = jax.random.split(prng_key)
  prng_seed = jax.random.split(decode_key, num=jax.local_device_count())
  logging.info('decoder prng_seed: %s', prng_seed)

  inputs = [instantiate(p) for p in input_p]
  trainer_lib.check_unique_names(inputs)
  summary_base_dir = os.path.join(job_log_dir, 'summaries')
  summary_decode_dirs = [
      os.path.join(summary_base_dir, f'decode_test_{p.name}') for p in input_p
  ]
  summary_eval_dirs = [
      os.path.join(summary_base_dir, f'eval_test_{p.name}')
      for p in eval_input_p
  ]
  with contextlib.ExitStack() as exit_stack:
    summary_writers = [
        exit_stack.enter_context(summary_utils.get_summary_writer(d))
        for d in summary_decode_dirs
    ]
    eval_summary_writers = [
        exit_stack.enter_context(summary_utils.get_summary_writer(d))
        for d in summary_eval_dirs
    ]
    last_checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(
        restore_checkpoint_dir)

    decode_once_fn = partition_decode_once_pmap_model(jax_task, task_p, inputs,
                                                      input_p, prng_seed,
                                                      job_log_dir)

    while True:
      decode_metrics = decode_once_fn(replicated_model_states, summary_writers)

      with py_utils.timeit() as eval_period:
        eval_metrics_list, eval_scoring_metrics_list, num_eval_steps = (
            eval_runner.run_one_step(replicated_model_states,
                                     eval_summary_writers))

      eval_metrics = tuning_lib.EvalMetrics(
          input_p=eval_input_p,
          metrics_list=eval_metrics_list,
          scoring_metrics_list=eval_scoring_metrics_list,
          steps_per_sec=sum(num_eval_steps) / eval_period.elapsed)

      if not continuous_decode:
        break
      if last_checkpoint_step is not None:
        exceeded_ckpt = last_checkpoint_step + task_p.train.save_interval_steps
        is_last_ckpt = exceeded_ckpt > task_p.train.num_train_steps
        if tuning_lib.should_early_stop(
            early_stopping_fn,
            last_checkpoint_step,
            is_last_ckpt,
            eval_metrics=eval_metrics,
            decode_metrics=decode_metrics):
          logging.info(
              'Decoding is early stopped at checkpoint step %d by the'
              'tuner, while the num_train_steps is %d', last_checkpoint_step,
              task_p.train.num_train_steps)
          break
        if is_last_ckpt:
          break
      # Release replicated_model_states.
      del replicated_model_states
      new_checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(
          restore_checkpoint_dir)
      while new_checkpoint_step == last_checkpoint_step:
        logging.info('Sleep before checking for new latest checkpoint.')
        time.sleep(60)
        new_checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(
            restore_checkpoint_dir)
      logging.info('Found new checkpoint at step: %d', new_checkpoint_step)
      if py_utils.pmap_use_tensorstore():
        model_states = tasks_lib.restore_pmap_from_tensorstore(
            train_state_global_shapes,
            restore_checkpoint_dir,
            step=new_checkpoint_step)
      else:
        model_states = checkpoints.restore_checkpoint(
            train_state_global_shapes,
            restore_checkpoint_dir,
            step=new_checkpoint_step,
            use_orbax=use_orbax)
      if use_ema:
        model_states = extract_ema(model_states)
      elif not track_metric:
        model_states = trim_opt_states(model_states)
      replicated_model_states = trainer_lib.replicate_model_state(model_states)
      last_checkpoint_step = new_checkpoint_step


def partition_decode_once_pmap_model(
    jax_task: tasks_lib.SingleTask, task_p: tasks_lib.SingleTask.HParams,
    inputs: List[base_input.BaseInput],
    input_p: Sequence[base_input.BaseInput.HParams], prng_seed: JTensor,
    job_log_dir: str
) -> Callable[[train_states.TrainState, List[SummaryWriter]],
              tuning_lib.DecodeMetrics]:

  def decode_once_fn(partitioned_train_state, summary_writers):
    with py_utils.timeit() as decode_period:
      (decode_metrics_list, processed_decode_metrics_list,
       decode_seqio_metrics_list, num_decode_steps) = (
           decode_once_pmap_model(jax_task, task_p, inputs, input_p, prng_seed,
                                  job_log_dir, partitioned_train_state,
                                  summary_writers))
    decode_steps_per_sec = sum(num_decode_steps) / decode_period.elapsed
    return tuning_lib.DecodeMetrics(
        input_p=input_p,
        metrics_list=decode_metrics_list,
        processed_metrics_list=processed_decode_metrics_list,
        seqio_metrics_list=decode_seqio_metrics_list,
        steps_per_sec=decode_steps_per_sec)

  return decode_once_fn


def decode_once_pmap_model(
    jax_task: tasks_lib.SingleTask,
    task_p: tasks_lib.SingleTask.HParams,
    inputs: List[base_input.BaseInput],
    input_p: Sequence[base_input.BaseInput.HParams],
    prng_seed: JTensor,
    job_log_dir: str,
    replicated_model_states: train_states.TrainState,
    summary_writers: List[SummaryWriter],
) -> Tuple[List[Optional[Dict[str, float]]],  # decode metrics.
           List[Optional[Dict[str, float]]],  # processed decode metrics.
           List[Optional[Dict[str, float]]],  # decode (seqio) metrics.
           List[int]  # performed decode steps.
          ]:
  """Runs the decoding on the entire decoder datasets for a PMAP model.

  Args:
    jax_task: instantiated model from task_p.
    task_p: Params for the task encapsulating a data parallel model.
    inputs: instantiated inputs.
    input_p: List of input params to be decoded.
    prng_seed: The prng seed used for decoding.
    job_log_dir: Directory for the job logs.
    replicated_model_states: A TrainState object.
    summary_writers: The summary writer objects to log summaries.

  Returns:
    A tuple of (a list of decode metrics,
                a list of processed decode metrics,
                a list of optional decoder (seqio) metrics.
                 list of integers as performed decode steps for each input).
      Items from each list are aligned with each input from input_p.
  """
  if not input_p:
    return [], [], [], []
  work_unit = platform.work_unit()
  model = jax_task.model
  model_p = task_p.model
  metrics_p = task_p.metrics
  if not metrics_p:
    metrics_p = base_metrics.MeanMetrics.HParams()

  step_i = int(
      py_utils.maybe_unreplicate_for_fully_replicated(
          replicated_model_states.step))

  logging.info('step=%d', step_i)

  def decode_step(mdl_states, prng_key, inputs):
    mdl_states = trainer_lib.train_state_for_eval_step(mdl_states)
    (weighted_scalars, per_example_out,
     updated_metrics), updated_vars = trainer_lib.decode_step(
         model, mdl_states, prng_key, inputs, model_p.fprop_dtype)

    weighted_scalars = decode_metrics.aggregate(weighted_scalars)
    aggregated_per_example_out = jax.lax.all_gather(
        per_example_out, axis_name=PMAP_PARALLEL_AXIS_NAME, tiled=True)

    summary_tensors = updated_vars.get(base_layer.SUMMARIES, {})
    summary_tensors = summary_utils.flatten_flax_summaries(summary_tensors)
    aggregated_summaries = summary_utils.aggregate_per_replica_summaries(
        summary_tensors)
    aggregated_summaries = NestedMap(fwd_summary_tensors=aggregated_summaries)

    # We want to aggregate metrics across workers.
    # In pmap we do an all gather of the metric state across workers, and then
    # call reduce() on the metric which by default calls merge across workers.
    aggregated_metrics = {}
    for metric_name, metric in updated_metrics.items():
      aggregated_metrics[metric_name] = jax.lax.all_gather(
          metric, axis_name=PMAP_PARALLEL_AXIS_NAME).reduce()

    return (weighted_scalars, aggregated_per_example_out, aggregated_summaries,
            aggregated_metrics)

  # As an example, suppose the output leaf from trainer_lib.decoder_step()
  # for each core has shape: [per_core_batch_size, decoding_length].
  # In the all_gather we set tiled=True, so the output chunks are all
  # concatenated into the existing batch axis, so we get shape
  # [num_cores x per_core_batch_size, decoding_length].
  # In the pmap call we set out_axes=None to not have to manually unreplicate,
  # so the output of pmap_decode_step() will have the same shape.
  #
  # Example code snippet showing this:
  #   # shape (8, 3, 2)
  #   x = jnp.tile(jnp.arange(8)[:, None, None],[1, 3, 2])
  #   # shape (24, 2)
  #   z = jax.pmap(
  #       lambda y: jax.lax.all_gather(y+1, axis_name='i', tiled=True),
  #       axis_name='i', out_axes=None)(x)
  #
  # We aggregate all outputs from decode_step.
  pmap_decode_step = jax.pmap(
      decode_step,
      axis_name=PMAP_PARALLEL_AXIS_NAME,
      out_axes=(None, None, None, None))

  def decode_step_func(inputs):
    # TODO(pax): shall we eval all sub-models during eval?
    return pmap_decode_step(replicated_model_states, prng_seed, inputs)

  num_steps_per_input = [
      -1 if p.reset_for_eval else p.eval_loop_num_batches for p in input_p
  ]
  basedir = os.path.join(job_log_dir, 'decoder_out')
  dirnames = _get_dir_names(input_p)
  filename = _get_filename(replicated_model_states.step)
  filenames = [os.path.join(basedir, s, filename) for s in dirnames]

  decode_metrics_list = []
  processed_decode_metrics_list = []
  seqio_metrics_list = []
  num_decode_steps = []

  for split, num_split_steps in enumerate(num_steps_per_input):
    if _can_load_decode_outs(job_log_dir, input_p[split].name, step_i):
      logging.info('Decoding on input %s at step %d already done, skipping.',
                   input_p[split].name, step_i)
      decode_metrics_list.append(None)
      processed_decode_metrics_list.append(None)
      seqio_metrics_list.append(None)
      num_decode_steps.append(0)
      continue
    logging.info('Start decoding on input %s', input_p[split].name)
    step_num = 0
    # decode_metrics and process_decode_metrics work on WeightedScalars
    # which are string -> (value, weight) pairs where value and weight
    # scalars. These metrics are configured on the task.
    decode_metrics = instantiate(metrics_p)
    process_decode_metrics = instantiate(metrics_p)

    # metrics and processed_metrics are dictionaries of
    # strings -> clu_metrics.Metric objects. metrics is returned from decode()
    # and processed_metrics is returned from process_decode_out.
    metrics = {}
    processed_metrics = {}
    processed_decodes = []
    all_summary_tensors = collections.defaultdict(list)
    while num_split_steps < 0 or step_num < num_split_steps:
      step_num += 1
      try:
        batch = inputs[split].get_next()
      except (tf.errors.OutOfRangeError, StopIteration):
        inputs[split].reset()
        break
      batch = tf.nest.map_structure(py_utils.reshard, batch)
      (batch_metrics, out, summary_tensors,
       updated_metrics) = decode_step_func(batch)
      for key, tensor in summary_utils.flatten_summary_dict(summary_tensors):
        all_summary_tensors[key].append(tensor)
      # we store the metric directly as it has already been aggregated in
      # side decode_step_fun
      decode_metrics.store(batch_metrics)
      logging.info('Finished decoding input batch %d', step_num)

      # Merge clu.metrics to update for each minibatch.
      metrics = _merge_clu_metrics(metrics, updated_metrics)

      if jax.process_index() == 0:
        process_decode_output = model.process_decode_out(inputs[split], out)
        # The process_decode_out API allows either two or three returns, so we
        # handle that here.
        if len(process_decode_output) == 2:
          (processed_scalars, processed_out) = process_decode_output
          processed_metric_updates = None
        else:
          (processed_scalars, processed_out,
           processed_metric_updates) = process_decode_output
        process_decode_metrics.store(processed_scalars)
        processed_decodes.extend(processed_out)
        if processed_metric_updates:
          processed_metrics = _merge_clu_metrics(processed_metrics,
                                                 processed_metric_updates)

        logging.info('Finished processing decoded input batch %d', step_num)
      work_unit.set_task_status(f'Finished decoding input batch {step_num} '
                                f'on {input_p[split].name}')

    # Now the decode loop of multiple batches on current dataset is done,
    # we start to aggregate copmuted metrics and put them in summary.
    seqio_metric_values = None
    if seqio_input.should_process_outputs(inputs[split]):
      logging.info('Finished processing all %d examples.',
                   len(processed_decodes))
      seqio_metric_values = seqio_input.process_outputs(
          inputs[split],
          processed_decodes,
          summary_writers[split],
          seqio_input.MetricType.PREDICT,
          step_i,
          plain_text_output_fname=f'{filenames[split]}.txt')

    # Convert metrics to Dict[str, clu_values.Value] for summary writing.
    metric_values = metric_utils.compute_metric_values(metrics)
    process_metric_values = metric_utils.compute_metric_values(
        processed_metrics)

    with summary_writers[split].as_default():
      logging.info('Summarizing of decode_metrics.')
      decode_metric_dict = decode_metrics.summarize(step_i, 'decode_metrics')
      logging.info('Summarizing of process_decode_metrics.')
      processed_metric_dict = process_decode_metrics.summarize(
          step_i, 'process_decode_metrics')
      for key, tensor in all_summary_tensors.items():
        summary_type = base_layer.get_summary_type_from_key(key)
        summary_utils.write_summary_tensor(step_i, key, np.array(tensor),
                                           summary_type)
      metric_utils.write_clu_metric_summaries(metric_values, step_i)
      metric_utils.write_clu_metric_summaries(process_metric_values, step_i)

    # Track metric specified by task_p.track_decoder_metric.
    track_metric = task_p.track_decoder_metric
    if track_metric and track_metric in processed_metric_dict:
      (m_value, _) = processed_metric_dict[track_metric]
      tracker_dir_path = os.path.join(basedir, dirnames[split],
                                      track_metric + '_min_tracker')
      maybe_update_min_tracked_metric(m_value, step_i, tracker_dir_path,
                                      track_metric, input_p[split].name,
                                      replicated_model_states)
    elif track_metric:
      logging.info('Cannot track metric %s on input %s.', track_metric,
                   input_p[split].name)

    if (jax.process_index() == 0 and
        not flags.FLAGS.pax_only_aggregate_summaries):
      dir_path = os.path.join(basedir, dirnames[split])
      if not tf.io.gfile.exists(dir_path):
        tf.io.gfile.makedirs(dir_path)
      output_file = filenames[split]
      logging.info('Writing decoder output to %s with %d entries', output_file,
                   len(processed_decodes))
      io_utils.write_key_value_pairs(output_file, processed_decodes)

    decode_metrics_list.append(
        metric_utils.update_float_dict(
            metric_utils.as_float_dict(decode_metric_dict),
            metric_utils.as_float_dict(metric_values)))
    processed_decode_metrics_list.append(
        metric_utils.update_float_dict(
            metric_utils.as_float_dict(processed_metric_dict),
            metric_utils.as_float_dict(process_metric_values)))
    seqio_metrics_list.append(seqio_metric_values)
    num_decode_steps.append(step_num)
  return (decode_metrics_list, processed_decode_metrics_list,
          seqio_metrics_list, num_decode_steps)


def decode_spmd_model(task_p: tasks_lib.SingleTask.HParams,
                      input_p: Sequence[base_input.BaseInput.HParams],
                      eval_input_p: Sequence[base_input.BaseInput.HParams],
                      job_log_dir: Optional[str],
                      checkpoint_type: CheckpointType,
                      restore_checkpoint_dir: str,
                      restore_checkpoint_step: Optional[int],
                      continuous_decode: bool,
                      early_stopping_fn: Optional[
                          trainer_lib.EarlyStoppingFn] = None,
                      use_orbax: bool = False) -> None:
  """Runs the decoding on the entire decoder datasets for SPMD model.

  Args:
    task_p: Params for the task that encapsulates an SPMD model.
    input_p: List of input params to be decoded.
    eval_input_p: List of input params to be evaluated.
    job_log_dir: Directory for the job logs.
    checkpoint_type: Type of model checkpointing method to use.
    restore_checkpoint_dir: The directory from which to restore checkpoint.
    restore_checkpoint_step: The checkpoint step to restore. If unset, the
      decoded model will be randomly initialized.
    continuous_decode: whether to continuously decode on the latest ckpt.
    early_stopping_fn: An optional callable object for reporting metrics and
      determining whether to early stop current training. The callable object
      has signature: (metrics, running_mode, ckpt_step, is_final_ckpt) ->
      should_stop_early.
    use_orbax: Enables checkpointing backed by Orbax.
  """
  # TODO(bf-jax): Retrieve the seeds from the model definition instead.
  prng_key = jax.random.PRNGKey(1234)
  prng_key, init_key, eval_key = jax.random.split(prng_key, 3)

  jax_task = instantiate(task_p)

  model_p = task_p.model
  device_mesh = py_utils.create_device_mesh(model_p.ici_mesh_shape,
                                            model_p.dcn_mesh_shape)
  global_mesh = maps.Mesh(device_mesh, model_p.mesh_axis_names)

  input_p = [
      trainer_lib.adjust_input_params_for_small_batch(inp, global_mesh)
      for inp in input_p
  ]
  eval_input_p = [
      trainer_lib.adjust_input_params_for_small_batch(inp, global_mesh)
      for inp in eval_input_p
  ]

  # Either decoder or eval inputs is not empty.
  assert list(input_p) + list(eval_input_p)
  sample_input_p = input_p[0] if input_p else eval_input_p[0]
  inputs_sample = instantiate(sample_input_p).get_next_padded()
  inputs_shape = tf.nest.map_structure(py_utils.get_global_input_shape_dtype,
                                       inputs_sample)
  inputs = [instantiate(p) for p in input_p]
  trainer_lib.check_unique_names(inputs)

  use_ema = has_ema(task_p)

  with global_mesh:
    use_gda = (
        jax.config.jax_parallel_functions_output_gda and
        checkpoint_type != CheckpointType.CHECKPOINT_PERSISTENCE)

    # _SpmdEvalRunner requires drawing a sample input for restoring checkpoints.
    # We assume that either eval_input or decoder_input can be used to retrieve
    # all the model variable shapes.
    # TODO(zhangqiaorjc): If we can no longer assume variable shapes will be the
    # same regardless of which eval_input or decoder_input we use to draw the
    # sample inputs, we need to revisit the design here.
    (partitioned_train_state, partitioned_specs, train_state_global_shapes,
     decode_step_fn, inputs_partition_spec,
     inputs_sample) = _SpmdEvalRunner.get_model_states_and_step_fn(
         jax_task,
         global_mesh,
         init_key,
         inputs_sample,
         restore_checkpoint_dir,
         checkpoint_type,
         checkpoint_step=restore_checkpoint_step,
         is_decode=True,
         use_ema=use_ema)
    eval_runner = _SpmdEvalRunner(task_p, eval_input_p, jax_task, global_mesh,
                                  init_key, partitioned_specs)
    trainer_lib.write_post_init_model_hparams_file(
        jax_task.model,
        jax_task.model.abstract_init_with_metadata(
            init_key, inputs_sample, do_eval=True),
        os.path.join(job_log_dir, 'decoder_out'))
    summary_base_dir = os.path.join(job_log_dir, 'summaries')
    summary_decode_dirs = [
        os.path.join(summary_base_dir, f'decode_test_{p.name}') for p in input_p
    ]
    summary_eval_dirs = [
        os.path.join(summary_base_dir, f'eval_test_{p.name}')
        for p in eval_input_p
    ]
    partitioned_train_state = trim_opt_states(partitioned_train_state)
    last_checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(
        restore_checkpoint_dir)
    with contextlib.ExitStack() as exit_stack:
      summary_writers = [
          exit_stack.enter_context(summary_utils.get_summary_writer(d))
          for d in summary_decode_dirs
      ]
      eval_summary_writers = [
          exit_stack.enter_context(summary_utils.get_summary_writer(d))
          for d in summary_eval_dirs
      ]
      decode_once_fn = partition_decode_once_spmd_model(
          jax_task, task_p, inputs, input_p, job_log_dir, prng_key, global_mesh,
          decode_step_fn, use_gda, inputs_shape, inputs_partition_spec)
      while True:
        decode_metrics = decode_once_fn(partitioned_train_state,
                                        summary_writers)

        with py_utils.timeit() as eval_period:
          (eval_metrics_list, eval_scoring_metrics_list,
           num_eval_steps) = eval_runner.run_one_step(partitioned_train_state,
                                                      eval_summary_writers,
                                                      eval_key, use_gda)

        eval_metrics = tuning_lib.EvalMetrics(
            input_p=eval_input_p,
            metrics_list=eval_metrics_list,
            scoring_metrics_list=eval_scoring_metrics_list,
            steps_per_sec=sum(num_eval_steps) / eval_period.elapsed)

        if not continuous_decode:
          break
        if last_checkpoint_step is not None:
          exceeded_ckpt = last_checkpoint_step + task_p.train.save_interval_steps
          is_last_ckpt = exceeded_ckpt > task_p.train.num_train_steps
          if tuning_lib.should_early_stop(
              early_stopping_fn,
              last_checkpoint_step,
              is_last_ckpt,
              eval_metrics=eval_metrics,
              decode_metrics=decode_metrics):
            logging.info(
                'Decoding is early stopped at checkpoint step %d by the'
                'tuner, while the num_train_steps is %d', last_checkpoint_step,
                task_p.train.num_train_steps)
            break
          if is_last_ckpt:
            break
        new_checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(
            restore_checkpoint_dir)
        while new_checkpoint_step == last_checkpoint_step:
          logging.info('Sleep before checking for new latest checkpoint.')
          time.sleep(60)
          new_checkpoint_step = checkpoints.retrieve_latest_checkpoint_step(
              restore_checkpoint_dir)
        logging.info('Found new checkpoint at step: %d', new_checkpoint_step)
        partitioned_train_state = checkpoints.restore_checkpoint(
            train_state_global_shapes,
            restore_checkpoint_dir,
            global_mesh=global_mesh,
            checkpoint_type=checkpoint_type,
            state_specs=partitioned_specs,
            use_orbax=use_orbax)
        if use_ema:
          partitioned_train_state = extract_ema(partitioned_train_state)
        else:
          partitioned_train_state = trim_opt_states(partitioned_train_state)
        last_checkpoint_step = new_checkpoint_step


def partition_decode_once_spmd_model(
    jax_task: tasks_lib.SingleTask,
    task_p: tasks_lib.SingleTask.HParams,
    inputs: List[base_input.BaseInput],
    input_p: Sequence[base_input.BaseInput.HParams],
    job_log_dir: str,
    prng_key: JTensor,
    global_mesh: maps.Mesh,
    decode_step_fn: Callable[[NestedJTensor, JTensor, NestedJTensor],
                             Tuple[Tuple[NestedMap, NestedMap], NestedMap]],
    use_gda: bool,
    inputs_shape: pytypes.NestedShapeDtypeStruct,
    inputs_partition_spec: NestedPartitionSpec,
) -> Callable[[train_states.TrainState, List[SummaryWriter]],
              tuning_lib.DecodeMetrics]:

  def decode_once_fn(partitioned_train_state, summary_writers):
    with py_utils.timeit() as decode_period:
      (decode_metrics_list, processed_decode_metrics_list,
       decode_seqio_metrics_list, num_decode_steps) = decode_once_spmd_model(
           jax_task, task_p, inputs, input_p, job_log_dir,
           partitioned_train_state, summary_writers, prng_key, global_mesh,
           decode_step_fn, use_gda, inputs_shape, inputs_partition_spec)
    decode_steps_per_sec = sum(num_decode_steps) / decode_period.elapsed
    return tuning_lib.DecodeMetrics(
        input_p=input_p,
        metrics_list=decode_metrics_list,
        processed_metrics_list=processed_decode_metrics_list,
        seqio_metrics_list=decode_seqio_metrics_list,
        steps_per_sec=decode_steps_per_sec)

  return decode_once_fn


def decode_once_spmd_model(
    jax_task: tasks_lib.SingleTask,
    task_p: tasks_lib.SingleTask.HParams,
    inputs: List[base_input.BaseInput],
    input_p: Sequence[base_input.BaseInput.HParams],
    job_log_dir: str,
    train_state: train_states.TrainState,
    summary_writers: List[SummaryWriter],
    prng_key: JTensor,
    global_mesh: maps.Mesh,
    decode_step_fn: Callable[[NestedJTensor, JTensor, NestedJTensor],
                             Tuple[Tuple[NestedMap, NestedMap], NestedMap]],
    use_gda: bool,
    inputs_shape: pytypes.NestedShapeDtypeStruct,
    inputs_partition_spec: NestedPartitionSpec,
) -> Tuple[List[Optional[Dict[str, float]]],  # decode metrics.
           List[Optional[Dict[str, float]]],  # processed decode metrics.
           List[Optional[Dict[str, float]]],  # decode (seqio) metrics.
           List[int]  # performed decode steps.
          ]:
  """Runs the decoding once on the entire decoder datasets for an SPMD model.

  Args:
    jax_task: instantiated model from task_p.
    task_p: Params for the task that encapsulates an SPMD model.
    inputs: instantiated inputs.
    input_p: List of input params to be decoded.
    job_log_dir: Directory for the job logs.
    train_state: A TrainState object.
    summary_writers: The summary writer objects to log summaries.
    prng_key: The prng key used for decoding.
    global_mesh: the global mesh.
    decode_step_fn: pjit'ed decode function.
    use_gda: bool, whether GDA is used.
    inputs_shape: nested map of shapes of inputs.
    inputs_partition_spec: Partition spec for inputs.

  Returns:
    A tuple of (a list of decode metrics,
                a list of processed decode metrics,
                a list of optional decoder (seqio) metrics.
                 list of integers as performed decode steps for each input).
      Items from each list are aligned with each input from input_p.
  """
  work_unit = platform.work_unit()
  metrics_p = task_p.metrics
  if not metrics_p:
    metrics_p = base_metrics.MeanMetrics.HParams()

  step_i = int(
      py_utils.maybe_unreplicate_for_fully_replicated(train_state.step))
  basedir = os.path.join(job_log_dir, 'decoder_out')
  dirnames = _get_dir_names(input_p)
  filenames = [
      os.path.join(basedir, s, _get_filename(step_i)) for s in dirnames
  ]

  logging.info('partitioned_train_state: %s',
               jax.tree_map(lambda x: x.shape, train_state))
  # We do not fold in jax.process_index in contrast to the pmap version and
  # use a single global key instead to rely on pjit to split for different
  # replicas.
  logging.info('decode prng_key: %s', prng_key)
  spmd_decode_step_fn = functools.partial(
      decode_step_fn, trainer_lib.train_state_for_eval_step(train_state),
      prng_key)

  num_steps_per_input = [
      -1 if p.reset_for_eval else p.eval_loop_num_batches for p in input_p
  ]
  decode_metrics_list = []
  processed_decode_metrics_list = []
  seqio_metrics_list = []
  num_decode_steps = []

  for split, num_split_steps in enumerate(num_steps_per_input):
    if _can_load_decode_outs(job_log_dir, input_p[split].name, step_i):
      logging.info('Decoding on input %s at step %d already done, skipping.',
                   input_p[split].name, step_i)
      decode_metrics_list.append(None)
      processed_decode_metrics_list.append(None)
      seqio_metrics_list.append(None)
      num_decode_steps.append(0)
      continue
    logging.info('Start decoding on input %s', input_p[split].name)
    step_num = 0
    # decode_metrics and process_decode_metrics work on WeightedScalars
    # which are string -> (value, weight) pairs where value and weight
    # scalars. These metrics are configured on the task.
    decode_metrics = instantiate(metrics_p)
    process_decode_metrics = instantiate(metrics_p)

    # metrics and processed_metrics are dictionaries of
    # strings -> clu_metrics.Metric objects. metrics is returned from decode()
    # and processed_metrics is returned from process_decode_out.
    metrics = {}
    processed_metrics = {}
    processed_decodes = []
    all_summary_tensors = collections.defaultdict(list)
    while num_split_steps < 0 or step_num < num_split_steps:
      step_num += 1
      try:
        batch = inputs[split].get_next_padded()
      except (tf.errors.OutOfRangeError, StopIteration):
        inputs[split].reset()
        break
      if use_gda:
        batch = py_utils.create_gda(batch, inputs_shape, global_mesh,
                                    inputs_partition_spec)
      (weighted_scalars, out,
       updated_metrics), updated_vars = spmd_decode_step_fn(batch)

      # Cross host synchronization happens at this point.
      py_utils.sync_global_devices(f'spmd_decode_step_fn{step_num}')
      # Output is fully replicated now, so it's ok to unreplicate it by
      # retrieving from device 0 only.
      out = py_utils.maybe_unreplicate_for_fully_replicated(out)
      weighted_scalars = py_utils.maybe_unreplicate_for_fully_replicated(
          weighted_scalars)

      # Because outputs of the decode step in pjit are annotated to be on the
      # GDA, they are already fully replicated across shards and we can just
      # unreplicate.
      # This also means we don't need to call an all_gather and a reduce()
      # on each clu.metric like we do in pmap mode.
      updated_metrics = py_utils.maybe_unreplicate_for_fully_replicated(
          updated_metrics)

      # Merge clu.metrics to update for each minibatch.
      metrics = _merge_clu_metrics(metrics, updated_metrics)

      summary_tensors = updated_vars.get(base_layer.SUMMARIES, {})
      summary_tensors = summary_utils.flatten_flax_summaries(summary_tensors)
      del updated_vars  # release GDA memory allocations

      summary_tensors = py_utils.maybe_unreplicate_for_fully_replicated(
          summary_tensors)
      summary_tensors = NestedMap(fwd_summary_tensors=summary_tensors)
      for key, tensor in summary_utils.flatten_summary_dict(summary_tensors):
        all_summary_tensors[key].append(tensor)

      logging.info('Finished decoding input batch %d', step_num)
      if jax.process_index() != 0:
        continue
      weighted_scalars = jax.tree_map(np.array, weighted_scalars)
      decode_metrics.store(weighted_scalars)

      process_decode_output = jax_task.model.process_decode_out(
          inputs[split], out)
      # The process_decode_out API allows either two or three returns, so we
      # handle that here.
      if len(process_decode_output) == 2:
        process_weighted_scalars, processed = process_decode_output
        processed_metric_updates = None
      else:
        (process_weighted_scalars, processed,
         processed_metric_updates) = process_decode_output

      process_decode_metrics.store(process_weighted_scalars)
      processed_decodes.extend(processed)
      if processed_metric_updates:
        processed_metrics = _merge_clu_metrics(processed_metrics,
                                               processed_metric_updates)

      logging.info('Finished processing decoded input batch %d', step_num)

    # Now the decode loop of multiple batches on current dataset is done,
    # we start to aggregate copmuted metrics and put them in summary.
    seqio_metric_values = None
    if seqio_input.should_process_outputs(inputs[split]):
      logging.info('Finished processing all %d examples.',
                   len(processed_decodes))
      seqio_metric_values = seqio_input.process_outputs(
          inputs[split],
          processed_decodes,
          summary_writers[split],
          seqio_input.MetricType.PREDICT,
          step_i,
          plain_text_output_fname=f'{filenames[split]}.txt')

    # Convert metrics to Dict[str, clu_values.Value] for summary writing.
    metric_values = metric_utils.compute_metric_values(metrics)
    process_metric_values = metric_utils.compute_metric_values(
        processed_metrics)

    with summary_writers[split].as_default():
      logging.info('Summarizing of decode_metrics.')
      decode_metric_dict = decode_metrics.summarize(step_i, 'decode_metrics')
      logging.info('Summarizing of process_decode_metrics.')
      processed_metric_dict = process_decode_metrics.summarize(
          step_i, 'process_decode_metrics')
      for key, tensor in all_summary_tensors.items():
        summary_type = base_layer.get_summary_type_from_key(key)
        summary_utils.write_summary_tensor(step_i, key, np.array(tensor),
                                           summary_type)
      metric_utils.write_clu_metric_summaries(metric_values, step_i)
      metric_utils.write_clu_metric_summaries(process_metric_values, step_i)

    # Track metric specified by task_p.track_decoder_metric.
    track_metric = task_p.track_decoder_metric
    if track_metric and track_metric in processed_metric_dict:
      logging.warn('Decoder metric tracking is not implemented yet for pjit '
                   'models. Ignoring metric tracking.')
    elif track_metric:
      logging.info('Cannot track metric %s on input %s.', track_metric,
                   input_p[split].name)

    if jax.process_index() == 0:
      dir_path = os.path.join(basedir, dirnames[split])
      if not tf.io.gfile.exists(dir_path):
        tf.io.gfile.makedirs(dir_path)
      output_file = filenames[split]
      logging.info('Writing decoder output to %s with %d entries', output_file,
                   len(processed_decodes))
      io_utils.write_key_value_pairs(output_file, processed_decodes)

    work_unit.set_task_status(f'Finished processing decoded input batch for '
                              f'{input_p[split].name}')

    decode_metrics_list.append(
        metric_utils.update_float_dict(
            metric_utils.as_float_dict(decode_metric_dict),
            metric_utils.as_float_dict(metric_values)))
    processed_decode_metrics_list.append(
        metric_utils.update_float_dict(
            metric_utils.as_float_dict(processed_metric_dict),
            metric_utils.as_float_dict(process_metric_values)))
    seqio_metrics_list.append(seqio_metric_values)
    num_decode_steps.append(step_num)
  return (decode_metrics_list, processed_decode_metrics_list,
          seqio_metrics_list, num_decode_steps)


def maybe_update_min_tracked_metric(
    m_value: float,
    step: int,
    tracker_dir_path: str,
    track_metric: str,
    data_partition_name: str,
    replicated_model_states: train_states.TrainState,
    use_orbax: bool = False) -> None:
  """Update tracked metric if new value (m_value) is lower that the stored one.

  Also updates the status file maintained by the tracker and writes
  new checkpoint assets in the same tracker directory.

  Args:
    m_value: new metric value.
    step: current training step.
    tracker_dir_path: directory where the tracker should store the status file
      and also write and garbage collect checkpoint assets.
    track_metric: name of metric being tracked, e.g. 'wer'.
    data_partition_name: data partition on which the value of the metric is
      being tracked.
    replicated_model_states: replicated model states used to save the best
      checkpoint.
    use_orbax: Enables checkpointing backed by Orbax.
  """

  if jax.process_index() == 0:
    if not tf.io.gfile.exists(tracker_dir_path):
      tf.io.gfile.makedirs(tracker_dir_path)
    tracker = trk_utils.MetricTracker(
        dir_name=tracker_dir_path,
        metric_name=track_metric,
        metric_partition=data_partition_name,
        initial_metric_value=sys.float_info.max)
    if m_value < tracker.metric_value:
      logging.info('Updating tracked wer value and checkpoint.')
      tracker.update(value=m_value, global_step=step)
      # Also save checkpoint; we just need to save the first model replica.
      # WARNING: the checkpoint saved here will not contain optimizer state
      # if it is written by a separate decoding job; if decoding is done
      # interleaved with training as part of the trainer then it will
      # contain them.
      # Decoding with this checkpoint may thus produce different results
      # than those obtained during training if the model state cannot be
      # fully recovered due to the missing optimizer state, e.g. when using
      # EMA during training and separate decoding jobs.
      # TODO(ciprianchelba): specify the checkpoint format and/or async
      # checkpointing.
      unreplicated_model_states = jax.tree_map(lambda x: x[0],
                                               replicated_model_states)
      checkpoints.save_checkpoint(
          unreplicated_model_states, tracker_dir_path, use_orbax=use_orbax)


def infer_and_write(experiment_config: base_experiment.BaseExperiment,
                    job_log_dir: Optional[str],
                    use_orbax: bool = False) -> None:
  """Generates output from a model and writes it out.

  Args:
    experiment_config: an instance of BaseExperiment for the experiment with
      output generators configured.
    job_log_dir: The base directory for writing the outputs.
    use_orbax: Enables checkpointing backed by Orbax.
  """
  task_p = experiment_config.task()
  task_p = typing.cast(tasks_lib.SingleTask.HParams, task_p)
  model_p = task_p.model
  inputs_p = experiment_config.decoder_datasets()

  for inp in inputs_p:
    if inp.num_infeed_hosts == 0:
      inp.num_infeed_hosts = jax.process_count()
    inp.infeed_host_index = jax.process_index()

  if model_p.mesh_shape is not None:
    # TODO(b/238416854): add support for SPMD models
    raise NotImplementedError('SPMD infer_and_write not implemented yet')
  else:
    infer_and_write_pmap(task_p, inputs_p, job_log_dir, use_orbax=use_orbax)


def infer_and_write_pmap(task_p: tasks_lib.SingleTask.HParams,
                         inputs_p: Sequence[base_input.BaseInput.HParams],
                         job_log_dir: str,
                         use_orbax: bool = False) -> None:
  """Runs the infer_and_write for each of the inputs given task in pmap."""
  task = instantiate(task_p)
  track_metric = bool(task_p.track_decoder_metric)

  prng_key = jax.random.PRNGKey(0)
  infer_writer_p = task_p.infer_writer

  if not inputs_p:
    return

  # TODO(pax-dev): Investigate if we can use model input specs
  # instead of instantiating this input pipeline or re-using one of the
  # input pipelines below.
  inputs_sample = instantiate(inputs_p[0]).get_next_padded()

  (replicated_model_states, _, prng_key) = _PmapEvalRunner.get_model_states(
      task,
      prng_key,
      inputs_sample,
      infer_writer_p.restore_checkpoint_dir,
      infer_writer_p.restore_checkpoint_step,
      has_ema(task_p),
      track_metric,
      use_orbax=use_orbax)

  @functools.partial(jax.pmap, axis_name=PMAP_PARALLEL_AXIS_NAME, out_axes=None)
  def infer_pmap_step(mdl_states, prng_seeds, input_batch):
    outputs = task.inference_runner.infer(mdl_states, prng_seeds, input_batch)
    # tiled=True folds in first axis into second axis [2,8,5] -> [2*8,5]
    replicated_outputs = jax.lax.all_gather(
        outputs, axis_name=PMAP_PARALLEL_AXIS_NAME, tiled=True)

    return replicated_outputs

  # Instantiate inputs to infer on
  inputs = [instantiate(p) for p in inputs_p]
  trainer_lib.check_unique_names(inputs)
  num_steps = [
      -1 if p.reset_for_eval else p.eval_loop_num_batches for p in inputs_p
  ]

  for input_p, input_gen, num_steps in zip(inputs_p, inputs, num_steps):
    logging.info('Starting output generation on input "%s"', input_p.name)

    # Feed each (device, input) pair a unique seed
    prng_key, output_seed = jax.random.split(prng_key)
    output_seeds = jax.random.split(output_seed, jax.local_device_count())

    if num_steps > 0:
      logging.info('total number of steps: %d', num_steps)

    # Only write from one process
    dirname = os.path.join(job_log_dir, 'output', input_p.name)
    # fq_filename = os.path.join(dirname, 'output.tfrecord')
    fq_filename = os.path.join(dirname, 'output')
    if jax.process_index() == 0:
      # Create output dirs if DNE
      if not tf.io.gfile.exists(dirname):
        tf.io.gfile.makedirs(dirname)

      # Write example schema, metadata, and serialized example protos
      logging.info('writing output to %s', fq_filename)
      features_dict = tfds.features.FeaturesDict(
          task.inference_runner.output_schema)
      features_dict.save_config(dirname)
      tfds.core.MetadataDict(
          restore_checkpoint_dir=infer_writer_p.restore_checkpoint_dir,
          restore_checkpoint_step=infer_writer_p.restore_checkpoint_step,
          input_name=input_p.name,
          model_name=task_p.model.name,
      ).save_metadata(dirname)

      writer = io_utils.ShardedParallelWriter(
          fq_filename,
          infer_writer_p.output_num_shards,
          output_format=infer_writer_p.output_format)

    step = 0
    while num_steps < 0 or step < num_steps:
      step += 1
      logging.info('processing input batch %d', step)
      try:
        batch = input_gen.get_next()
      except (tf.errors.OutOfRangeError, StopIteration):
        input_gen.reset()
        break

      pmap_batch = jax.tree_map(py_utils.reshard, batch)
      outputs = infer_pmap_step(replicated_model_states, output_seeds,
                                pmap_batch)
      # Get first device's output since it's been replicated by all-gather
      outputs = py_utils.maybe_unreplicate_for_fully_replicated(outputs)
      outputs_cpu = jax.tree_map(np.asarray, outputs)

      if jax.process_index() == 0:
        serialized_outputs = task.inference_runner.serialize_outputs(
            outputs_cpu)
        # fire-and-forget writing
        writer.write(serialized_outputs)

    if jax.process_index() == 0:
      writer.close()
