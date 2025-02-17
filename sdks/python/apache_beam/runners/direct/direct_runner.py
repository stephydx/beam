#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""DirectRunner, executing on the local machine.

The DirectRunner is a runner implementation that executes the entire
graph of transformations belonging to a pipeline on the local machine.
"""

from __future__ import absolute_import

import itertools
import logging
import time
import typing

from google.protobuf import wrappers_pb2

import apache_beam as beam
from apache_beam import coders
from apache_beam import typehints
from apache_beam.internal.util import ArgumentPlaceholder
from apache_beam.options.pipeline_options import DirectOptions
from apache_beam.options.pipeline_options import StandardOptions
from apache_beam.options.value_provider import RuntimeValueProvider
from apache_beam.pvalue import PCollection
from apache_beam.runners.direct.bundle_factory import BundleFactory
from apache_beam.runners.direct.clock import RealClock
from apache_beam.runners.direct.clock import TestClock
from apache_beam.runners.runner import PipelineResult
from apache_beam.runners.runner import PipelineRunner
from apache_beam.runners.runner import PipelineState
from apache_beam.transforms.core import CombinePerKey
from apache_beam.transforms.core import CombineValuesDoFn
from apache_beam.transforms.core import DoFn
from apache_beam.transforms.core import ParDo
from apache_beam.transforms.core import _GroupAlsoByWindow
from apache_beam.transforms.core import _GroupAlsoByWindowDoFn
from apache_beam.transforms.core import _GroupByKeyOnly
from apache_beam.transforms.ptransform import PTransform

# Note that the BundleBasedDirectRunner and SwitchingDirectRunner names are
# experimental and have no backwards compatibility guarantees.
__all__ = ['BundleBasedDirectRunner',
           'DirectRunner',
           'SwitchingDirectRunner']


_LOGGER = logging.getLogger(__name__)


class SwitchingDirectRunner(PipelineRunner):
  """Executes a single pipeline on the local machine.

  This implementation switches between using the FnApiRunner (which has
  high throughput for batch jobs) and using the BundleBasedDirectRunner,
  which supports streaming execution and certain primitives not yet
  implemented in the FnApiRunner.
  """

  def is_fnapi_compatible(self):
    return BundleBasedDirectRunner.is_fnapi_compatible()

  def run_pipeline(self, pipeline, options):

    from apache_beam.pipeline import PipelineVisitor
    from apache_beam.runners.dataflow.native_io.iobase import NativeSource
    from apache_beam.runners.dataflow.native_io.iobase import _NativeWrite
    from apache_beam.testing.test_stream import _TestStream

    class _FnApiRunnerSupportVisitor(PipelineVisitor):
      """Visitor determining if a Pipeline can be run on the FnApiRunner."""

      def accept(self, pipeline):
        self.supported_by_fnapi_runner = True
        pipeline.visit(self)
        return self.supported_by_fnapi_runner

      def visit_transform(self, applied_ptransform):
        transform = applied_ptransform.transform
        # The FnApiRunner does not support streaming execution.
        if isinstance(transform, _TestStream):
          self.supported_by_fnapi_runner = False
        # The FnApiRunner does not support reads from NativeSources.
        if (isinstance(transform, beam.io.Read) and
            isinstance(transform.source, NativeSource)):
          self.supported_by_fnapi_runner = False
        # The FnApiRunner does not support the use of _NativeWrites.
        if isinstance(transform, _NativeWrite):
          self.supported_by_fnapi_runner = False
        if isinstance(transform, beam.ParDo):
          dofn = transform.dofn
          # The FnApiRunner does not support execution of CombineFns with
          # deferred side inputs.
          if isinstance(dofn, CombineValuesDoFn):
            args, kwargs = transform.raw_side_inputs
            args_to_check = itertools.chain(args,
                                            kwargs.values())
            if any(isinstance(arg, ArgumentPlaceholder)
                   for arg in args_to_check):
              self.supported_by_fnapi_runner = False

    # Check whether all transforms used in the pipeline are supported by the
    # FnApiRunner, and the pipeline was not meant to be run as streaming.
    use_fnapi_runner = (
        _FnApiRunnerSupportVisitor().accept(pipeline))

    # Also ensure grpc is available.
    try:
      # pylint: disable=unused-import
      import grpc
    except ImportError:
      use_fnapi_runner = False

    if use_fnapi_runner:
      from apache_beam.runners.portability.fn_api_runner import FnApiRunner
      runner = FnApiRunner()
    else:
      runner = BundleBasedDirectRunner()

    return runner.run_pipeline(pipeline, options)


# Type variables.
K = typing.TypeVar('K')
V = typing.TypeVar('V')


@typehints.with_input_types(typing.Tuple[K, V])
@typehints.with_output_types(typing.Tuple[K, typing.Iterable[V]])
class _StreamingGroupByKeyOnly(_GroupByKeyOnly):
  """Streaming GroupByKeyOnly placeholder for overriding in DirectRunner."""
  urn = "direct_runner:streaming_gbko:v0.1"

  # These are needed due to apply overloads.
  def to_runner_api_parameter(self, unused_context):
    return _StreamingGroupByKeyOnly.urn, None

  @PTransform.register_urn(urn, None)
  def from_runner_api_parameter(unused_payload, unused_context):
    return _StreamingGroupByKeyOnly()


@typehints.with_input_types(typing.Tuple[K, typing.Iterable[V]])
@typehints.with_output_types(typing.Tuple[K, typing.Iterable[V]])
class _StreamingGroupAlsoByWindow(_GroupAlsoByWindow):
  """Streaming GroupAlsoByWindow placeholder for overriding in DirectRunner."""
  urn = "direct_runner:streaming_gabw:v0.1"

  # These are needed due to apply overloads.
  def to_runner_api_parameter(self, context):
    return (
        _StreamingGroupAlsoByWindow.urn,
        wrappers_pb2.BytesValue(value=context.windowing_strategies.get_id(
            self.windowing)))

  @PTransform.register_urn(urn, wrappers_pb2.BytesValue)
  def from_runner_api_parameter(payload, context):
    return _StreamingGroupAlsoByWindow(
        context.windowing_strategies.get_by_id(payload.value))


def _get_transform_overrides(pipeline_options):
  # A list of PTransformOverride objects to be applied before running a pipeline
  # using DirectRunner.
  # Currently this only works for overrides where the input and output types do
  # not change.
  # For internal use only; no backwards-compatibility guarantees.

  # Importing following locally to avoid a circular dependency.
  from apache_beam.pipeline import PTransformOverride
  from apache_beam.runners.direct.helper_transforms import LiftedCombinePerKey
  from apache_beam.runners.direct.sdf_direct_runner import ProcessKeyedElementsViaKeyedWorkItemsOverride
  from apache_beam.runners.direct.sdf_direct_runner import SplittableParDoOverride

  class CombinePerKeyOverride(PTransformOverride):
    def matches(self, applied_ptransform):
      if isinstance(applied_ptransform.transform, CombinePerKey):
        return applied_ptransform.inputs[0].windowing.is_default()

    def get_replacement_transform(self, transform):
      # TODO: Move imports to top. Pipeline <-> Runner dependency cause problems
      # with resolving imports when they are at top.
      # pylint: disable=wrong-import-position
      try:
        return LiftedCombinePerKey(transform.fn, transform.args,
                                   transform.kwargs)
      except NotImplementedError:
        return transform

  class StreamingGroupByKeyOverride(PTransformOverride):
    def matches(self, applied_ptransform):
      # Note: we match the exact class, since we replace it with a subclass.
      return applied_ptransform.transform.__class__ == _GroupByKeyOnly

    def get_replacement_transform(self, transform):
      # Use specialized streaming implementation.
      transform = _StreamingGroupByKeyOnly()
      return transform

  class StreamingGroupAlsoByWindowOverride(PTransformOverride):
    def matches(self, applied_ptransform):
      # Note: we match the exact class, since we replace it with a subclass.
      transform = applied_ptransform.transform
      return (isinstance(applied_ptransform.transform, ParDo) and
              isinstance(transform.dofn, _GroupAlsoByWindowDoFn) and
              transform.__class__ != _StreamingGroupAlsoByWindow)

    def get_replacement_transform(self, transform):
      # Use specialized streaming implementation.
      transform = _StreamingGroupAlsoByWindow(transform.dofn.windowing)
      return transform

  overrides = [SplittableParDoOverride(),
               ProcessKeyedElementsViaKeyedWorkItemsOverride(),
               CombinePerKeyOverride()]

  # Add streaming overrides, if necessary.
  if pipeline_options.view_as(StandardOptions).streaming:
    overrides.append(StreamingGroupByKeyOverride())
    overrides.append(StreamingGroupAlsoByWindowOverride())

  # Add PubSub overrides, if PubSub is available.
  try:
    from apache_beam.io.gcp import pubsub as unused_pubsub
    overrides += _get_pubsub_transform_overrides(pipeline_options)
  except ImportError:
    pass

  return overrides


class _DirectReadFromPubSub(PTransform):
  def __init__(self, source):
    self._source = source

  def _infer_output_coder(self, unused_input_type=None,
                          unused_input_coder=None):
    return coders.BytesCoder()

  def get_windowing(self, inputs):
    return beam.Windowing(beam.window.GlobalWindows())

  def expand(self, pvalue):
    # This is handled as a native transform.
    return PCollection(self.pipeline, is_bounded=self._source.is_bounded())


class _DirectWriteToPubSubFn(DoFn):
  BUFFER_SIZE_ELEMENTS = 100
  FLUSH_TIMEOUT_SECS = BUFFER_SIZE_ELEMENTS * 0.5

  def __init__(self, sink):
    self.project = sink.project
    self.short_topic_name = sink.topic_name
    self.id_label = sink.id_label
    self.timestamp_attribute = sink.timestamp_attribute
    self.with_attributes = sink.with_attributes

    # TODO(BEAM-4275): Add support for id_label and timestamp_attribute.
    if sink.id_label:
      raise NotImplementedError('DirectRunner: id_label is not supported for '
                                'PubSub writes')
    if sink.timestamp_attribute:
      raise NotImplementedError('DirectRunner: timestamp_attribute is not '
                                'supported for PubSub writes')

  def start_bundle(self):
    self._buffer = []

  def process(self, elem):
    self._buffer.append(elem)
    if len(self._buffer) >= self.BUFFER_SIZE_ELEMENTS:
      self._flush()

  def finish_bundle(self):
    self._flush()

  def _flush(self):
    from google.cloud import pubsub
    pub_client = pubsub.PublisherClient()
    topic = pub_client.topic_path(self.project, self.short_topic_name)

    if self.with_attributes:
      futures = [pub_client.publish(topic, elem.data, **elem.attributes)
                 for elem in self._buffer]
    else:
      futures = [pub_client.publish(topic, elem)
                 for elem in self._buffer]

    timer_start = time.time()
    for future in futures:
      remaining = self.FLUSH_TIMEOUT_SECS - (time.time() - timer_start)
      future.result(remaining)
    self._buffer = []


def _get_pubsub_transform_overrides(pipeline_options):
  from apache_beam.io.gcp import pubsub as beam_pubsub
  from apache_beam.pipeline import PTransformOverride

  class ReadFromPubSubOverride(PTransformOverride):
    def matches(self, applied_ptransform):
      return isinstance(applied_ptransform.transform,
                        beam_pubsub.ReadFromPubSub)

    def get_replacement_transform(self, transform):
      if not pipeline_options.view_as(StandardOptions).streaming:
        raise Exception('PubSub I/O is only available in streaming mode '
                        '(use the --streaming flag).')
      return _DirectReadFromPubSub(transform._source)

  class WriteToPubSubOverride(PTransformOverride):
    def matches(self, applied_ptransform):
      return isinstance(
          applied_ptransform.transform,
          (beam_pubsub.WriteToPubSub, beam_pubsub._WriteStringsToPubSub))

    def get_replacement_transform(self, transform):
      if not pipeline_options.view_as(StandardOptions).streaming:
        raise Exception('PubSub I/O is only available in streaming mode '
                        '(use the --streaming flag).')
      return beam.ParDo(_DirectWriteToPubSubFn(transform._sink))

  return [ReadFromPubSubOverride(), WriteToPubSubOverride()]


class BundleBasedDirectRunner(PipelineRunner):
  """Executes a single pipeline on the local machine."""

  @staticmethod
  def is_fnapi_compatible():
    return False

  def run_pipeline(self, pipeline, options):
    """Execute the entire pipeline and returns an DirectPipelineResult."""

    # TODO: Move imports to top. Pipeline <-> Runner dependency cause problems
    # with resolving imports when they are at top.
    # pylint: disable=wrong-import-position
    from apache_beam.pipeline import PipelineVisitor
    from apache_beam.runners.direct.consumer_tracking_pipeline_visitor import \
      ConsumerTrackingPipelineVisitor
    from apache_beam.runners.direct.evaluation_context import EvaluationContext
    from apache_beam.runners.direct.executor import Executor
    from apache_beam.runners.direct.transform_evaluator import \
      TransformEvaluatorRegistry
    from apache_beam.testing.test_stream import _TestStream

    # Performing configured PTransform overrides.
    pipeline.replace_all(_get_transform_overrides(options))

    # If the TestStream I/O is used, use a mock test clock.
    class _TestStreamUsageVisitor(PipelineVisitor):
      """Visitor determining whether a Pipeline uses a TestStream."""

      def __init__(self):
        self.uses_test_stream = False

      def visit_transform(self, applied_ptransform):
        if isinstance(applied_ptransform.transform, _TestStream):
          self.uses_test_stream = True

    visitor = _TestStreamUsageVisitor()
    pipeline.visit(visitor)
    clock = TestClock() if visitor.uses_test_stream else RealClock()

    _LOGGER.info('Running pipeline with DirectRunner.')
    self.consumer_tracking_visitor = ConsumerTrackingPipelineVisitor()
    pipeline.visit(self.consumer_tracking_visitor)

    evaluation_context = EvaluationContext(
        options,
        BundleFactory(stacked=options.view_as(DirectOptions)
                      .direct_runner_use_stacked_bundle),
        self.consumer_tracking_visitor.root_transforms,
        self.consumer_tracking_visitor.value_to_consumers,
        self.consumer_tracking_visitor.step_names,
        self.consumer_tracking_visitor.views,
        clock)

    executor = Executor(self.consumer_tracking_visitor.value_to_consumers,
                        TransformEvaluatorRegistry(evaluation_context),
                        evaluation_context)
    # DirectRunner does not support injecting
    # PipelineOptions values at runtime
    RuntimeValueProvider.set_runtime_options({})
    # Start the executor. This is a non-blocking call, it will start the
    # execution in background threads and return.
    executor.start(self.consumer_tracking_visitor.root_transforms)
    result = DirectPipelineResult(executor, evaluation_context)

    return result


# Use the SwitchingDirectRunner as the default.
DirectRunner = SwitchingDirectRunner


class DirectPipelineResult(PipelineResult):
  """A DirectPipelineResult provides access to info about a pipeline."""

  def __init__(self, executor, evaluation_context):
    super(DirectPipelineResult, self).__init__(PipelineState.RUNNING)
    self._executor = executor
    self._evaluation_context = evaluation_context

  def __del__(self):
    if self._state == PipelineState.RUNNING:
      _LOGGER.warning(
          'The DirectPipelineResult is being garbage-collected while the '
          'DirectRunner is still running the corresponding pipeline. This may '
          'lead to incomplete execution of the pipeline if the main thread '
          'exits before pipeline completion. Consider using '
          'result.wait_until_finish() to wait for completion of pipeline '
          'execution.')

  def wait_until_finish(self, duration=None):
    if not PipelineState.is_terminal(self.state):
      if duration:
        raise NotImplementedError(
            'DirectRunner does not support duration argument.')
      try:
        self._executor.await_completion()
        self._state = PipelineState.DONE
      except:  # pylint: disable=broad-except
        self._state = PipelineState.FAILED
        raise
    return self._state

  def aggregated_values(self, aggregator_or_name):
    return self._evaluation_context.get_aggregator_values(aggregator_or_name)

  def metrics(self):
    return self._evaluation_context.metrics()

  def cancel(self):
    """Shuts down pipeline workers.

    For testing use only. Does not properly wait for pipeline workers to shut
    down.
    """
    self._state = PipelineState.CANCELLING
    self._executor.shutdown()
    self._state = PipelineState.CANCELLED
