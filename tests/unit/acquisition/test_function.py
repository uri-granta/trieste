# Copyright 2020 The Trieste Contributors
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
from __future__ import annotations

import itertools
import math
import unittest.mock
from collections.abc import Mapping
from typing import Callable, Optional, Union
from unittest.mock import MagicMock

import gpflow
import numpy.testing as npt
import pytest
import tensorflow as tf
import tensorflow_probability as tfp

from tests.util.acquisition.sampler import PseudoBatchReparametrizationSampler
from tests.util.misc import (
    TF_DEBUGGING_ERROR_TYPES,
    ShapeLike,
    empty_dataset,
    mk_dataset,
    quadratic,
    raise_exc,
    random_seed,
    various_shapes,
)
from tests.util.models.gpflow.models import GaussianProcess, QuadraticMeanAndRBFKernel, rbf
from tests.util.models.gpflux.models import trieste_deep_gaussian_process
from trieste.acquisition.function import (
    GIBBON,
    AcquisitionFunction,
    AcquisitionFunctionBuilder,
    AugmentedExpectedImprovement,
    BatchMonteCarloExpectedHypervolumeImprovement,
    BatchMonteCarloExpectedImprovement,
    ExpectedConstrainedHypervolumeImprovement,
    ExpectedConstrainedImprovement,
    ExpectedHypervolumeImprovement,
    ExpectedImprovement,
    LocalPenalizationAcquisitionFunction,
    MinValueEntropySearch,
    NegativeLowerConfidenceBound,
    NegativePredictiveMean,
    PenalizationFunction,
    PredictiveVariance,
    ProbabilityOfFeasibility,
    SingleModelAcquisitionBuilder,
    SingleModelGreedyAcquisitionBuilder,
    UpdatablePenalizationFunction,
    augmented_expected_improvement,
    batch_ehvi,
    expected_hv_improvement,
    expected_improvement,
    gibbon_quality_term,
    gibbon_repulsion_term,
    hard_local_penalizer,
    lower_confidence_bound,
    min_value_entropy_search,
    predictive_variance,
    probability_of_feasibility,
    soft_local_penalizer,
)
from trieste.acquisition.multi_objective.pareto import Pareto, get_reference_point
from trieste.acquisition.multi_objective.partition import (
    ExactPartition2dNonDominated,
    prepare_default_non_dominated_partition_bounds,
)
from trieste.data import Dataset
from trieste.models import ProbabilisticModel
from trieste.objectives import BRANIN_MINIMUM, branin
from trieste.space import Box
from trieste.types import TensorType
from trieste.utils import DEFAULTS


class _ArbitrarySingleBuilder(SingleModelAcquisitionBuilder):
    def prepare_acquisition_function(
        self,
        model: ProbabilisticModel,
        dataset: Optional[Dataset] = None,
    ) -> AcquisitionFunction:
        return raise_exc


class _ArbitraryGreedySingleBuilder(SingleModelGreedyAcquisitionBuilder):
    def prepare_acquisition_function(
        self,
        model: ProbabilisticModel,
        dataset: Optional[Dataset] = None,
        pending_points: Optional[TensorType] = None,
    ) -> AcquisitionFunction:
        return raise_exc


def test_single_model_acquisition_builder_raises_immediately_for_wrong_key() -> None:
    builder = _ArbitrarySingleBuilder().using("foo")

    with pytest.raises(KeyError):
        builder.prepare_acquisition_function(
            {"bar": QuadraticMeanAndRBFKernel()}, datasets={"bar": empty_dataset([1], [1])}
        )


def test_single_model_acquisition_builder_repr_includes_class_name() -> None:
    builder = _ArbitrarySingleBuilder()
    assert type(builder).__name__ in repr(builder)


def test_single_model_acquisition_builder_using_passes_on_correct_dataset_and_model() -> None:
    class Builder(SingleModelAcquisitionBuilder):
        def prepare_acquisition_function(
            self,
            model: ProbabilisticModel,
            dataset: Optional[Dataset] = None,
        ) -> AcquisitionFunction:
            assert dataset is data["foo"]
            assert model is models["foo"]
            return raise_exc

    data = {"foo": empty_dataset([1], [1]), "bar": empty_dataset([1], [1])}
    models = {"foo": QuadraticMeanAndRBFKernel(), "bar": QuadraticMeanAndRBFKernel()}
    Builder().using("foo").prepare_acquisition_function(models, datasets=data)


def test_single_model_greedy_acquisition_builder_raises_immediately_for_wrong_key() -> None:
    builder = _ArbitraryGreedySingleBuilder().using("foo")

    with pytest.raises(KeyError):
        builder.prepare_acquisition_function(
            {"bar": QuadraticMeanAndRBFKernel()}, {"bar": empty_dataset([1], [1])}, None
        )


def test_single_model_greedy_acquisition_builder_repr_includes_class_name() -> None:
    builder = _ArbitraryGreedySingleBuilder()
    assert type(builder).__name__ in repr(builder)


def test_expected_improvement_builder_builds_expected_improvement_using_best_from_model() -> None:
    dataset = Dataset(
        tf.constant([[-2.0], [-1.0], [0.0], [1.0], [2.0]]),
        tf.constant([[4.1], [0.9], [0.1], [1.1], [3.9]]),
    )
    model = QuadraticMeanAndRBFKernel()
    acq_fn = ExpectedImprovement().prepare_acquisition_function(model, dataset=dataset)
    xs = tf.linspace([[-10.0]], [[10.0]], 100)
    expected = expected_improvement(model, tf.constant([0.0]))(xs)
    npt.assert_allclose(acq_fn(xs), expected)


def test_expected_improvement_builder_updates_expected_improvement_using_best_from_model() -> None:
    dataset = Dataset(
        tf.constant([[-2.0], [-1.0]]),
        tf.constant([[4.1], [0.9]]),
    )
    model = QuadraticMeanAndRBFKernel()
    acq_fn = ExpectedImprovement().prepare_acquisition_function(model, dataset=dataset)
    assert acq_fn.__call__._get_tracing_count() == 0  # type: ignore
    xs = tf.linspace([[-10.0]], [[10.0]], 100)
    expected = expected_improvement(model, tf.constant([1.0]))(xs)
    npt.assert_allclose(acq_fn(xs), expected)
    assert acq_fn.__call__._get_tracing_count() == 1  # type: ignore

    new_dataset = Dataset(
        tf.concat([dataset.query_points, tf.constant([[0.0], [1.0], [2.0]])], 0),
        tf.concat([dataset.observations, tf.constant([[0.1], [1.1], [3.9]])], 0),
    )
    updated_acq_fn = ExpectedImprovement().update_acquisition_function(
        acq_fn, model, dataset=new_dataset
    )
    assert updated_acq_fn == acq_fn
    expected = expected_improvement(model, tf.constant([0.0]))(xs)
    npt.assert_allclose(acq_fn(xs), expected)
    assert acq_fn.__call__._get_tracing_count() == 1  # type: ignore


def test_expected_improvement_builder_raises_for_empty_data() -> None:
    data = Dataset(tf.zeros([0, 1]), tf.ones([0, 1]))

    with pytest.raises(tf.errors.InvalidArgumentError):
        ExpectedImprovement().prepare_acquisition_function(
            QuadraticMeanAndRBFKernel(), dataset=data
        )
    with pytest.raises(tf.errors.InvalidArgumentError):
        ExpectedImprovement().prepare_acquisition_function(QuadraticMeanAndRBFKernel())


@pytest.mark.parametrize("at", [tf.constant([[0.0], [1.0]]), tf.constant([[[0.0], [1.0]]])])
def test_expected_improvement_raises_for_invalid_batch_size(at: TensorType) -> None:
    ei = expected_improvement(QuadraticMeanAndRBFKernel(), tf.constant([1.0]))

    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        ei(at)


@random_seed
@pytest.mark.parametrize("best", [tf.constant([50.0]), BRANIN_MINIMUM, BRANIN_MINIMUM * 1.01])
@pytest.mark.parametrize("test_update", [False, True])
@pytest.mark.parametrize(
    "variance_scale, num_samples_per_point, rtol, atol",
    [
        (0.1, 1000, 0.01, 1e-9),
        (1.0, 50_000, 0.01, 1e-3),
        (10.0, 100_000, 0.01, 1e-2),
        (100.0, 150_000, 0.01, 1e-1),
    ],
)
def test_expected_improvement(
    variance_scale: float,
    num_samples_per_point: int,
    best: tf.Tensor,
    rtol: float,
    atol: float,
    test_update: bool,
) -> None:
    variance_scale = tf.constant(variance_scale, tf.float64)
    best = tf.cast(best, dtype=tf.float64)

    x_range = tf.linspace(0.0, 1.0, 11)
    x_range = tf.cast(x_range, dtype=tf.float64)
    xs = tf.reshape(tf.stack(tf.meshgrid(x_range, x_range, indexing="ij"), axis=-1), (-1, 2))

    kernel = tfp.math.psd_kernels.MaternFiveHalves(variance_scale, length_scale=0.25)
    model = GaussianProcess([branin], [kernel])

    mean, variance = model.predict(xs)
    samples = tfp.distributions.Normal(mean, tf.sqrt(variance)).sample(num_samples_per_point)
    samples_improvement = tf.where(samples < best, best - samples, 0)
    ei_approx = tf.reduce_mean(samples_improvement, axis=0)

    if test_update:
        eif = expected_improvement(model, tf.constant([100.0], dtype=tf.float64))
        eif.update(best)
    else:
        eif = expected_improvement(model, best)
    ei = eif(xs[..., None, :])

    npt.assert_allclose(ei, ei_approx, rtol=rtol, atol=atol)


def test_augmented_expected_improvement_builder_raises_for_empty_data() -> None:
    data = Dataset(tf.zeros([0, 1]), tf.ones([0, 1]))

    with pytest.raises(tf.errors.InvalidArgumentError):
        AugmentedExpectedImprovement().prepare_acquisition_function(
            QuadraticMeanAndRBFKernel(),
            dataset=data,
        )
    with pytest.raises(tf.errors.InvalidArgumentError):
        AugmentedExpectedImprovement().prepare_acquisition_function(QuadraticMeanAndRBFKernel())


@pytest.mark.parametrize("at", [tf.constant([[0.0], [1.0]]), tf.constant([[[0.0], [1.0]]])])
def test_augmented_expected_improvement_raises_for_invalid_batch_size(at: TensorType) -> None:
    aei = augmented_expected_improvement(QuadraticMeanAndRBFKernel(), tf.constant([1.0]))

    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        aei(at)


def test_augmented_expected_improvement_raises_for_invalid_model() -> None:
    class dummy_model_without_likelihood(ProbabilisticModel):
        def predict(self, query_points: TensorType) -> tuple[None, None]:
            return None, None

        def predict_joint(self, query_points: TensorType) -> tuple[None, None]:
            return None, None

        def sample(self, query_points: TensorType, num_samples: int) -> None:
            return None

    with pytest.raises(ValueError):
        model_without_likelihood = dummy_model_without_likelihood()
        augmented_expected_improvement(model_without_likelihood, tf.constant([1.0]))


@pytest.mark.parametrize("observation_noise", [1e-8, 1.0, 10.0])
def test_augmented_expected_improvement_builder_builds_expected_improvement_times_augmentation(
    observation_noise: float,
) -> None:
    dataset = Dataset(
        tf.constant([[-2.0], [-1.0], [0.0], [1.0], [2.0]]),
        tf.constant([[4.1], [0.9], [0.1], [1.1], [3.9]]),
    )

    model = QuadraticMeanAndRBFKernel(noise_variance=observation_noise)
    acq_fn = AugmentedExpectedImprovement().prepare_acquisition_function(model, dataset=dataset)

    xs = tf.linspace([[-10.0]], [[10.0]], 100)
    ei = ExpectedImprovement().prepare_acquisition_function(model, dataset=dataset)(xs)

    @tf.function
    def augmentation() -> TensorType:
        _, variance = model.predict(tf.squeeze(xs, -2))
        return 1.0 - (tf.math.sqrt(observation_noise)) / (
            tf.math.sqrt(observation_noise + variance)
        )

    npt.assert_allclose(acq_fn(xs), ei * augmentation(), rtol=1e-6)


@pytest.mark.parametrize("observation_noise", [1e-8, 1.0, 10.0])
def test_augmented_expected_improvement_builder_updates_acquisition_function(
    observation_noise: float,
) -> None:
    partial_dataset = Dataset(
        tf.constant([[-2.0], [-1.0]]),
        tf.constant([[4.1], [0.9]]),
    )
    full_dataset = Dataset(
        tf.constant([[-2.0], [-1.0], [0.0], [1.0], [2.0]]),
        tf.constant([[4.1], [0.9], [0.1], [1.1], [3.9]]),
    )
    model = QuadraticMeanAndRBFKernel(noise_variance=observation_noise)

    partial_data_acq_fn = AugmentedExpectedImprovement().prepare_acquisition_function(
        model,
        dataset=partial_dataset,
    )
    updated_acq_fn = AugmentedExpectedImprovement().update_acquisition_function(
        partial_data_acq_fn,
        model,
        dataset=full_dataset,
    )
    assert updated_acq_fn == partial_data_acq_fn
    full_data_acq_fn = AugmentedExpectedImprovement().prepare_acquisition_function(
        model, dataset=full_dataset
    )

    xs = tf.linspace([[-10.0]], [[10.0]], 100)
    npt.assert_allclose(updated_acq_fn(xs), full_data_acq_fn(xs))


def test_min_value_entropy_search_builder_raises_for_empty_data() -> None:
    empty_data = Dataset(tf.zeros([0, 2], dtype=tf.float64), tf.ones([0, 2], dtype=tf.float64))
    non_empty_data = Dataset(tf.zeros([3, 2], dtype=tf.float64), tf.ones([3, 2], dtype=tf.float64))
    search_space = Box([0, 0], [1, 1])
    builder = MinValueEntropySearch(search_space)
    with pytest.raises(tf.errors.InvalidArgumentError):
        builder.prepare_acquisition_function(QuadraticMeanAndRBFKernel(), dataset=empty_data)
    with pytest.raises(tf.errors.InvalidArgumentError):
        builder.prepare_acquisition_function(QuadraticMeanAndRBFKernel())
    acq = builder.prepare_acquisition_function(QuadraticMeanAndRBFKernel(), dataset=non_empty_data)
    with pytest.raises(tf.errors.InvalidArgumentError):
        builder.update_acquisition_function(acq, QuadraticMeanAndRBFKernel(), dataset=empty_data)
    with pytest.raises(tf.errors.InvalidArgumentError):
        builder.update_acquisition_function(acq, QuadraticMeanAndRBFKernel())


@pytest.mark.parametrize("param", [-2, 0])
def test_min_value_entropy_search_builder_raises_for_invalid_init_params(param: int) -> None:
    search_space = Box([0, 0], [1, 1])
    with pytest.raises(tf.errors.InvalidArgumentError):
        MinValueEntropySearch(search_space, num_samples=param)
    with pytest.raises(tf.errors.InvalidArgumentError):
        MinValueEntropySearch(search_space, grid_size=param)
    with pytest.raises(tf.errors.InvalidArgumentError):
        MinValueEntropySearch(search_space, num_fourier_features=param)


def test_min_value_entropy_search_builder_raises_when_given_num_features_and_gumbel() -> None:
    # cannot do feature-based approx of Gumbel sampler
    search_space = Box([0, 0], [1, 1])
    with pytest.raises(tf.errors.InvalidArgumentError):
        MinValueEntropySearch(search_space, use_thompson=False, num_fourier_features=10)


@unittest.mock.patch("trieste.acquisition.function.min_value_entropy_search")
@pytest.mark.parametrize("use_thompson", [True, False])
def test_min_value_entropy_search_builder_builds_min_value_samples(
    mocked_mves: MagicMock, use_thompson: bool
) -> None:
    dataset = Dataset(tf.zeros([3, 2], dtype=tf.float64), tf.ones([3, 2], dtype=tf.float64))
    search_space = Box([0, 0], [1, 1])
    builder = MinValueEntropySearch(search_space, use_thompson=use_thompson)
    model = QuadraticMeanAndRBFKernel()
    builder.prepare_acquisition_function(model, dataset=dataset)
    mocked_mves.assert_called_once()

    # check that the Gumbel samples look sensible
    min_value_samples = mocked_mves.call_args[0][1]
    query_points = builder._search_space.sample(num_samples=builder._grid_size)
    query_points = tf.concat([dataset.query_points, query_points], 0)
    fmean, _ = model.predict(query_points)
    assert max(min_value_samples) < min(fmean)


@pytest.mark.parametrize("use_thompson", [True, False, 100])
def test_min_value_entropy_search_builder_updates_acquisition_function(use_thompson: bool) -> None:
    search_space = Box([0.0, 0.0], [1.0, 1.0])
    model = QuadraticMeanAndRBFKernel(noise_variance=tf.constant(1e-10, dtype=tf.float64))
    model.kernel = (
        gpflow.kernels.RBF()
    )  # need a gpflow kernel object for random feature decompositions

    x_range = tf.linspace(0.0, 1.0, 5)
    x_range = tf.cast(x_range, dtype=tf.float64)
    xs = tf.reshape(tf.stack(tf.meshgrid(x_range, x_range, indexing="ij"), axis=-1), (-1, 2))
    ys = quadratic(xs)
    partial_dataset = Dataset(xs[:10], ys[:10])
    full_dataset = Dataset(xs, ys)

    builder = MinValueEntropySearch(
        search_space,
        use_thompson=bool(use_thompson),
        num_fourier_features=None if isinstance(use_thompson, bool) else use_thompson,
    )
    xs = tf.cast(tf.linspace([[0.0]], [[1.0]], 10), tf.float64)

    old_acq_fn = builder.prepare_acquisition_function(model, dataset=partial_dataset)
    tf.random.set_seed(0)  # to ensure consistent sampling
    updated_acq_fn = builder.update_acquisition_function(old_acq_fn, model, dataset=full_dataset)
    assert updated_acq_fn == old_acq_fn
    updated_values = updated_acq_fn(xs)

    tf.random.set_seed(0)  # to ensure consistent sampling
    new_acq_fn = builder.prepare_acquisition_function(model, dataset=full_dataset)
    new_values = new_acq_fn(xs)

    npt.assert_allclose(updated_values, new_values)


@random_seed
@unittest.mock.patch("trieste.acquisition.function.min_value_entropy_search")
def test_min_value_entropy_search_builder_builds_min_value_samples_rff(
    mocked_mves: MagicMock,
) -> None:
    search_space = Box([0.0, 0.0], [1.0, 1.0])
    model = QuadraticMeanAndRBFKernel(noise_variance=tf.constant(1e-10, dtype=tf.float64))
    model.kernel = (
        gpflow.kernels.RBF()
    )  # need a gpflow kernel object for random feature decompositions

    x_range = tf.linspace(0.0, 1.0, 5)
    x_range = tf.cast(x_range, dtype=tf.float64)
    xs = tf.reshape(tf.stack(tf.meshgrid(x_range, x_range, indexing="ij"), axis=-1), (-1, 2))
    ys = quadratic(xs)
    dataset = Dataset(xs, ys)

    builder = MinValueEntropySearch(search_space, use_thompson=True, num_fourier_features=100)
    builder.prepare_acquisition_function(model, dataset=dataset)
    mocked_mves.assert_called_once()

    # check that the Gumbel samples look sensible
    min_value_samples = mocked_mves.call_args[0][1]
    query_points = builder._search_space.sample(num_samples=builder._grid_size)
    query_points = tf.concat([dataset.query_points, query_points], 0)
    fmean, _ = model.predict(query_points)
    assert max(min_value_samples) < min(fmean) + 1e-4


@pytest.mark.parametrize("samples", [tf.constant([]), tf.constant([[[]]])])
def test_min_value_entropy_search_raises_for_min_values_samples_with_invalid_shape(
    samples: TensorType,
) -> None:
    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        min_value_entropy_search(QuadraticMeanAndRBFKernel(), samples)


@pytest.mark.parametrize("at", [tf.constant([[0.0], [1.0]]), tf.constant([[[0.0], [1.0]]])])
def test_min_value_entropy_search_raises_for_invalid_batch_size(at: TensorType) -> None:
    mes = min_value_entropy_search(QuadraticMeanAndRBFKernel(), tf.constant([[1.0], [2.0]]))

    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        mes(at)


def test_min_value_entropy_search_returns_correct_shape() -> None:
    model = QuadraticMeanAndRBFKernel()
    min_value_samples = tf.constant([[1.0], [2.0]])
    query_at = tf.linspace([[-10.0]], [[10.0]], 5)
    evals = min_value_entropy_search(model, min_value_samples)(query_at)
    npt.assert_array_equal(evals.shape, tf.constant([5, 1]))


def test_min_value_entropy_search_chooses_same_as_probability_of_improvement() -> None:
    """
    When based on a single max-value sample, MES should choose the same point that probability of
    improvement would when calcualted with the max-value as its threshold (See :cite:`wang2017max`).
    """

    kernel = tfp.math.psd_kernels.MaternFiveHalves()
    model = GaussianProcess([branin], [kernel])

    x_range = tf.linspace(0.0, 1.0, 11)
    x_range = tf.cast(x_range, dtype=tf.float64)
    xs = tf.reshape(tf.stack(tf.meshgrid(x_range, x_range, indexing="ij"), axis=-1), (-1, 2))

    min_value_sample = tf.constant([[1.0]], dtype=tf.float64)
    mes_evals = min_value_entropy_search(model, min_value_sample)(xs[..., None, :])

    mean, variance = model.predict(xs)
    gamma = (tf.cast(min_value_sample, dtype=mean.dtype) - mean) / tf.sqrt(variance)
    norm = tfp.distributions.Normal(tf.cast(0, dtype=mean.dtype), tf.cast(1, dtype=mean.dtype))
    pi_evals = norm.cdf(gamma)

    npt.assert_array_equal(tf.argmax(mes_evals), tf.argmax(pi_evals))


def test_negative_lower_confidence_bound_builder_builds_negative_lower_confidence_bound() -> None:
    model = QuadraticMeanAndRBFKernel()
    beta = 1.96
    acq_fn = NegativeLowerConfidenceBound(beta).prepare_acquisition_function(model)
    query_at = tf.linspace([[-10]], [[10]], 100)
    expected = -lower_confidence_bound(model, beta)(query_at)
    npt.assert_array_almost_equal(acq_fn(query_at), expected)


def test_negative_lower_confidence_bound_builder_updates_without_retracing() -> None:
    model = QuadraticMeanAndRBFKernel()
    beta = 1.96
    builder = NegativeLowerConfidenceBound(beta)
    acq_fn = builder.prepare_acquisition_function(model)
    assert acq_fn._get_tracing_count() == 0  # type: ignore
    query_at = tf.linspace([[-10]], [[10]], 100)
    expected = -lower_confidence_bound(model, beta)(query_at)
    npt.assert_array_almost_equal(acq_fn(query_at), expected)
    assert acq_fn._get_tracing_count() == 1  # type: ignore

    up_acq_fn = builder.update_acquisition_function(acq_fn, model)
    assert up_acq_fn == acq_fn
    npt.assert_array_almost_equal(acq_fn(query_at), expected)
    assert acq_fn._get_tracing_count() == 1  # type: ignore


@pytest.mark.parametrize("beta", [-0.1, -2.0])
def test_lower_confidence_bound_raises_for_negative_beta(beta: float) -> None:
    with pytest.raises(tf.errors.InvalidArgumentError):
        lower_confidence_bound(QuadraticMeanAndRBFKernel(), beta)


@pytest.mark.parametrize("at", [tf.constant([[0.0], [1.0]]), tf.constant([[[0.0], [1.0]]])])
def test_lower_confidence_bound_raises_for_invalid_batch_size(at: TensorType) -> None:
    lcb = lower_confidence_bound(QuadraticMeanAndRBFKernel(), tf.constant(1.0))

    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        lcb(at)


@pytest.mark.parametrize("beta", [0.0, 0.1, 7.8])
def test_lower_confidence_bound(beta: float) -> None:
    query_at = tf.linspace([[-3]], [[3]], 10)
    actual = lower_confidence_bound(QuadraticMeanAndRBFKernel(), beta)(query_at)
    npt.assert_array_almost_equal(actual, tf.squeeze(query_at, -2) ** 2 - beta)


@pytest.mark.parametrize(
    "threshold, at, expected",
    [
        (0.0, tf.constant([[0.0]]), 0.5),
        # values looked up on a standard normal table
        (2.0, tf.constant([[1.0]]), 0.5 + 0.34134),
        (-0.25, tf.constant([[-0.5]]), 0.5 - 0.19146),
    ],
)
def test_probability_of_feasibility(threshold: float, at: tf.Tensor, expected: float) -> None:
    actual = probability_of_feasibility(QuadraticMeanAndRBFKernel(), threshold)(at)
    npt.assert_allclose(actual, expected, rtol=1e-4)


@pytest.mark.parametrize(
    "at",
    [
        tf.constant([[0.0]], tf.float64),
        tf.constant([[-3.4]], tf.float64),
        tf.constant([[0.2]], tf.float64),
    ],
)
@pytest.mark.parametrize("threshold", [-2.3, 0.2])
def test_probability_of_feasibility_builder_builds_pof(threshold: float, at: tf.Tensor) -> None:
    builder = ProbabilityOfFeasibility(threshold)
    acq = builder.prepare_acquisition_function(QuadraticMeanAndRBFKernel())
    expected = probability_of_feasibility(QuadraticMeanAndRBFKernel(), threshold)(at)

    npt.assert_allclose(acq(at), expected)


@pytest.mark.parametrize("shape", various_shapes() - {()})
def test_probability_of_feasibility_raises_on_non_scalar_threshold(shape: ShapeLike) -> None:
    threshold = tf.ones(shape)
    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        probability_of_feasibility(QuadraticMeanAndRBFKernel(), threshold)


@pytest.mark.parametrize("shape", [[], [0], [2], [2, 1], [1, 2, 1]])
def test_probability_of_feasibility_raises_on_invalid_at_shape(shape: ShapeLike) -> None:
    at = tf.ones(shape)
    pof = probability_of_feasibility(QuadraticMeanAndRBFKernel(), 0.0)
    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        pof(at)


@pytest.mark.parametrize("shape", various_shapes() - {()})
def test_probability_of_feasibility_builder_raises_on_non_scalar_threshold(
    shape: ShapeLike,
) -> None:
    threshold = tf.ones(shape)
    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        ProbabilityOfFeasibility(threshold)


@pytest.mark.parametrize("at", [tf.constant([[0.0]], tf.float64)])
@pytest.mark.parametrize("threshold", [-2.3, 0.2])
def test_probability_of_feasibility_builder_updates_without_retracing(
    threshold: float, at: tf.Tensor
) -> None:
    builder = ProbabilityOfFeasibility(threshold)
    model = QuadraticMeanAndRBFKernel()
    expected = probability_of_feasibility(QuadraticMeanAndRBFKernel(), threshold)(at)
    acq = builder.prepare_acquisition_function(model)
    assert acq._get_tracing_count() == 0  # type: ignore
    npt.assert_allclose(acq(at), expected)
    assert acq._get_tracing_count() == 1  # type: ignore
    up_acq = builder.update_acquisition_function(acq, model)
    assert up_acq == acq
    npt.assert_allclose(acq(at), expected)
    assert acq._get_tracing_count() == 1  # type: ignore


@pytest.mark.parametrize(
    "function",
    [
        ExpectedConstrainedImprovement,
        ExpectedConstrainedHypervolumeImprovement,
    ],
)
def test_expected_constrained_improvement_raises_for_non_scalar_min_pof(
    function: type[ExpectedConstrainedImprovement | ExpectedConstrainedHypervolumeImprovement],
) -> None:
    pof = ProbabilityOfFeasibility(0.0).using("")
    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        function("", pof, tf.constant([0.0]))


@pytest.mark.parametrize(
    "function",
    [
        ExpectedConstrainedImprovement,
        ExpectedConstrainedHypervolumeImprovement,
    ],
)
def test_expected_constrained_improvement_raises_for_out_of_range_min_pof(
    function: type[ExpectedConstrainedImprovement | ExpectedConstrainedHypervolumeImprovement],
) -> None:
    pof = ProbabilityOfFeasibility(0.0).using("")
    with pytest.raises(tf.errors.InvalidArgumentError):
        function("", pof, 1.5)


@pytest.mark.parametrize("at", [tf.constant([[0.0], [1.0]]), tf.constant([[[0.0], [1.0]]])])
def test_expected_constrained_improvement_raises_for_invalid_batch_size(at: TensorType) -> None:
    pof = ProbabilityOfFeasibility(0.0).using("")
    builder = ExpectedConstrainedImprovement("", pof, tf.constant(0.0))
    initial_query_points = tf.constant([[-1.0]])
    initial_objective_function_values = tf.constant([[1.0]])
    data = {"": Dataset(initial_query_points, initial_objective_function_values)}

    eci = builder.prepare_acquisition_function({"": QuadraticMeanAndRBFKernel()}, datasets=data)

    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        eci(at)


def test_expected_constrained_improvement_can_reproduce_expected_improvement() -> None:
    class _Certainty(AcquisitionFunctionBuilder):
        def prepare_acquisition_function(
            self,
            models: Mapping[str, ProbabilisticModel],
            datasets: Optional[Mapping[str, Dataset]] = None,
        ) -> AcquisitionFunction:
            return lambda x: tf.ones_like(tf.squeeze(x, -2))

    data = {"foo": Dataset(tf.constant([[0.5]]), tf.constant([[0.25]]))}
    models_ = {"foo": QuadraticMeanAndRBFKernel()}

    builder = ExpectedConstrainedImprovement("foo", _Certainty(), 0)
    eci = builder.prepare_acquisition_function(models_, datasets=data)

    ei = ExpectedImprovement().using("foo").prepare_acquisition_function(models_, datasets=data)

    at = tf.constant([[[-0.1]], [[1.23]], [[-6.78]]])
    npt.assert_allclose(eci(at), ei(at))

    new_data = {"foo": Dataset(tf.constant([[0.5], [1.0]]), tf.constant([[0.25], [0.5]]))}
    up_eci = builder.update_acquisition_function(eci, models_, datasets=new_data)
    assert up_eci == eci
    up_ei = (
        ExpectedImprovement().using("foo").prepare_acquisition_function(models_, datasets=new_data)
    )

    npt.assert_allclose(eci(at), up_ei(at))
    assert eci._get_tracing_count() == 1  # type: ignore


def test_expected_constrained_improvement_is_relative_to_feasible_point() -> None:
    class _Constraint(AcquisitionFunctionBuilder):
        def prepare_acquisition_function(
            self,
            models: Mapping[str, ProbabilisticModel],
            datasets: Optional[Mapping[str, Dataset]] = None,
        ) -> AcquisitionFunction:
            return lambda x: tf.cast(tf.squeeze(x, -2) >= 0, x.dtype)

    models_ = {"foo": QuadraticMeanAndRBFKernel()}

    eci_data = {"foo": Dataset(tf.constant([[-0.2], [0.3]]), tf.constant([[0.04], [0.09]]))}
    eci = ExpectedConstrainedImprovement("foo", _Constraint()).prepare_acquisition_function(
        models_,
        datasets=eci_data,
    )

    ei_data = {"foo": Dataset(tf.constant([[0.3]]), tf.constant([[0.09]]))}
    ei = ExpectedImprovement().using("foo").prepare_acquisition_function(models_, datasets=ei_data)

    npt.assert_allclose(eci(tf.constant([[0.1]])), ei(tf.constant([[0.1]])))


def test_expected_constrained_improvement_is_less_for_constrained_points() -> None:
    class _Constraint(AcquisitionFunctionBuilder):
        def prepare_acquisition_function(
            self,
            models: Mapping[str, ProbabilisticModel],
            datasets: Optional[Mapping[str, Dataset]] = None,
        ) -> AcquisitionFunction:
            return lambda x: tf.cast(tf.squeeze(x, -2) >= 0, x.dtype)

    def two_global_minima(x: tf.Tensor) -> tf.Tensor:
        return x ** 4 / 4 - x ** 2 / 2

    initial_query_points = tf.constant([[-2.0], [0.0], [1.2]])
    data = {"foo": Dataset(initial_query_points, two_global_minima(initial_query_points))}
    models_ = {"foo": GaussianProcess([two_global_minima], [rbf()])}

    eci = ExpectedConstrainedImprovement("foo", _Constraint()).prepare_acquisition_function(
        models_,
        datasets=data,
    )

    npt.assert_array_less(eci(tf.constant([[-1.0]])), eci(tf.constant([[1.0]])))


@pytest.mark.parametrize(
    "function",
    [
        ExpectedConstrainedImprovement,
        ExpectedConstrainedHypervolumeImprovement,
    ],
)
def test_expected_constrained_improvement_raises_for_empty_data(
    function: type[ExpectedConstrainedImprovement | ExpectedConstrainedHypervolumeImprovement],
) -> None:
    class _Constraint(AcquisitionFunctionBuilder):
        def prepare_acquisition_function(
            self,
            models: Mapping[str, ProbabilisticModel],
            datasets: Optional[Mapping[str, Dataset]] = None,
        ) -> AcquisitionFunction:
            return raise_exc

    data = {"foo": Dataset(tf.zeros([0, 2]), tf.zeros([0, 1]))}
    models_ = {"foo": QuadraticMeanAndRBFKernel()}
    builder = function("foo", _Constraint())

    with pytest.raises(tf.errors.InvalidArgumentError):
        builder.prepare_acquisition_function(models_, datasets=data)
    with pytest.raises(tf.errors.InvalidArgumentError):
        builder.prepare_acquisition_function(models_)


def test_expected_constrained_improvement_is_constraint_when_no_feasible_points() -> None:
    class _Constraint(AcquisitionFunctionBuilder):
        def prepare_acquisition_function(
            self,
            models: Mapping[str, ProbabilisticModel],
            datasets: Optional[Mapping[str, Dataset]] = None,
        ) -> AcquisitionFunction:
            def acquisition(x: TensorType) -> TensorType:
                x_ = tf.squeeze(x, -2)
                return tf.cast(tf.logical_and(0.0 <= x_, x_ < 1.0), x.dtype)

            return acquisition

    data = {"foo": Dataset(tf.constant([[-2.0], [1.0]]), tf.constant([[4.0], [1.0]]))}
    models_ = {"foo": QuadraticMeanAndRBFKernel()}
    eci = ExpectedConstrainedImprovement("foo", _Constraint()).prepare_acquisition_function(
        models_,
        datasets=data,
    )

    constraint_fn = _Constraint().prepare_acquisition_function(models_, datasets=data)

    xs = tf.linspace([[-10.0]], [[10.0]], 100)
    npt.assert_allclose(eci(xs), constraint_fn(xs))


def test_expected_constrained_improvement_min_feasibility_probability_bound_is_inclusive() -> None:
    def pof(x_: TensorType) -> TensorType:
        return tfp.bijectors.Sigmoid().forward(tf.squeeze(x_, -2))

    class _Constraint(AcquisitionFunctionBuilder):
        def prepare_acquisition_function(
            self,
            models: Mapping[str, ProbabilisticModel],
            datasets: Optional[Mapping[str, Dataset]] = None,
        ) -> AcquisitionFunction:
            return pof

    models_ = {"foo": QuadraticMeanAndRBFKernel()}

    data = {"foo": Dataset(tf.constant([[1.1], [2.0]]), tf.constant([[1.21], [4.0]]))}
    eci = ExpectedConstrainedImprovement(
        "foo", _Constraint(), min_feasibility_probability=tfp.bijectors.Sigmoid().forward(1.0)
    ).prepare_acquisition_function(
        models_,
        datasets=data,
    )

    ei = ExpectedImprovement().using("foo").prepare_acquisition_function(models_, datasets=data)
    x = tf.constant([[1.5]])
    npt.assert_allclose(eci(x), ei(x) * pof(x))


def _mo_test_model(num_obj: int, *kernel_amplitudes: float | TensorType | None) -> GaussianProcess:
    means = [quadratic, lambda x: tf.reduce_sum(x, axis=-1, keepdims=True), quadratic]
    kernels = [tfp.math.psd_kernels.ExponentiatedQuadratic(k_amp) for k_amp in kernel_amplitudes]
    return GaussianProcess(means[:num_obj], kernels[:num_obj])


def test_ehvi_builder_raises_for_empty_data() -> None:
    num_obj = 3
    dataset = empty_dataset([2], [num_obj])
    model = QuadraticMeanAndRBFKernel()

    with pytest.raises(tf.errors.InvalidArgumentError):
        ExpectedHypervolumeImprovement().prepare_acquisition_function(model, dataset=dataset)
    with pytest.raises(tf.errors.InvalidArgumentError):
        ExpectedHypervolumeImprovement().prepare_acquisition_function(model, dataset)


def test_ehvi_builder_builds_expected_hv_improvement_using_pareto_from_model() -> None:
    num_obj = 2
    train_x = tf.constant([[-2.0], [-1.5], [-1.0], [0.0], [0.5], [1.0], [1.5], [2.0]])
    dataset = Dataset(
        train_x,
        tf.tile(
            tf.constant([[4.1], [0.9], [1.2], [0.1], [-8.8], [1.1], [2.1], [3.9]]), [1, num_obj]
        ),
    )

    model = _mo_test_model(num_obj, *[10, 10] * num_obj)
    acq_fn = ExpectedHypervolumeImprovement().prepare_acquisition_function(model, dataset=dataset)

    model_pred_observation = model.predict(train_x)[0]
    _prt = Pareto(model_pred_observation)
    _partition_bounds = ExactPartition2dNonDominated(_prt.front).partition_bounds(
        tf.constant([-1e10] * 2), get_reference_point(_prt.front)
    )
    xs = tf.linspace([[-10.0]], [[10.0]], 100)
    expected = expected_hv_improvement(model, _partition_bounds)(xs)
    npt.assert_allclose(acq_fn(xs), expected)


def test_ehvi_builder_updates_expected_hv_improvement_using_pareto_from_model() -> None:
    num_obj = 2
    train_x = tf.constant([[-2.0], [-1.5], [-1.0], [0.0], [0.5], [1.0], [1.5], [2.0]])
    dataset = Dataset(
        train_x,
        tf.tile(
            tf.constant([[4.1], [0.9], [1.2], [0.1], [-8.8], [1.1], [2.1], [3.9]]), [1, num_obj]
        ),
    )
    partial_dataset = Dataset(dataset.query_points[:4], dataset.observations[:4])
    xs = tf.linspace([[-10.0]], [[10.0]], 100)

    model = _mo_test_model(num_obj, *[10, 10] * num_obj)
    acq_fn = ExpectedHypervolumeImprovement().prepare_acquisition_function(
        model, dataset=partial_dataset
    )
    assert acq_fn.__call__._get_tracing_count() == 0  # type: ignore
    model_pred_observation = model.predict(train_x)[0]
    _prt = Pareto(model_pred_observation)
    _partition_bounds = ExactPartition2dNonDominated(_prt.front).partition_bounds(
        tf.constant([-1e10] * 2), get_reference_point(_prt.front)
    )
    expected = expected_hv_improvement(model, _partition_bounds)(xs)
    npt.assert_allclose(acq_fn(xs), expected)
    assert acq_fn.__call__._get_tracing_count() == 1  # type: ignore

    # update the acquisition function, evaluate it, and check that it hasn't been retraced
    updated_acq_fn = ExpectedHypervolumeImprovement().update_acquisition_function(
        acq_fn,
        model,
        dataset=dataset,
    )
    assert updated_acq_fn == acq_fn
    model_pred_observation = model.predict(train_x)[0]
    _prt = Pareto(model_pred_observation)
    _partition_bounds = ExactPartition2dNonDominated(_prt.front).partition_bounds(
        tf.constant([-1e10] * 2), get_reference_point(_prt.front)
    )
    expected = expected_hv_improvement(model, _partition_bounds)(xs)
    npt.assert_allclose(acq_fn(xs), expected)
    assert acq_fn.__call__._get_tracing_count() == 1  # type: ignore


@pytest.mark.parametrize("at", [tf.constant([[0.0], [1.0]]), tf.constant([[[0.0], [1.0]]])])
def test_ehvi_raises_for_invalid_batch_size(at: TensorType) -> None:
    num_obj = 2
    train_x = tf.constant([[-2.0], [-1.5], [-1.0], [0.0], [0.5], [1.0], [1.5], [2.0]])

    model = _mo_test_model(num_obj, *[None] * num_obj)
    model_pred_observation = model.predict(train_x)[0]
    _prt = Pareto(model_pred_observation)
    _partition_bounds = ExactPartition2dNonDominated(_prt.front).partition_bounds(
        tf.constant([-math.inf] * 2), get_reference_point(_prt.front)
    )
    ehvi = expected_hv_improvement(model, _partition_bounds)

    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        ehvi(at)


@random_seed
@pytest.mark.parametrize(
    "input_dim, num_samples_per_point, existing_observations, obj_num, variance_scale",
    [
        pytest.param(
            1,
            100_000,
            tf.constant([[0.3, 0.2], [0.2, 0.22], [0.1, 0.25], [0.0, 0.3]]),
            2,
            1.0,
            id="1d_input_2obj_gp_var_1",
        ),
        pytest.param(
            1,
            200_000,
            tf.constant([[0.3, 0.2], [0.2, 0.22], [0.1, 0.25], [0.0, 0.3]]),
            2,
            2.0,
            id="1d_input_2obj_gp_var_2",
        ),
        pytest.param(2, 50_000, tf.constant([[0.0, 0.0]]), 2, 1.0, id="2d_input_2obj_gp_var_2"),
        pytest.param(
            3,
            50_000,
            tf.constant([[2.0, 1.0], [0.8, 3.0]]),
            2,
            1.0,
            id="3d_input_2obj_gp_var_1",
        ),
        pytest.param(
            4,
            100_000,
            tf.constant([[3.0, 2.0, 1.0], [1.1, 2.0, 3.0]]),
            3,
            1.0,
            id="4d_input_3obj_gp_var_1",
        ),
    ],
)
def test_expected_hypervolume_improvement_matches_monte_carlo(
    input_dim: int,
    num_samples_per_point: int,
    existing_observations: tf.Tensor,
    obj_num: int,
    variance_scale: float,
) -> None:
    # Note: the test data number grows exponentially with num of obj
    data_num_seg_per_dim = 2  # test data number per input dim
    N = data_num_seg_per_dim ** input_dim
    xs = tf.convert_to_tensor(
        list(itertools.product(*[list(tf.linspace(-1, 1, data_num_seg_per_dim))] * input_dim))
    )

    xs = tf.cast(xs, dtype=existing_observations.dtype)
    model = _mo_test_model(obj_num, *[variance_scale] * obj_num)
    mean, variance = model.predict(xs)

    predict_samples = tfp.distributions.Normal(mean, tf.sqrt(variance)).sample(
        num_samples_per_point  # [f_samples, batch_size, obj_num]
    )
    _pareto = Pareto(existing_observations)
    ref_pt = get_reference_point(_pareto.front)
    lb_points, ub_points = prepare_default_non_dominated_partition_bounds(ref_pt, _pareto.front)

    # calc MC approx EHVI
    splus_valid = tf.reduce_all(
        tf.tile(ub_points[tf.newaxis, :, tf.newaxis, :], [num_samples_per_point, 1, N, 1])
        > tf.expand_dims(predict_samples, axis=1),
        axis=-1,  # can predict_samples contribute to hvi in cell
    )  # [f_samples, num_cells,  B]
    splus_idx = tf.expand_dims(tf.cast(splus_valid, dtype=ub_points.dtype), -1)
    splus_lb = tf.tile(lb_points[tf.newaxis, :, tf.newaxis, :], [num_samples_per_point, 1, N, 1])
    splus_lb = tf.maximum(  # max of lower bounds and predict_samples
        splus_lb, tf.expand_dims(predict_samples, 1)
    )
    splus_ub = tf.tile(ub_points[tf.newaxis, :, tf.newaxis, :], [num_samples_per_point, 1, N, 1])
    splus = tf.concat(  # concatenate validity labels and possible improvements
        [splus_idx, splus_ub - splus_lb], axis=-1
    )

    # calculate hyper-volume improvement over the non-dominated cells
    ehvi_approx = tf.transpose(tf.reduce_sum(tf.reduce_prod(splus, axis=-1), axis=1, keepdims=True))
    ehvi_approx = tf.reduce_mean(ehvi_approx, axis=-1)  # average through mc sample

    ehvi = expected_hv_improvement(model, (lb_points, ub_points))(tf.expand_dims(xs, -2))

    npt.assert_allclose(ehvi, ehvi_approx, rtol=0.01, atol=0.01)


def test_qehvi_builder_raises_for_empty_data() -> None:
    num_obj = 3
    dataset = empty_dataset([2], [num_obj])
    model = QuadraticMeanAndRBFKernel()

    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        BatchMonteCarloExpectedHypervolumeImprovement(sample_size=100).prepare_acquisition_function(
            model,
            dataset=dataset,
        )
    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        BatchMonteCarloExpectedHypervolumeImprovement(sample_size=100).prepare_acquisition_function(
            model,
        )


@pytest.mark.parametrize("sample_size", [-2, 0])
def test_batch_monte_carlo_expected_hypervolume_improvement_raises_for_invalid_sample_size(
    sample_size: int,
) -> None:
    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        BatchMonteCarloExpectedHypervolumeImprovement(sample_size)


def test_batch_monte_carlo_expected_hypervolume_improvement_raises_for_invalid_jitter() -> None:
    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        BatchMonteCarloExpectedHypervolumeImprovement(100, jitter=-1.0)


@random_seed
@pytest.mark.parametrize(
    "input_dim, num_samples_per_point, training_input, obj_num, variance_scale",
    [
        pytest.param(
            1,
            50_000,
            tf.constant([[0.3], [0.22], [0.1], [0.35]]),
            2,
            1.0,
            id="1d_input_2obj_model_var_1_q_1",
        ),
        pytest.param(
            1,
            50_000,
            tf.constant([[0.3], [0.22], [0.1], [0.35]]),
            2,
            2.0,
            id="1d_input_2obj_model_var_2_q_1",
        ),
        pytest.param(
            2,
            50_000,
            tf.constant([[0.0, 0.0], [0.2, 0.5]]),
            2,
            1.0,
            id="2d_input_2obj_model_var_1_q_1",
        ),
        pytest.param(
            3,
            25_000,
            tf.constant([[0.0, 0.0, 0.2], [-0.2, 0.5, -0.1], [0.2, -0.5, 0.2]]),
            3,
            1.0,
            id="3d_input_3obj_model_var_1_q_1",
        ),
    ],
)
def test_batch_monte_carlo_expected_hypervolume_improvement_can_reproduce_ehvi(
    input_dim: int,
    num_samples_per_point: int,
    training_input: tf.Tensor,
    obj_num: int,
    variance_scale: float,
) -> None:
    data_num_seg_per_dim = 10  # test data number per input dim

    model = _mo_test_model(obj_num, *[variance_scale] * obj_num)

    mean, _ = model.predict(training_input)  # gen prepare Pareto
    _model_based_tr_dataset = Dataset(training_input, mean)

    _model_based_pareto = Pareto(mean)
    _reference_pt = get_reference_point(_model_based_pareto.front)
    _partition_bounds = prepare_default_non_dominated_partition_bounds(
        _reference_pt, _model_based_pareto.front
    )

    qehvi_builder = BatchMonteCarloExpectedHypervolumeImprovement(sample_size=num_samples_per_point)
    qehvi_acq = qehvi_builder.prepare_acquisition_function(model, dataset=_model_based_tr_dataset)
    ehvi_acq = expected_hv_improvement(model, _partition_bounds)

    test_xs = tf.convert_to_tensor(
        list(itertools.product(*[list(tf.linspace(-1, 1, data_num_seg_per_dim))] * input_dim)),
        dtype=training_input.dtype,
    )  # [test_num, input_dim]
    test_xs = tf.expand_dims(test_xs, -2)  # add Batch dim: q=1

    npt.assert_allclose(ehvi_acq(test_xs), qehvi_acq(test_xs), rtol=1e-2, atol=1e-2)


@random_seed
@pytest.mark.parametrize(
    "test_input, obj_samples, pareto_front_obs, reference_point, expected_output",
    [
        pytest.param(
            tf.zeros(shape=(1, 2, 1)),
            tf.constant([[[-6.5, -4.5], [-7.0, -4.0]]]),
            tf.constant([[-4.0, -5.0], [-5.0, -5.0], [-8.5, -3.5], [-8.5, -3.0], [-9.0, -1.0]]),
            tf.constant([0.0, 0.0]),
            tf.constant([[1.75]]),
            id="q_2, both points contribute",
        ),
        pytest.param(
            tf.zeros(shape=(1, 2, 1)),
            tf.constant([[[-6.5, -4.5], [-6.0, -4.0]]]),
            tf.constant([[-4.0, -5.0], [-5.0, -5.0], [-8.5, -3.5], [-8.5, -3.0], [-9.0, -1.0]]),
            tf.constant([0.0, 0.0]),
            tf.constant([[1.5]]),
            id="q_2, only 1 point contributes",
        ),
        pytest.param(
            tf.zeros(shape=(1, 2, 1)),
            tf.constant([[[-2.0, -2.0], [0.0, -0.1]]]),
            tf.constant([[-4.0, -5.0], [-5.0, -5.0], [-8.5, -3.5], [-8.5, -3.0], [-9.0, -1.0]]),
            tf.constant([0.0, 0.0]),
            tf.constant([[0.0]]),
            id="q_2, neither contributes",
        ),
        pytest.param(
            tf.zeros(shape=(1, 2, 1)),
            tf.constant([[[-6.5, -4.5], [-9.0, -2.0]]]),
            tf.constant([[-4.0, -5.0], [-5.0, -5.0], [-8.5, -3.5], [-8.5, -3.0], [-9.0, -1.0]]),
            tf.constant([0.0, 0.0]),
            tf.constant([[2.0]]),
            id="obj_2_q_2, test input better than current-best first objective",
        ),
        pytest.param(
            tf.zeros(shape=(1, 2, 1)),
            tf.constant([[[-6.5, -4.5], [-6.0, -6.0]]]),
            tf.constant([[-4.0, -5.0], [-5.0, -5.0], [-8.5, -3.5], [-8.5, -3.0], [-9.0, -1.0]]),
            tf.constant([0.0, 0.0]),
            tf.constant([[8.0]]),
            id="obj_2_q_2, test input better than current best second objective",
        ),
        pytest.param(
            tf.zeros(shape=(1, 3, 1)),
            tf.constant([[[-6.5, -4.5], [-9.0, -2.0], [-7.0, -4.0]]]),
            tf.constant([[-4.0, -5.0], [-5.0, -5.0], [-8.5, -3.5], [-8.5, -3.0], [-9.0, -1.0]]),
            tf.constant([0.0, 0.0]),
            tf.constant([[2.25]]),
            id="obj_2_q_3, all points contribute",
        ),
        pytest.param(
            tf.zeros(shape=(1, 3, 1)),
            tf.constant([[[-6.5, -4.5], [-9.0, -2.0], [-7.0, -5.0]]]),
            tf.constant([[-4.0, -5.0], [-5.0, -5.0], [-8.5, -3.5], [-8.5, -3.0], [-9.0, -1.0]]),
            tf.constant([0.0, 0.0]),
            tf.constant([[3.5]]),
            id="obj_2_q_3, not all points contribute",
        ),
        pytest.param(
            tf.zeros(shape=(1, 3, 1)),
            tf.constant([[[-0.0, -4.5], [-1.0, -2.0], [-3.0, -0.0]]]),
            tf.constant([[-4.0, -5.0], [-5.0, -5.0], [-8.5, -3.5], [-8.5, -3.0], [-9.0, -1.0]]),
            tf.constant([0.0, 0.0]),
            tf.constant([[0.0]]),
            id="obj_2_q_3, none contribute",
        ),
        pytest.param(
            tf.zeros(shape=(1, 2, 1)),
            tf.constant([[[-1.0, -1.0, -1.0], [-2.0, -2.0, -2.0]]]),
            tf.constant([[-4.0, -2.0, -3.0], [-3.0, -5.0, -1.0], [-2.0, -4.0, -2.0]]),
            tf.constant([1.0, 1.0, 1.0]),
            tf.constant([[0.0]]),
            id="obj_3_q_2, none contribute",
        ),
        pytest.param(
            tf.zeros(shape=(1, 2, 1)),
            tf.constant([[[-1.0, -2.0, -6.0], [-1.0, -3.0, -4.0]]]),
            tf.constant([[-4.0, -2.0, -3.0], [-3.0, -5.0, -1.0], [-2.0, -4.0, -2.0]]),
            tf.constant([1.0, 1.0, 1.0]),
            tf.constant([[22.0]]),
            id="obj_3_q_2, all points contribute",
        ),
        pytest.param(
            tf.zeros(shape=(1, 2, 1)),
            tf.constant(
                [[[-2.0, -3.0, -7.0], [-2.0, -4.0, -5.0]], [[-1.0, -2.0, -6.0], [-1.0, -3.0, -4.0]]]
            ),
            tf.constant([[-4.0, -2.0, -3.0], [-3.0, -5.0, -1.0], [-2.0, -4.0, -2.0]]),
            tf.constant([1.0, 1.0, 1.0]),
            tf.constant([[41.0]]),
            id="obj_3_q_2, mc sample size=2",
        ),
    ],
)
def test_batch_monte_carlo_expected_hypervolume_improvement_utility_on_specified_samples(
    test_input: TensorType,
    obj_samples: TensorType,
    pareto_front_obs: TensorType,
    reference_point: TensorType,
    expected_output: TensorType,
) -> None:
    npt.assert_allclose(
        batch_ehvi(
            PseudoBatchReparametrizationSampler(obj_samples),
            sampler_jitter=DEFAULTS.JITTER,
            partition_bounds=prepare_default_non_dominated_partition_bounds(
                reference_point, Pareto(pareto_front_obs).front
            ),
        )(test_input),
        expected_output,
        rtol=1e-5,
        atol=1e-5,
    )


@pytest.mark.parametrize("sample_size", [-2, 0])
def test_batch_monte_carlo_expected_improvement_raises_for_invalid_sample_size(
    sample_size: int,
) -> None:
    with pytest.raises(tf.errors.InvalidArgumentError):
        BatchMonteCarloExpectedImprovement(sample_size)


def test_batch_monte_carlo_expected_improvement_raises_for_invalid_jitter() -> None:
    with pytest.raises(tf.errors.InvalidArgumentError):
        BatchMonteCarloExpectedImprovement(100, jitter=-1.0)


def test_batch_monte_carlo_expected_improvement_raises_for_empty_data() -> None:
    builder = BatchMonteCarloExpectedImprovement(100)
    data = Dataset(tf.zeros([0, 2]), tf.zeros([0, 1]))
    model = QuadraticMeanAndRBFKernel()
    with pytest.raises(tf.errors.InvalidArgumentError):
        builder.prepare_acquisition_function(model, dataset=data)
    with pytest.raises(tf.errors.InvalidArgumentError):
        builder.prepare_acquisition_function(model)


def test_batch_monte_carlo_expected_improvement_raises_for_model_with_wrong_event_shape() -> None:
    builder = BatchMonteCarloExpectedImprovement(100)
    data = mk_dataset([(0.0, 0.0)], [(0.0, 0.0)])
    matern52 = tfp.math.psd_kernels.MaternFiveHalves(
        amplitude=tf.cast(2.3, tf.float64), length_scale=tf.cast(0.5, tf.float64)
    )
    model = GaussianProcess([lambda x: branin(x), lambda x: quadratic(x)], [matern52, rbf()])
    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        builder.prepare_acquisition_function(model, dataset=data)


@random_seed
def test_batch_monte_carlo_expected_improvement_can_reproduce_ei() -> None:
    known_query_points = tf.random.uniform([5, 2], dtype=tf.float64)
    data = Dataset(known_query_points, quadratic(known_query_points))
    model = QuadraticMeanAndRBFKernel()
    batch_ei = BatchMonteCarloExpectedImprovement(10_000).prepare_acquisition_function(
        model, dataset=data
    )
    ei = ExpectedImprovement().prepare_acquisition_function(model, dataset=data)
    xs = tf.random.uniform([3, 5, 1, 2], dtype=tf.float64)
    npt.assert_allclose(batch_ei(xs), ei(xs), rtol=0.06)
    # and again, since the sampler uses cacheing
    npt.assert_allclose(batch_ei(xs), ei(xs), rtol=0.06)


@random_seed
def test_batch_monte_carlo_expected_improvement() -> None:
    xs = tf.random.uniform([3, 5, 7, 2], dtype=tf.float64)
    model = QuadraticMeanAndRBFKernel()

    mean, cov = model.predict_joint(xs)
    mvn = tfp.distributions.MultivariateNormalFullCovariance(tf.linalg.matrix_transpose(mean), cov)
    mvn_samples = mvn.sample(10_000)
    min_predictive_mean_at_known_points = 0.09
    # fmt: off
    expected = tf.reduce_mean(tf.reduce_max(tf.maximum(
        min_predictive_mean_at_known_points - mvn_samples, 0.0
    ), axis=-1), axis=0)
    # fmt: on

    builder = BatchMonteCarloExpectedImprovement(10_000)
    acq = builder.prepare_acquisition_function(
        model, dataset=mk_dataset([[0.3], [0.5]], [[0.09], [0.25]])
    )

    npt.assert_allclose(acq(xs), expected, rtol=0.05)


@random_seed
def test_batch_monte_carlo_expected_improvement_updates_without_retracing() -> None:
    known_query_points = tf.random.uniform([10, 2], dtype=tf.float64)
    data = Dataset(known_query_points[:5], quadratic(known_query_points[:5]))
    model = QuadraticMeanAndRBFKernel()
    builder = BatchMonteCarloExpectedImprovement(10_000)
    ei = ExpectedImprovement().prepare_acquisition_function(model, dataset=data)
    xs = tf.random.uniform([3, 5, 1, 2], dtype=tf.float64)

    batch_ei = builder.prepare_acquisition_function(model, dataset=data)
    assert batch_ei.__call__._get_tracing_count() == 0  # type: ignore
    npt.assert_allclose(batch_ei(xs), ei(xs), rtol=0.06)
    assert batch_ei.__call__._get_tracing_count() == 1  # type: ignore

    data = Dataset(known_query_points, quadratic(known_query_points))
    up_batch_ei = builder.update_acquisition_function(batch_ei, model, dataset=data)
    assert up_batch_ei == batch_ei
    assert batch_ei.__call__._get_tracing_count() == 1  # type: ignore
    npt.assert_allclose(batch_ei(xs), ei(xs), rtol=0.06)
    assert batch_ei.__call__._get_tracing_count() == 1  # type: ignore


@pytest.mark.parametrize(
    "function, function_repr",
    [
        (ExpectedImprovement(), "ExpectedImprovement()"),
        (NegativeLowerConfidenceBound(1.96), "NegativeLowerConfidenceBound(1.96)"),
        (NegativePredictiveMean(), "NegativePredictiveMean()"),
        (ProbabilityOfFeasibility(0.5), "ProbabilityOfFeasibility(0.5)"),
        (ExpectedHypervolumeImprovement(), "ExpectedHypervolumeImprovement()"),
        (
            BatchMonteCarloExpectedImprovement(10_000),
            f"BatchMonteCarloExpectedImprovement(10000, jitter={DEFAULTS.JITTER})",
        ),
        (PredictiveVariance(), f"PredictiveVariance(jitter={DEFAULTS.JITTER})"),
    ],
)
def test_single_model_acquisition_function_builder_reprs(
    function: SingleModelAcquisitionBuilder, function_repr: str
) -> None:
    assert repr(function) == function_repr
    assert repr(function.using("TAG")) == f"{function_repr} using tag 'TAG'"
    assert (
        repr(ExpectedConstrainedImprovement("TAG", function.using("TAG"), 0.0))
        == f"ExpectedConstrainedImprovement('TAG', {function_repr} using tag 'TAG', 0.0)"
    )
    assert (
        repr(ExpectedConstrainedHypervolumeImprovement("TAG", function.using("TAG"), 0.0))
        == f"ExpectedConstrainedHypervolumeImprovement('TAG', {function_repr} using tag 'TAG', 0.0)"
    )


def test_locally_penalized_expected_improvement_builder_raises_for_empty_data() -> None:
    data = Dataset(tf.zeros([0, 1]), tf.ones([0, 1]))
    space = Box([0, 0], [1, 1])
    with pytest.raises(tf.errors.InvalidArgumentError):
        LocalPenalizationAcquisitionFunction(search_space=space).prepare_acquisition_function(
            QuadraticMeanAndRBFKernel(),
            dataset=data,
        )
    with pytest.raises(tf.errors.InvalidArgumentError):
        LocalPenalizationAcquisitionFunction(search_space=space).prepare_acquisition_function(
            QuadraticMeanAndRBFKernel(),
        )


def test_locally_penalized_expected_improvement_builder_raises_for_invalid_num_samples() -> None:
    search_space = Box([0, 0], [1, 1])
    with pytest.raises(tf.errors.InvalidArgumentError):
        LocalPenalizationAcquisitionFunction(search_space, num_samples=-5)


@pytest.mark.parametrize("pending_points", [tf.constant([0.0]), tf.constant([[[0.0], [1.0]]])])
def test_locally_penalized_expected_improvement_builder_raises_for_invalid_pending_points_shape(
    pending_points: TensorType,
) -> None:
    data = Dataset(tf.zeros([3, 2], dtype=tf.float64), tf.ones([3, 2], dtype=tf.float64))
    space = Box([0, 0], [1, 1])
    builder = LocalPenalizationAcquisitionFunction(search_space=space)
    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        builder.prepare_acquisition_function(QuadraticMeanAndRBFKernel(), data, pending_points)


@random_seed
@pytest.mark.parametrize(
    "base_builder",
    [
        ExpectedImprovement(),
        MinValueEntropySearch(Box([0, 0], [1, 1]), grid_size=10000, num_samples=10),
    ],
)
def test_locally_penalized_acquisitions_match_base_acquisition(
    base_builder: ExpectedImprovement | MinValueEntropySearch,
) -> None:
    data = Dataset(tf.zeros([3, 2], dtype=tf.float64), tf.ones([3, 2], dtype=tf.float64))
    search_space = Box([0, 0], [1, 1])
    model = QuadraticMeanAndRBFKernel()

    lp_acq_builder = LocalPenalizationAcquisitionFunction(
        search_space, base_acquisition_function_builder=base_builder
    )
    lp_acq = lp_acq_builder.prepare_acquisition_function(model, data, None)

    base_acq = base_builder.prepare_acquisition_function(model, dataset=data)

    x_range = tf.linspace(0.0, 1.0, 11)
    x_range = tf.cast(x_range, dtype=tf.float64)
    xs = tf.reshape(tf.stack(tf.meshgrid(x_range, x_range, indexing="ij"), axis=-1), (-1, 2))
    lp_acq_values = lp_acq(xs[..., None, :])
    base_acq_values = base_acq(xs[..., None, :])

    if isinstance(base_builder, ExpectedImprovement):
        npt.assert_array_equal(lp_acq_values, base_acq_values)
    else:  # check sampling-based acquisition functions are close
        npt.assert_allclose(lp_acq_values, base_acq_values, atol=0.001)


@random_seed
@pytest.mark.parametrize("penalizer", [soft_local_penalizer, hard_local_penalizer])
@pytest.mark.parametrize(
    "base_builder",
    [ExpectedImprovement(), MinValueEntropySearch(Box([0, 0], [1, 1]), grid_size=5000)],
)
def test_locally_penalized_acquisitions_combine_base_and_penalization_correctly(
    penalizer: Callable[..., Union[PenalizationFunction, UpdatablePenalizationFunction]],
    base_builder: ExpectedImprovement | MinValueEntropySearch,
) -> None:
    data = Dataset(tf.zeros([3, 2], dtype=tf.float64), tf.ones([3, 2], dtype=tf.float64))
    search_space = Box([0, 0], [1, 1])
    model = QuadraticMeanAndRBFKernel()
    pending_points = tf.zeros([2, 2], dtype=tf.float64)

    acq_builder = LocalPenalizationAcquisitionFunction(
        search_space, penalizer=penalizer, base_acquisition_function_builder=base_builder
    )
    lp_acq = acq_builder.prepare_acquisition_function(model, data, None)  # initialize
    lp_acq = acq_builder.update_acquisition_function(lp_acq, model, data, pending_points[:1], False)
    up_lp_acq = acq_builder.update_acquisition_function(lp_acq, model, data, pending_points, False)
    assert up_lp_acq == lp_acq  # in-place updates

    base_acq = base_builder.prepare_acquisition_function(model, dataset=data)

    best = acq_builder._eta
    lipshitz_constant = acq_builder._lipschitz_constant
    penalizer = penalizer(model, pending_points, lipshitz_constant, best)

    x_range = tf.linspace(0.0, 1.0, 11)
    x_range = tf.cast(x_range, dtype=tf.float64)
    xs = tf.reshape(tf.stack(tf.meshgrid(x_range, x_range, indexing="ij"), axis=-1), (-1, 2))

    lp_acq_values = lp_acq(xs[..., None, :])
    base_acq_values = base_acq(xs[..., None, :])
    penal_values = penalizer(xs[..., None, :])
    penalized_base_acq = tf.math.exp(tf.math.log(base_acq_values) + tf.math.log(penal_values))

    if isinstance(base_builder, ExpectedImprovement):
        npt.assert_array_equal(lp_acq_values, penalized_base_acq)
    else:  # check sampling-based acquisition functions are close
        npt.assert_allclose(lp_acq_values, penalized_base_acq, atol=0.001)


@pytest.mark.parametrize("penalizer", [soft_local_penalizer, hard_local_penalizer])
@pytest.mark.parametrize("at", [tf.constant([[0.0], [1.0]]), tf.constant([[[0.0], [1.0]]])])
def test_lipschitz_penalizers_raises_for_invalid_batch_size(
    at: TensorType,
    penalizer: Callable[..., PenalizationFunction],
) -> None:
    pending_points = tf.zeros([1, 2], dtype=tf.float64)
    best = tf.constant([0], dtype=tf.float64)
    lipshitz_constant = tf.constant([1], dtype=tf.float64)
    lp = penalizer(QuadraticMeanAndRBFKernel(), pending_points, lipshitz_constant, best)

    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        lp(at)


@pytest.mark.parametrize("penalizer", [soft_local_penalizer, hard_local_penalizer])
@pytest.mark.parametrize("pending_points", [tf.constant([0.0]), tf.constant([[[0.0], [1.0]]])])
def test_lipschitz_penalizers_raises_for_invalid_pending_points_shape(
    pending_points: TensorType,
    penalizer: Callable[..., PenalizationFunction],
) -> None:
    best = tf.constant([0], dtype=tf.float64)
    lipshitz_constant = tf.constant([1], dtype=tf.float64)
    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        soft_local_penalizer(QuadraticMeanAndRBFKernel(), pending_points, lipshitz_constant, best)


def test_gibbon_builder_raises_for_empty_data() -> None:
    empty_data = Dataset(tf.zeros([0, 2], dtype=tf.float64), tf.ones([0, 2], dtype=tf.float64))
    non_empty_data = Dataset(tf.zeros([3, 2], dtype=tf.float64), tf.ones([3, 2], dtype=tf.float64))
    search_space = Box([0, 0], [1, 1])
    builder = GIBBON(search_space)
    with pytest.raises(tf.errors.InvalidArgumentError):
        builder.prepare_acquisition_function(QuadraticMeanAndRBFKernel(), empty_data)
    with pytest.raises(tf.errors.InvalidArgumentError):
        builder.prepare_acquisition_function(QuadraticMeanAndRBFKernel())
    acq = builder.prepare_acquisition_function(QuadraticMeanAndRBFKernel(), non_empty_data)
    with pytest.raises(tf.errors.InvalidArgumentError):
        builder.update_acquisition_function(acq, QuadraticMeanAndRBFKernel(), empty_data)
    with pytest.raises(tf.errors.InvalidArgumentError):
        builder.update_acquisition_function(acq, QuadraticMeanAndRBFKernel())


@pytest.mark.parametrize("param", [-2, 0])
def test_gibbon_builder_raises_for_invalid_init_params(param: int) -> None:
    search_space = Box([0, 0], [1, 1])
    with pytest.raises(tf.errors.InvalidArgumentError):
        GIBBON(search_space, num_samples=param)
    with pytest.raises(tf.errors.InvalidArgumentError):
        GIBBON(search_space, grid_size=param)
    with pytest.raises(tf.errors.InvalidArgumentError):
        GIBBON(search_space, num_fourier_features=param)


def test_gibbon_builder_raises_when_given_num_features_and_gumbel() -> None:
    # cannot do feature-based approx of Gumbel sampler
    search_space = Box([0, 0], [1, 1])
    with pytest.raises(tf.errors.InvalidArgumentError):
        GIBBON(search_space, use_thompson=False, num_fourier_features=10)


@pytest.mark.parametrize("samples", [tf.constant([]), tf.constant([[[]]])])
def test_gibbon_quality_term_raises_for_gumbel_samples_with_invalid_shape(
    samples: TensorType,
) -> None:
    with pytest.raises(ValueError):
        model = QuadraticMeanAndRBFKernel()
        gibbon_quality_term(model, samples)


@pytest.mark.parametrize("at", [tf.constant([[0.0], [1.0]]), tf.constant([[[0.0], [1.0]]])])
def test_gibbon_quality_term_raises_for_invalid_batch_size(at: TensorType) -> None:
    model = QuadraticMeanAndRBFKernel()
    gibbon_acq = gibbon_quality_term(model, tf.constant([[1.0], [2.0]]))

    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        gibbon_acq(at)


def test_gibbon_quality_term_returns_correct_shape() -> None:
    model = QuadraticMeanAndRBFKernel()
    gumbel_samples = tf.constant([[1.0], [2.0]])
    query_at = tf.linspace([[-10.0]], [[10.0]], 5)
    evals = gibbon_quality_term(model, gumbel_samples)(query_at)
    npt.assert_array_equal(evals.shape, tf.constant([5, 1]))


@unittest.mock.patch("trieste.acquisition.function.gibbon_quality_term")
@pytest.mark.parametrize("use_thompson", [True, False])
def test_gibbon_builder_builds_min_value_samples(
    mocked_mves: MagicMock, use_thompson: bool
) -> None:
    dataset = Dataset(tf.zeros([3, 2], dtype=tf.float64), tf.ones([3, 2], dtype=tf.float64))
    search_space = Box([0, 0], [1, 1])
    builder = GIBBON(search_space, use_thompson=use_thompson)
    model = QuadraticMeanAndRBFKernel()
    builder.prepare_acquisition_function(model, dataset=dataset)
    mocked_mves.assert_called_once()

    # check that the Gumbel samples look sensible
    min_value_samples = builder._min_value_samples
    query_points = builder._search_space.sample(num_samples=builder._grid_size)
    query_points = tf.concat([dataset.query_points, query_points], 0)
    fmean, _ = model.predict(query_points)
    assert max(min_value_samples) < min(fmean)  # type: ignore


@pytest.mark.parametrize("use_thompson", [True, False])
def test_gibbon_builder_updates_acquisition_function(use_thompson: bool) -> None:

    search_space = Box([0.0, 0.0], [1.0, 1.0])
    x_range = tf.cast(tf.linspace(0.0, 1.0, 5), dtype=tf.float64)
    xs = tf.reshape(tf.stack(tf.meshgrid(x_range, x_range, indexing="ij"), axis=-1), (-1, 2))
    ys = quadratic(xs)
    partial_dataset = Dataset(xs[:10], ys[:10])
    full_dataset = Dataset(xs, ys)

    builder = GIBBON(search_space, use_thompson=use_thompson)
    xs = tf.cast(tf.linspace([[0.0]], [[1.0]], 10), tf.float64)
    model = QuadraticMeanAndRBFKernel()

    old_acq_fn = builder.prepare_acquisition_function(model, dataset=partial_dataset)
    tf.random.set_seed(0)  # to ensure consistent sampling
    updated_acq_fn = builder.update_acquisition_function(old_acq_fn, model, dataset=full_dataset)
    assert updated_acq_fn == old_acq_fn
    updated_values = updated_acq_fn(xs)

    tf.random.set_seed(0)  # to ensure consistent sampling
    new_acq_fn = builder.prepare_acquisition_function(model, dataset=full_dataset)
    new_values = new_acq_fn(xs)

    npt.assert_allclose(updated_values, new_values)


@pytest.mark.parametrize("pending_points", [tf.constant([0.0]), tf.constant([[[0.0], [1.0]]])])
def test_gibbon_builder_raises_for_invalid_pending_points_shape(
    pending_points: TensorType,
) -> None:
    data = Dataset(tf.zeros([3, 2], dtype=tf.float64), tf.ones([3, 2], dtype=tf.float64))
    space = Box([0, 0], [1, 1])
    builder = GIBBON(search_space=space)
    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        builder.prepare_acquisition_function(QuadraticMeanAndRBFKernel(), data, pending_points)


def test_gibbon_raises_for_model_without_homoscedastic_likelihood() -> None:
    class dummy_model_without_likelihood(ProbabilisticModel):
        def predict(self, query_points: TensorType) -> tuple[None, None]:
            return None, None

        def predict_joint(self, query_points: TensorType) -> tuple[None, None]:
            return None, None

        def sample(self, query_points: TensorType, num_samples: int) -> None:
            return None

        def covariance_between_points(
            self, query_points_1: TensorType, query_points_2: TensorType
        ) -> None:
            return None

    with pytest.raises(ValueError):
        model_without_likelihood = dummy_model_without_likelihood()
        gibbon_quality_term(model_without_likelihood, tf.constant([[1.0]]))


def test_gibbon_raises_for_model_without_covariance_between_points_method() -> None:
    class dummy_model_without_covariance_between_points(ProbabilisticModel):
        def predict(self, query_points: TensorType) -> tuple[None, None]:
            return None, None

        def predict_joint(self, query_points: TensorType) -> tuple[None, None]:
            return None, None

        def sample(self, query_points: TensorType, num_samples: int) -> None:
            return None

        def get_observation_noise(self) -> None:
            return None

    with pytest.raises(AttributeError):
        model_without_likelihood = dummy_model_without_covariance_between_points()
        gibbon_quality_term(model_without_likelihood, tf.constant([[1.0]]))


@random_seed
@unittest.mock.patch("trieste.acquisition.function.gibbon_quality_term")
def test_gibbon_builder_builds_min_value_samples_rff(mocked_mves: MagicMock) -> None:
    search_space = Box([0.0, 0.0], [1.0, 1.0])
    model = QuadraticMeanAndRBFKernel(noise_variance=tf.constant(1e-10, dtype=tf.float64))
    model.kernel = (
        gpflow.kernels.RBF()
    )  # need a gpflow kernel object for random feature decompositions

    x_range = tf.linspace(0.0, 1.0, 5)
    x_range = tf.cast(x_range, dtype=tf.float64)
    xs = tf.reshape(tf.stack(tf.meshgrid(x_range, x_range, indexing="ij"), axis=-1), (-1, 2))
    ys = quadratic(xs)
    dataset = Dataset(xs, ys)

    builder = GIBBON(search_space, use_thompson=True, num_fourier_features=100)
    builder.prepare_acquisition_function(model, dataset=dataset)
    mocked_mves.assert_called_once()

    # check that the Gumbel samples look sensible
    min_value_samples = mocked_mves.call_args[0][1]
    query_points = builder._search_space.sample(num_samples=builder._grid_size)
    query_points = tf.concat([dataset.query_points, query_points], 0)
    fmean, _ = model.predict(query_points)
    assert max(min_value_samples) < min(fmean) + 1e-4


def test_gibbon_chooses_same_as_min_value_entropy_search() -> None:
    """
    When based on a single max-value sample, GIBBON should choose the same point as
    MES (see :cite:`Moss:2021`).
    """
    model = QuadraticMeanAndRBFKernel(noise_variance=tf.constant(1e-10, dtype=tf.float64))

    x_range = tf.linspace(-1.0, 1.0, 11)
    x_range = tf.cast(x_range, dtype=tf.float64)
    xs = tf.reshape(tf.stack(tf.meshgrid(x_range, x_range, indexing="ij"), axis=-1), (-1, 2))

    min_value_sample = tf.constant([[1.0]], dtype=tf.float64)
    mes_evals = min_value_entropy_search(model, min_value_sample)(xs[..., None, :])
    gibbon_evals = gibbon_quality_term(model, min_value_sample)(xs[..., None, :])

    npt.assert_array_equal(tf.argmax(mes_evals), tf.argmax(gibbon_evals))


@pytest.mark.parametrize("rescaled_repulsion", [True, False])
@pytest.mark.parametrize("noise_variance", [0.1, 1e-10])
def test_batch_gibbon_is_sum_of_individual_gibbons_and_repulsion_term(
    rescaled_repulsion: bool, noise_variance: float
) -> None:
    """
    Check that batch GIBBON can be decomposed into the sum of sequential GIBBONs and a repulsion
    term (see :cite:`Moss:2021`).
    """
    noise_variance = tf.constant(noise_variance, dtype=tf.float64)
    model = QuadraticMeanAndRBFKernel(noise_variance=noise_variance)
    model.kernel = (
        gpflow.kernels.RBF()
    )  # need a gpflow kernel object for random feature decomposition

    x_range = tf.linspace(0.0, 1.0, 4)
    x_range = tf.cast(x_range, dtype=tf.float64)
    xs = tf.reshape(tf.stack(tf.meshgrid(x_range, x_range, indexing="ij"), axis=-1), (-1, 2))

    pending_points = tf.constant([[0.11, 0.51], [0.21, 0.31], [0.41, 0.91]], dtype=tf.float64)
    min_value_sample = tf.constant([[-0.1, 0.1]], dtype=tf.float64)

    gibbon_of_new_points = gibbon_quality_term(model, min_value_sample)(xs[..., None, :])
    mean, var = model.predict(xs)
    _, pending_var = model.predict_joint(pending_points)
    pending_var += noise_variance * tf.eye(len(pending_points), dtype=pending_var.dtype)

    calculated_batch_gibbon = gibbon_of_new_points + gibbon_repulsion_term(
        model, pending_points, rescaled_repulsion=rescaled_repulsion
    )(xs[..., None, :])

    for i in tf.range(len(xs)):  # check across a set of candidate points
        candidate_and_pending = tf.concat([xs[i : i + 1], pending_points], axis=0)
        _, A = model.predict_joint(candidate_and_pending)
        A += noise_variance * tf.eye(len(pending_points) + 1, dtype=A.dtype)
        repulsion = tf.linalg.logdet(A) - tf.math.log(A[0, 0, 0]) - tf.linalg.logdet(pending_var)
        if rescaled_repulsion:  # down-weight repulsion term
            batch_size, search_space_dim = tf.cast(tf.shape(pending_points), dtype=mean.dtype)
            repulsion = repulsion * ((1 / batch_size) ** (2))

        reconstructed_batch_gibbon = 0.5 * repulsion + gibbon_of_new_points[i : i + 1]
        npt.assert_array_almost_equal(
            calculated_batch_gibbon[i : i + 1], reconstructed_batch_gibbon
        )


@pytest.mark.parametrize("at", [tf.constant([[0.0], [1.0]]), tf.constant([[[0.0], [1.0]]])])
def test_expected_constrained_hypervolume_improvement_raises_for_invalid_batch_size(
    at: TensorType,
) -> None:
    pof = ProbabilityOfFeasibility(0.0).using("")
    builder = ExpectedConstrainedHypervolumeImprovement("", pof, tf.constant(0.5))
    initial_query_points = tf.constant([[-1.0]])
    initial_objective_function_values = tf.constant([[1.0, 1.0]])
    data = {"": Dataset(initial_query_points, initial_objective_function_values)}

    echvi = builder.prepare_acquisition_function({"": QuadraticMeanAndRBFKernel()}, datasets=data)

    with pytest.raises(TF_DEBUGGING_ERROR_TYPES):
        echvi(at)


def test_expected_constrained_hypervolume_improvement_can_reproduce_ehvi() -> None:
    num_obj = 2
    train_x = tf.constant([[-2.0], [-1.5], [-1.0], [0.0], [0.5], [1.0], [1.5], [2.0]])

    obj_model = _mo_test_model(num_obj, *[None] * num_obj)
    model_pred_observation = obj_model.predict(train_x)[0]

    class _Certainty(AcquisitionFunctionBuilder):
        def prepare_acquisition_function(
            self,
            models: Mapping[str, ProbabilisticModel],
            datasets: Optional[Mapping[str, Dataset]] = None,
        ) -> AcquisitionFunction:
            return lambda x: tf.ones_like(tf.squeeze(x, -2))

    data = {"foo": Dataset(train_x[:5], model_pred_observation[:5])}
    models_ = {"foo": obj_model}

    builder = ExpectedConstrainedHypervolumeImprovement("foo", _Certainty(), 0)
    echvi = builder.prepare_acquisition_function(models_, datasets=data)

    ehvi = (
        ExpectedHypervolumeImprovement()
        .using("foo")
        .prepare_acquisition_function(models_, datasets=data)
    )

    at = tf.constant([[[-0.1]], [[1.23]], [[-6.78]]])
    npt.assert_allclose(echvi(at), ehvi(at))

    new_data = {"foo": Dataset(train_x, model_pred_observation)}
    up_echvi = builder.update_acquisition_function(echvi, models_, datasets=new_data)
    assert up_echvi == echvi
    up_ehvi = (
        ExpectedHypervolumeImprovement()
        .using("foo")
        .prepare_acquisition_function(models_, datasets=new_data)
    )

    npt.assert_allclose(up_echvi(at), up_ehvi(at))
    assert up_echvi._get_tracing_count() == 1  # type: ignore


def test_echvi_is_constraint_when_no_feasible_points() -> None:
    class _Constraint(AcquisitionFunctionBuilder):
        def prepare_acquisition_function(
            self,
            models: Mapping[str, ProbabilisticModel],
            datasets: Optional[Mapping[str, Dataset]] = None,
        ) -> AcquisitionFunction:
            def acquisition(x: TensorType) -> TensorType:
                x_ = tf.squeeze(x, -2)
                return tf.cast(tf.logical_and(0.0 <= x_, x_ < 1.0), x.dtype)

            return acquisition

    data = {"foo": Dataset(tf.constant([[-2.0], [1.0]]), tf.constant([[4.0], [1.0]]))}
    models_ = {"foo": QuadraticMeanAndRBFKernel()}
    echvi = ExpectedConstrainedHypervolumeImprovement(
        "foo", _Constraint()
    ).prepare_acquisition_function(models_, datasets=data)

    constraint_fn = _Constraint().prepare_acquisition_function(models_, datasets=data)

    xs = tf.linspace([[-10.0]], [[10.0]], 100)
    npt.assert_allclose(echvi(xs), constraint_fn(xs))


def test_predictive_variance_builder_builds_predictive_variance() -> None:
    model = QuadraticMeanAndRBFKernel()
    acq_fn = PredictiveVariance().prepare_acquisition_function(model)
    query_at = tf.linspace([[-10]], [[10]], 100)
    _, covariance = model.predict_joint(query_at)
    expected = tf.linalg.det(covariance)
    npt.assert_array_almost_equal(acq_fn(query_at), expected)


@pytest.mark.parametrize(
    "at, acquisition_shape",
    [
        (tf.constant([[[1.0]]]), tf.constant([1, 1])),
        (tf.linspace([[-10.0]], [[10.0]], 5), tf.constant([5, 1])),
        (tf.constant([[[1.0, 1.0]]]), tf.constant([1, 1])),
        (tf.linspace([[-10.0, -10.0]], [[10.0, 10.0]], 5), tf.constant([5, 1])),
    ],
)
def test_predictive_variance_returns_correct_shape(
    at: TensorType, acquisition_shape: TensorType
) -> None:
    model = QuadraticMeanAndRBFKernel()
    acq_fn = PredictiveVariance().prepare_acquisition_function(model)
    npt.assert_array_equal(acq_fn(at).shape, acquisition_shape)


@random_seed
@pytest.mark.parametrize(
    "variance_scale, num_samples_per_point, rtol, atol",
    [
        (0.1, 10_000, 0.05, 1e-6),
        (1.0, 50_000, 0.05, 1e-3),
        (10.0, 100_000, 0.05, 1e-2),
        (100.0, 150_000, 0.05, 1e-1),
    ],
)
def test_predictive_variance(
    variance_scale: float,
    num_samples_per_point: int,
    rtol: float,
    atol: float,
) -> None:
    variance_scale = tf.constant(variance_scale, tf.float64)

    x_range = tf.linspace(0.0, 1.0, 11)
    x_range = tf.cast(x_range, dtype=tf.float64)
    xs = tf.reshape(tf.stack(tf.meshgrid(x_range, x_range, indexing="ij"), axis=-1), (-1, 2))

    kernel = tfp.math.psd_kernels.MaternFiveHalves(variance_scale, length_scale=0.25)
    model = GaussianProcess([branin], [kernel])

    mean, variance = model.predict(xs)
    samples = tfp.distributions.Normal(mean, tf.sqrt(variance)).sample(num_samples_per_point)
    predvar_approx = tf.math.reduce_variance(samples, axis=0)

    detcov = predictive_variance(model, DEFAULTS.JITTER)
    predvar = detcov(xs[..., None, :])

    npt.assert_allclose(predvar, predvar_approx, rtol=rtol, atol=atol)


def test_predictive_variance_builder_updates_without_retracing() -> None:
    model = QuadraticMeanAndRBFKernel()
    builder = PredictiveVariance()
    acq_fn = builder.prepare_acquisition_function(model)
    assert acq_fn._get_tracing_count() == 0  # type: ignore
    query_at = tf.linspace([[-10]], [[10]], 100)
    expected = predictive_variance(model, DEFAULTS.JITTER)(query_at)
    npt.assert_array_almost_equal(acq_fn(query_at), expected)
    assert acq_fn._get_tracing_count() == 1  # type: ignore

    up_acq_fn = builder.update_acquisition_function(acq_fn, model)
    assert up_acq_fn == acq_fn
    npt.assert_array_almost_equal(acq_fn(query_at), expected)
    assert acq_fn._get_tracing_count() == 1  # type: ignore


def test_predictive_variance_raises_for_void_predict_joint() -> None:
    model, _ = trieste_deep_gaussian_process(tf.zeros([0, 1]), 2, 20, 0.01, 100, 100)
    acq_fn = predictive_variance(model, DEFAULTS.JITTER)

    with pytest.raises(ValueError):
        acq_fn(tf.zeros([0, 1]))
