# This code is part of Qiskit.
#
# (C) Copyright IBM 2018, 2020.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""The Maximum Likelihood Amplitude Estimation algorithm."""

from typing import Optional, List, Union, Tuple, Dict, Callable
import numpy as np
from scipy.optimize import brute
from scipy.stats import norm, chi2

from qiskit.providers import BaseBackend
from qiskit.providers import Backend
from qiskit import ClassicalRegister, QuantumRegister, QuantumCircuit
from qiskit.utils import QuantumInstance

from .amplitude_estimator import AmplitudeEstimator, AmplitudeEstimatorResult
from .estimation_problem import EstimationProblem
from ..exceptions import AlgorithmError

MINIMIZER = Callable[[Callable[[float], float], List[Tuple[float, float]]], float]


class MaximumLikelihoodAmplitudeEstimation(AmplitudeEstimator):
    """The Maximum Likelihood Amplitude Estimation algorithm.

    This class implements the quantum amplitude estimation (QAE) algorithm without phase
    estimation, as introduced in [1]. In comparison to the original QAE algorithm [2],
    this implementation relies solely on different powers of the Grover operator and does not
    require additional evaluation qubits.
    Finally, the estimate is determined via a maximum likelihood estimation, which is why this
    class in named ``MaximumLikelihoodAmplitudeEstimation``.

    References:
        [1]: Suzuki, Y., Uno, S., Raymond, R., Tanaka, T., Onodera, T., & Yamamoto, N. (2019).
             Amplitude Estimation without Phase Estimation.
             `arXiv:1904.10246 <https://arxiv.org/abs/1904.10246>`_.
        [2]: Brassard, G., Hoyer, P., Mosca, M., & Tapp, A. (2000).
             Quantum Amplitude Amplification and Estimation.
             `arXiv:quant-ph/0005055 <http://arxiv.org/abs/quant-ph/0005055>`_.
    """

    def __init__(
        self,
        evaluation_schedule: Union[List[int], int],
        minimizer: Optional[MINIMIZER] = None,
        quantum_instance: Optional[Union[QuantumInstance, BaseBackend, Backend]] = None,
        run_circuits_as_one_job: bool = True,
    ) -> None:
        r"""
        Args:
            evaluation_schedule: If a list, the powers applied to the Grover operator. The list
                element must be non-negative. If a non-negative integer, an exponential schedule is
                used where the highest power is 2 to the integer minus 1:
                `[id, Q^2^0, ..., Q^2^(evaluation_schedule-1)]`.
            minimizer: A minimizer used to find the minimum of the likelihood function.
                Defaults to a brute search where the number of evaluation points is determined
                according to ``evaluation_schedule``. The minimizer takes a function as first
                argument and a list of (float, float) tuples (as bounds) as second argument and
                returns a single float which is the found minimum.
            quantum_instance: Quantum Instance or Backend
            run_circuits_as_one_job: If set to True, the necessary circuits will be submitted as
                one job. Otherwise, each circuit will be run separately. This is useful for
                backends that don't support multi-circuit experiments.

        Raises:
            ValueError: If the number of oracle circuits is smaller than 1.
        """

        super().__init__()

        # set quantum instance
        self.quantum_instance = quantum_instance

        # get parameters
        if isinstance(evaluation_schedule, int):
            if evaluation_schedule < 0:
                raise ValueError("The evaluation schedule cannot be < 0.")

            self._evaluation_schedule = [0] + [2 ** j for j in range(evaluation_schedule)]
        else:
            if any(value < 0 for value in evaluation_schedule):
                raise ValueError("The elements of the evaluation schedule cannot be < 0.")

            self._evaluation_schedule = evaluation_schedule

        if minimizer is None:
            # default number of evaluations is max(10^4, pi/2 * 10^3 * 2^(m))
            nevals = max(10000, int(np.pi / 2 * 1000 * 2 * self._evaluation_schedule[-1]))

            def default_minimizer(objective_fn, bounds):
                return brute(objective_fn, bounds, Ns=nevals)[0]

            self._minimizer = default_minimizer
        else:
            self._minimizer = minimizer

        self._run_circuits_as_one_job = run_circuits_as_one_job

    @property
    def quantum_instance(self) -> Optional[QuantumInstance]:
        """Get the quantum instance.

        Returns:
            The quantum instance used to run this algorithm.
        """
        return self._quantum_instance

    @quantum_instance.setter
    def quantum_instance(
        self, quantum_instance: Union[QuantumInstance, BaseBackend, Backend]
    ) -> None:
        """Set quantum instance.

        Args:
            quantum_instance: The quantum instance used to run this algorithm.
        """
        if isinstance(quantum_instance, (BaseBackend, Backend)):
            quantum_instance = QuantumInstance(quantum_instance)
        self._quantum_instance = quantum_instance

    def construct_circuits(
        self, estimation_problem: EstimationProblem, measurement: bool = False
    ) -> List[QuantumCircuit]:
        """Construct the Amplitude Estimation w/o QPE quantum circuits.

        Args:
            estimation_problem: The estimation problem for which to construct the QAE circuit.
            measurement: Boolean flag to indicate if measurement should be included in the circuits.

        Returns:
            A list with the QuantumCircuit objects for the algorithm.
        """
        # keep track of the Q-oracle queries
        circuits = []

        num_qubits = max(
            estimation_problem.state_preparation.num_qubits,
            estimation_problem.grover_operator.num_qubits,
        )
        q = QuantumRegister(num_qubits, "q")
        qc_0 = QuantumCircuit(q, name="qc_a")  # 0 applications of Q, only a single A operator

        # add classical register if needed
        if measurement:
            c = ClassicalRegister(len(estimation_problem.objective_qubits))
            qc_0.add_register(c)

        qc_0.compose(estimation_problem.state_preparation, inplace=True)

        for k in self._evaluation_schedule:
            qc_k = qc_0.copy(name="qc_a_q_%s" % k)

            if k != 0:
                qc_k.compose(estimation_problem.grover_operator.power(k), inplace=True)

            if measurement:
                # real hardware can currently not handle operations after measurements,
                # which might happen if the circuit gets transpiled, hence we're adding
                # a safeguard-barrier
                qc_k.barrier()
                qc_k.measure(estimation_problem.objective_qubits, c[:])

            circuits += [qc_k]

        return circuits

    @staticmethod
    def compute_confidence_interval(
        result: "MaximumLikelihoodAmplitudeEstimationResult",
        alpha: float,
        kind: str = "fisher",
        apply_post_processing: bool = False,
    ) -> Tuple[float, float]:
        """Compute the `alpha` confidence interval using the method `kind`.

        The confidence level is (1 - `alpha`) and supported kinds are 'fisher',
        'likelihood_ratio' and 'observed_fisher' with shorthand
        notations 'fi', 'lr' and 'oi', respectively.

        Args:
            result: A maximum likelihood amplitude estimation result.
            alpha: The confidence level.
            kind: The method to compute the confidence interval. Defaults to 'fisher', which
                computes the theoretical Fisher information.
            apply_post_processing: If True, apply post-processing to the confidence interval.

        Returns:
            The specified confidence interval.

        Raises:
            AlgorithmError: If `run()` hasn't been called yet.
            NotImplementedError: If the method `kind` is not supported.
        """
        interval = None

        # if statevector simulator the estimate is exact
        if all(isinstance(data, (list, np.ndarray)) for data in result.circuit_results):
            interval = 2 * [result.estimation]

        elif kind in ["likelihood_ratio", "lr"]:
            interval = _likelihood_ratio_confint(result, alpha)

        elif kind in ["fisher", "fi"]:
            interval = _fisher_confint(result, alpha, observed=False)

        elif kind in ["observed_fisher", "observed_information", "oi"]:
            interval = _fisher_confint(result, alpha, observed=True)

        if interval is None:
            raise NotImplementedError(f"CI `{kind}` is not implemented.")

        if apply_post_processing:
            return tuple(result.post_processing(value) for value in interval)

        return interval

    def compute_mle(
        self,
        circuit_results: Union[List[Dict[str, int]], List[np.ndarray]],
        estimation_problem: EstimationProblem,
        num_state_qubits: Optional[int] = None,
        return_counts: bool = False,
    ) -> Union[float, Tuple[float, List[float]]]:
        """Compute the MLE via a grid-search.

        This is a stable approach if sufficient gridpoints are used.

        Args:
            circuit_results: A list of circuit outcomes. Can be counts or statevectors.
            estimation_problem: The estimation problem containing the evaluation schedule and the
                number of likelihood function evaluations used to find the minimum.
            num_state_qubits: The number of state qubits, required for statevector simulations.
            return_counts: If True, returns the good counts.

        Returns:
            The MLE for the provided result object.
        """
        good_counts, all_counts = _get_counts(circuit_results, estimation_problem, num_state_qubits)

        # search range
        eps = 1e-15  # to avoid invalid value in log
        search_range = [0 + eps, np.pi / 2 - eps]

        def loglikelihood(theta):
            # loglik contains the first `it` terms of the full loglikelihood
            loglik = 0
            for i, k in enumerate(self._evaluation_schedule):
                angle = (2 * k + 1) * theta
                loglik += np.log(np.sin(angle) ** 2) * good_counts[i]
                loglik += np.log(np.cos(angle) ** 2) * (all_counts[i] - good_counts[i])
            return -loglik

        est_theta = self._minimizer(loglikelihood, [search_range])

        if return_counts:
            return est_theta, good_counts
        return est_theta

    def estimate(
        self, estimation_problem: EstimationProblem
    ) -> "MaximumLikelihoodAmplitudeEstimationResult":
        if estimation_problem.state_preparation is None:
            raise AlgorithmError(
                "Either the state_preparation variable or the a_factory "
                "(deprecated) must be set to run the algorithm."
            )

        result = MaximumLikelihoodAmplitudeEstimationResult()
        result.evaluation_schedule = self._evaluation_schedule
        result.minimizer = self._minimizer
        result.post_processing = estimation_problem.post_processing

        if self._quantum_instance.is_statevector:
            # run circuit on statevector simulator
            circuits = self.construct_circuits(estimation_problem, measurement=False)
            ret = self._quantum_instance.execute(circuits)

            # get statevectors and construct MLE input
            statevectors = [np.asarray(ret.get_statevector(circuit)) for circuit in circuits]
            result.circuit_results = statevectors

            # to count the number of Q-oracle calls (don't count shots)
            result.shots = 1

        else:
            # construct circuits
            circuits = self.construct_circuits(estimation_problem, measurement=True)

            # run circuit on QASM simulator, get counts, and construct MLE input
            if self._run_circuits_as_one_job:
                ret = self._quantum_instance.execute(circuits)
                result.circuit_results = [ret.get_counts(circuit) for circuit in circuits]
            else:
                rets = [self._quantum_instance.execute(circuit) for circuit in circuits]
                result.circuit_results = [
                    ret.get_counts(circuit) for ret, circuit in zip(rets, circuits)
                ]

            # to count the number of Q-oracle calls
            result.shots = self._quantum_instance._run_config.shots

        # run maximum likelihood estimation
        num_state_qubits = circuits[0].num_qubits - circuits[0].num_ancillas
        theta, good_counts = self.compute_mle(
            result.circuit_results, estimation_problem, num_state_qubits, True
        )

        # store results
        result.theta = theta
        result.good_counts = good_counts
        result.estimation = np.sin(result.theta) ** 2

        # not sure why pylint complains, this is a callable and the tests pass
        # pylint: disable=not-callable
        result.estimation_processed = result.post_processing(result.estimation)

        result.fisher_information = _compute_fisher_information(result)
        result.num_oracle_queries = result.shots * sum(k for k in result.evaluation_schedule)

        # compute and store confidence interval
        confidence_interval = self.compute_confidence_interval(result, alpha=0.05, kind="fisher")
        result.confidence_interval = confidence_interval
        result.confidence_interval_processed = tuple(
            estimation_problem.post_processing(value) for value in confidence_interval
        )

        return result


class MaximumLikelihoodAmplitudeEstimationResult(AmplitudeEstimatorResult):
    """The ``MaximumLikelihoodAmplitudeEstimation`` result object."""

    def __init__(self) -> None:
        super().__init__()
        self._theta = None
        self._minimizer = None
        self._good_counts = None
        self._evaluation_schedule = None
        self._fisher_information = None

    @property
    def theta(self) -> float:
        r"""Return the estimate for the angle :math:`\theta`."""
        return self._theta

    @theta.setter
    def theta(self, value: float) -> None:
        r"""Set the estimate for the angle :math:`\theta`."""
        self._theta = value

    @property
    def minimizer(self) -> callable:
        """Return the minimizer used for the search of the likelihood function."""
        return self._minimizer

    @minimizer.setter
    def minimizer(self, value: callable) -> None:
        """Set the number minimizer used for the search of the likelihood function."""
        self._minimizer = value

    @property
    def good_counts(self) -> List[float]:
        """Return the percentage of good counts per circuit power."""
        return self._good_counts

    @good_counts.setter
    def good_counts(self, counts: List[float]) -> None:
        """Set the percentage of good counts per circuit power."""
        self._good_counts = counts

    @property
    def evaluation_schedule(self) -> List[int]:
        """Return the evaluation schedule for the powers of the Grover operator."""
        return self._evaluation_schedule

    @evaluation_schedule.setter
    def evaluation_schedule(self, evaluation_schedule: List[int]) -> None:
        """Set the evaluation schedule for the powers of the Grover operator."""
        self._evaluation_schedule = evaluation_schedule

    @property
    def fisher_information(self) -> float:
        """Return the Fisher information for the estimated amplitude."""
        return self._fisher_information

    @fisher_information.setter
    def fisher_information(self, value: float) -> None:
        """Set the Fisher information for the estimated amplitude."""
        self._fisher_information = value


def _safe_min(array, default=0):
    if len(array) == 0:
        return default
    return np.min(array)


def _safe_max(array, default=(np.pi / 2)):
    if len(array) == 0:
        return default
    return np.max(array)


def _compute_fisher_information(
    result: "MaximumLikelihoodAmplitudeEstimationResult",
    num_sum_terms: Optional[int] = None,
    observed: bool = False,
) -> float:
    """Compute the Fisher information.

    Args:
        result: A maximum likelihood amplitude estimation result.
        num_sum_terms: The number of sum terms to be included in the calculation of the
            Fisher information. By default all values are included.
        observed: If True, compute the observed Fisher information, otherwise the theoretical
            one.

    Returns:
        The computed Fisher information, or np.inf if statevector simulation was used.

    Raises:
        KeyError: Call run() first!
    """
    a = result.estimation

    # Corresponding angle to the value a (only use real part of 'a')
    theta_a = np.arcsin(np.sqrt(np.real(a)))

    # Get the number of hits (shots_k) and one-hits (h_k)
    one_hits = result.good_counts
    all_hits = [result.shots] * len(one_hits)

    # Include all sum terms or just up to a certain term?
    evaluation_schedule = result.evaluation_schedule
    if num_sum_terms is not None:
        evaluation_schedule = evaluation_schedule[:num_sum_terms]
        # not necessary since zip goes as far as shortest list:
        # all_hits = all_hits[:num_sum_terms]
        # one_hits = one_hits[:num_sum_terms]

    # Compute the Fisher information
    fisher_information = None
    if observed:
        # Note, that the observed Fisher information is very unreliable in this algorithm!
        d_loglik = 0
        for shots_k, h_k, m_k in zip(all_hits, one_hits, evaluation_schedule):
            tan = np.tan((2 * m_k + 1) * theta_a)
            d_loglik += (2 * m_k + 1) * (h_k / tan + (shots_k - h_k) * tan)

        d_loglik /= np.sqrt(a * (1 - a))
        fisher_information = d_loglik ** 2 / len(all_hits)

    else:
        fisher_information = sum(
            shots_k * (2 * m_k + 1) ** 2 for shots_k, m_k in zip(all_hits, evaluation_schedule)
        )
        fisher_information /= a * (1 - a)

    return fisher_information


def _fisher_confint(
    result: MaximumLikelihoodAmplitudeEstimationResult, alpha: float = 0.05, observed: bool = False
) -> Tuple[float, float]:
    """Compute the `alpha` confidence interval based on the Fisher information.

    Args:
        result: A maximum likelihood amplitude estimation results object.
        alpha: The level of the confidence interval (must be <= 0.5), default to 0.05.
        observed: If True, use observed Fisher information.

    Returns:
        float: The alpha confidence interval based on the Fisher information
    Raises:
        AssertionError: Call run() first!
    """
    # Get the (observed) Fisher information
    fisher_information = None
    try:
        fisher_information = result.fisher_information
    except KeyError as ex:
        raise AssertionError("Call run() first!") from ex

    if observed:
        fisher_information = _compute_fisher_information(result, observed=True)

    normal_quantile = norm.ppf(1 - alpha / 2)
    confint = np.real(result.estimation) + normal_quantile / np.sqrt(fisher_information) * np.array(
        [-1, 1]
    )
    mapped_confint = tuple(result.post_processing(bound) for bound in confint)
    return mapped_confint


def _likelihood_ratio_confint(
    result: MaximumLikelihoodAmplitudeEstimationResult,
    alpha: float = 0.05,
    nevals: Optional[int] = None,
) -> List[float]:
    """Compute the likelihood-ratio confidence interval.

    Args:
        result: A maximum likelihood amplitude estimation results object.
        alpha: The level of the confidence interval (< 0.5), defaults to 0.05.
        nevals: The number of evaluations to find the intersection with the loglikelihood
            function. Defaults to an adaptive value based on the maximal power of Q.

    Returns:
        The alpha-likelihood-ratio confidence interval.
    """
    if nevals is None:
        nevals = max(10000, int(np.pi / 2 * 1000 * 2 * result.evaluation_schedule[-1]))

    def loglikelihood(theta, one_counts, all_counts):
        loglik = 0
        for i, k in enumerate(result.evaluation_schedule):
            loglik += np.log(np.sin((2 * k + 1) * theta) ** 2) * one_counts[i]
            loglik += np.log(np.cos((2 * k + 1) * theta) ** 2) * (all_counts[i] - one_counts[i])
        return loglik

    one_counts = result.good_counts
    all_counts = [result.shots] * len(one_counts)

    eps = 1e-15  # to avoid invalid value in log
    thetas = np.linspace(0 + eps, np.pi / 2 - eps, nevals)
    values = np.zeros(len(thetas))
    for i, theta in enumerate(thetas):
        values[i] = loglikelihood(theta, one_counts, all_counts)

    loglik_mle = loglikelihood(result.theta, one_counts, all_counts)
    chi2_quantile = chi2.ppf(1 - alpha, df=1)
    thres = loglik_mle - chi2_quantile / 2

    # the (outer) LR confidence interval
    above_thres = thetas[values >= thres]

    # it might happen that the `above_thres` array is empty,
    # to still provide a valid result use safe_min/max which
    # then yield [0, pi/2]
    confint = [_safe_min(above_thres, default=0), _safe_max(above_thres, default=np.pi / 2)]
    mapped_confint = tuple(result.post_processing(np.sin(bound) ** 2) for bound in confint)

    return mapped_confint


def _get_counts(
    circuit_results: List[Union[np.ndarray, List[float], Dict[str, int]]],
    estimation_problem: EstimationProblem,
    num_state_qubits: int,
) -> Tuple[List[float], List[int]]:
    """Get the good and total counts.

    Returns:
        A pair of two lists, ([1-counts per experiment], [shots per experiment]).

    Raises:
        AlgorithmError: If self.run() has not been called yet.
    """
    one_hits = []  # h_k: how often 1 has been measured, for a power Q^(m_k)
    all_hits = []  # shots_k: how often has been measured at a power Q^(m_k)
    if all(isinstance(data, (list, np.ndarray)) for data in circuit_results):
        probabilities = []
        num_qubits = int(np.log2(len(circuit_results[0])))  # the total number of qubits
        for statevector in circuit_results:
            p_k = 0.0
            for i, amplitude in enumerate(statevector):
                probability = np.abs(amplitude) ** 2
                # consider only state qubits and revert bit order
                bitstr = bin(i)[2:].zfill(num_qubits)[-num_state_qubits:][::-1]
                objectives = [bitstr[index] for index in estimation_problem.objective_qubits]
                if estimation_problem.is_good_state(objectives):
                    p_k += probability
            probabilities += [p_k]

        one_hits = probabilities
        all_hits = np.ones_like(one_hits)
    else:
        for counts in circuit_results:
            all_hits.append(sum(counts.values()))
            one_hits.append(
                sum(
                    count
                    for bitstr, count in counts.items()
                    if estimation_problem.is_good_state(bitstr)
                )
            )

    return one_hits, all_hits
