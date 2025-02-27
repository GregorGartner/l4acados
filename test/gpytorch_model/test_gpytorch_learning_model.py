import os
from time import perf_counter, sleep
from typing import Optional, Tuple

import numpy as np
import torch
import gpytorch

from l4acados.models import PyTorchFeatureSelector, GPyTorchResidualModel
from l4acados.models.pytorch_models.gpytorch_models.gpytorch_data_processing_strategy import (
    OnlineLearningStrategy,
    RecordDataStrategy,
)
from l4acados.models.pytorch_models.gpytorch_models import gpytorch_gp


def count_lines(filepath: str) -> int:
    with open(filepath, "r") as file:
        return sum(1 for line in file)


def test_unconditioned_gp(num_tests: int = 6) -> None:
    """Test initalization, querying and updating of unconditioned gp"""
    add_data_times_ms = []
    eval_times_ms = []
    current_dir = os.getcwd()
    x_data_path = os.path.join(current_dir, "tmp_x_data.txt")
    y_data_path = os.path.join(current_dir, "tmp_y_data.txt")
    for curr_test_num in range(num_tests):
        print(f"\n\nPerforming run {curr_test_num}/{num_tests}")
        # Test random input size and random jacobian
        state_dimension = np.random.randint(1, 10)
        residual_dimension = np.random.randint(1, 5)
        input_feature_selection = np.random.randint(0, 2, size=(state_dimension))
        input_feature_selection[0] = 1  # Make sure input dimension to GP is at least 1
        input_dimension = np.sum(input_feature_selection)

        if (curr_test_num % 2) == 0:
            input_feature_selection = None
            input_dimension = state_dimension

        enable_ard = curr_test_num > num_tests / 2

        hyperparameters = (
            {
                "likelihood.task_noises": torch.rand(residual_dimension) + 1e-4,
                "covar_module.base_kernel.lengthscale": 10
                * torch.rand(residual_dimension).reshape(residual_dimension, 1, 1)
                + 1,
                "covar_module.outputscale": torch.rand(residual_dimension) + 1e-4,
            }
            if not enable_ard
            else {
                "likelihood.task_noises": torch.rand(residual_dimension) + 1e-4,
                "covar_module.base_kernel.lengthscale": 10
                * torch.rand((residual_dimension, 1, input_dimension))
                + 1,
                "covar_module.outputscale": torch.rand(residual_dimension) + 1e-4,
            }
        )

        input_selection = PyTorchFeatureSelector(input_feature_selection)

        gpytorch_gp_model = gpytorch_gp.BatchIndependentMultitaskGPModel(
            train_x=None,
            train_y=None,
            likelihood=gpytorch.likelihoods.MultitaskGaussianLikelihood(
                num_tasks=residual_dimension,
            ),
            residual_dimension=residual_dimension,
            input_dimension=input_dimension,
            use_ard=enable_ard,
        )

        gpytorch_gp_model.initialize(**hyperparameters)

        gpytorch_gp_model.eval()
        gpytorch_gp_model.likelihood.eval()

        num_record_steps = 20
        gp = GPyTorchResidualModel(
            gp_model=gpytorch_gp_model,
            feature_selector=input_selection,
            data_processing_strategy=RecordDataStrategy(
                x_data_path,
                y_data_path,
                buffer_size=num_record_steps,
                # NOTE: num_record_steps needs to be divisible by buffer size; otherwise wrong file size
            ),
        )

        for _ in range(num_record_steps):
            x_rand_data = np.random.rand(state_dimension)
            y_rand_data = np.random.rand(residual_dimension)

            time_before = perf_counter()
            gp.record_datapoint(x_rand_data, y_rand_data)
            add_data_times_ms.append((perf_counter() - time_before) * 1e3)

            num_query_points = np.random.randint(1, 40)
            x_rand_eval = torch.Tensor(
                np.random.rand(num_query_points, state_dimension)
            )

            time_before = perf_counter()
            val, jac = gp.value_and_jacobian(x_rand_eval)
            assert val.shape == torch.Size([num_query_points, residual_dimension])
            assert jac.shape == torch.Size(
                [residual_dimension, num_query_points, state_dimension]
            )
            eval_times_ms.append((perf_counter() - time_before) * 1e3)

    print(
        f"data adding avg over {len(add_data_times_ms)} "
        f"trials: {np.average(add_data_times_ms):.4f} ms."
    )
    print(
        f"gp eval times over {len(eval_times_ms)} "
        f"trials: {np.average(eval_times_ms):.4f} ms."
    )

    # wait for threads to finish
    sleep(5)

    num_lines_x_data = count_lines(x_data_path)
    num_lines_y_data = count_lines(y_data_path)

    assert num_lines_x_data == num_tests * num_record_steps
    assert num_lines_y_data == num_tests * num_record_steps

    os.remove(x_data_path)
    os.remove(y_data_path)


def generate_fake_data(
    filepath_x: str,
    filepath_y: str,
    state_size: int,
    residual_size: int,
    num_datapoints: Optional[int] = 800,
) -> None:
    os.remove(filepath_x) if os.path.exists(filepath_x) else None
    os.remove(filepath_y) if os.path.exists(filepath_y) else None
    x_data = np.random.rand(num_datapoints, state_size)
    y_data = np.random.rand(num_datapoints, residual_size)
    np.savetxt(filepath_x, x_data, delimiter=",")
    np.savetxt(filepath_y, y_data, delimiter=",")


def load_data(x_data_path: str, y_data_path: str) -> Tuple[torch.Tensor, torch.Tensor]:
    try:
        with open(x_data_path, "r") as file:
            train_x_tensor = torch.from_numpy(np.genfromtxt(file, delimiter=",")).type(
                torch.float32
            )
    except FileNotFoundError:
        print(f"File {x_data_path} does not exist.")
    except Exception as e:
        raise e

    # If the data only has one feature, we need to add a batch dimension
    if train_x_tensor.dim() == 1:
        train_x_tensor = torch.unsqueeze(train_x_tensor, -1)

    try:
        with open(y_data_path, "r") as file:
            train_y_tensor = torch.from_numpy(np.genfromtxt(file, delimiter=",")).type(
                torch.float32
            )
    except FileNotFoundError:
        print(f"File {y_data_path} does not exist.")
    except Exception as e:
        raise e

    # If the data only has one feature, we need to add a batch dimension
    if train_y_tensor.dim() == 1:
        train_y_tensor = torch.unsqueeze(train_y_tensor, -1)

    return train_x_tensor, train_y_tensor


def test_load_gp_from_file(num_tests: int = 6) -> None:
    """Test initialization of the GP from a file"""
    add_data_times_ms = []
    eval_times_ms = []
    current_dir = os.getcwd()
    x_data_path = os.path.join(current_dir, "tmp_x_data.txt")
    y_data_path = os.path.join(current_dir, "tmp_y_data.txt")

    for curr_test_num in range(num_tests):
        print(f"\n\nPerforming run {curr_test_num}/{num_tests}")
        # Test random input size and random jacobian
        state_dimension = np.random.randint(1, 10)
        residual_dimension = np.random.randint(1, 5)
        input_feature_selection = np.random.randint(0, 2, size=(state_dimension))
        input_feature_selection[0] = 1  # Make sure input dimension to GP is at least 1
        input_dimension = np.sum(input_feature_selection)

        if (curr_test_num % 2) == 0:
            input_feature_selection = None
            input_dimension = state_dimension

        enable_ard = curr_test_num > num_tests / 2
        hyperparameters = (
            {
                "likelihood.task_noises": torch.rand(residual_dimension) + 1e-4,
                "covar_module.base_kernel.lengthscale": 10
                * torch.rand(residual_dimension).reshape(residual_dimension, 1, 1)
                + 1,
                "covar_module.outputscale": torch.rand(residual_dimension) + 1e-4,
            }
            if not enable_ard
            else {
                "likelihood.task_noises": torch.rand(residual_dimension) + 1e-4,
                "covar_module.base_kernel.lengthscale": 10
                * torch.rand((residual_dimension, 1, input_dimension))
                + 1,
                "covar_module.outputscale": torch.rand(residual_dimension) + 1e-4,
            }
        )

        num_fake_datapoints = np.random.randint(100, 1000)

        generate_fake_data(
            x_data_path,
            y_data_path,
            state_dimension,
            residual_dimension,
            num_fake_datapoints,
        )

        max_num_datapoints = (
            None
            if np.random.randint(2) == 0
            else np.random.randint(50, num_fake_datapoints)
        )
        print(
            f"Setting max num datapoints to {max_num_datapoints} out of "
            f"{num_fake_datapoints} datapoints."
        )

        train_x_tensor, train_y_tensor = load_data(x_data_path, y_data_path)

        # Generate a random permutation of indices
        if np.random.randint(2):
            print("Permuting dataset")
            # Set manual seed for reproducibility
            indices = torch.randperm(train_x_tensor.size(0))

            train_x_tensor = train_x_tensor[indices]
            train_y_tensor = train_y_tensor[indices]

        if max_num_datapoints is not None:
            train_x_tensor = train_x_tensor[:max_num_datapoints, :]
            train_y_tensor = train_y_tensor[:max_num_datapoints, :]

        print(
            f"Loaded training data sucessfully with x and y shapes "
            f"{train_x_tensor.shape} {train_y_tensor.shape}"
        )

        input_selection = PyTorchFeatureSelector(input_feature_selection)

        gpytorch_model = gpytorch_gp.BatchIndependentMultitaskGPModel(
            input_selection(train_x_tensor),
            train_y_tensor,
            gpytorch.likelihoods.MultitaskGaussianLikelihood(
                num_tasks=train_y_tensor.shape[1],
                has_task_noise=True,
                has_global_noise=False,
            ),
            use_ard=enable_ard,
        )

        gpytorch_model.initialize(**hyperparameters)

        gpytorch_model.eval()
        gpytorch_model.likelihood.eval()

        gp = GPyTorchResidualModel(
            gp_model=gpytorch_model,
            feature_selector=input_selection,
            data_processing_strategy=RecordDataStrategy(x_data_path, y_data_path),
        )

        for _ in range(20):
            x_rand_data = np.random.rand(state_dimension)
            y_rand_data = np.random.rand(residual_dimension)

            time_before = perf_counter()
            gp.record_datapoint(x_rand_data, y_rand_data)
            add_data_times_ms.append((perf_counter() - time_before) * 1e3)

            num_query_points = np.random.randint(1, 40)
            x_rand_eval = torch.Tensor(
                np.random.rand(num_query_points, state_dimension)
            )

            time_before = perf_counter()
            val, jac = gp.value_and_jacobian(x_rand_eval)
            assert val.shape == torch.Size([num_query_points, residual_dimension])
            assert jac.shape == torch.Size(
                [residual_dimension, num_query_points, state_dimension]
            )
            eval_times_ms.append((perf_counter() - time_before) * 1e3)

    print(
        f"data adding avg over {len(add_data_times_ms)} "
        f"trials: {np.average(add_data_times_ms):.4f} ms."
    )
    print(
        f"gp eval times over {len(eval_times_ms)} "
        f"trials: {np.average(eval_times_ms):.4f} ms."
    )

    # wait for threads to finish
    sleep(5)

    os.remove(x_data_path)
    os.remove(y_data_path)


def test_incorporate_new_data(num_tests: int = 6):
    add_data_times_ms = []
    eval_times_ms = []

    max_points_online = 20
    sim_steps_list = np.linspace(
        max_points_online - 5, max_points_online + 5, num_tests, dtype=int
    )

    for i in range(num_tests):
        # Test random input size and random jacobian
        state_dimension = np.random.randint(1, 10)
        residual_dimension = np.random.randint(1, 5)
        input_feature_selection = np.random.randint(0, 2, size=(state_dimension))
        input_feature_selection[0] = 1  # Make sure input dimension to GP is at least 1
        input_dimension = np.sum(input_feature_selection)

        hyperparameters = {
            "likelihood.task_noises": torch.rand(residual_dimension) + 1e-4,
            "covar_module.base_kernel.lengthscale": 10
            * torch.rand(residual_dimension).reshape(residual_dimension, 1, 1)
            + 1,
            "covar_module.outputscale": torch.rand(residual_dimension) + 1e-4,
        }

        input_selection = PyTorchFeatureSelector(input_feature_selection)

        gpytorch_gp_model = gpytorch_gp.BatchIndependentMultitaskGPModel(
            train_x=None,
            train_y=None,
            likelihood=gpytorch.likelihoods.MultitaskGaussianLikelihood(
                num_tasks=residual_dimension,
            ),
            input_dimension=input_dimension,
            residual_dimension=residual_dimension,
        )

        gpytorch_gp_model.initialize(**hyperparameters)

        gpytorch_gp_model.eval()
        gpytorch_gp_model.likelihood.eval()

        gp = GPyTorchResidualModel(
            gp_model=gpytorch_gp_model,
            feature_selector=input_selection,
            data_processing_strategy=OnlineLearningStrategy(max_points_online),
        )

        gp.value_and_jacobian(torch.rand(1, state_dimension))

        for _ in range(sim_steps_list[i]):
            x_rand_data = np.random.rand(state_dimension)
            y_rand_data = np.random.rand(residual_dimension)

            time_before = perf_counter()
            gp.record_datapoint(x_rand_data, y_rand_data)
            add_data_times_ms.append((perf_counter() - time_before) * 1e3)

            num_query_points = np.random.randint(1, 40)
            x_rand_eval = torch.Tensor(
                np.random.rand(num_query_points, state_dimension)
            )

            time_before = perf_counter()
            val, jac = gp.value_and_jacobian(x_rand_eval)
            assert val.shape == torch.Size([num_query_points, residual_dimension])
            assert jac.shape == torch.Size(
                [residual_dimension, num_query_points, state_dimension]
            )
            eval_times_ms.append((perf_counter() - time_before) * 1e3)

        assert gp.gp_model.train_inputs[0].shape == torch.Size(
            [min(sim_steps_list[i], max_points_online), sum(input_feature_selection)]
        )
        assert gp.gp_model.train_targets.shape == torch.Size(
            [min(sim_steps_list[i], max_points_online), residual_dimension]
        )

    print(
        f"data adding avg over {len(add_data_times_ms)} "
        f"trials: {np.average(add_data_times_ms):.4f} ms."
    )
    print(
        f"gp eval times over {len(eval_times_ms)} "
        f"trials: {np.average(eval_times_ms):.4f} ms."
    )


def train_gp_model(
    gp_model, torch_seed=None, training_iterations=10, learning_rate=1.0
):
    if torch_seed is not None:
        torch.manual_seed(torch_seed)

    likelihood = gp_model.likelihood
    train_x = gp_model.train_inputs[0]
    train_y = gp_model.train_targets

    # Find optimal model hyperparameters
    gp_model.train()
    likelihood.train()

    # Use the adam optimizer
    optimizer = torch.optim.Adam(
        filter(lambda p: p.requires_grad, gp_model.parameters()), lr=learning_rate
    )  # Includes GaussianLikelihood parameters

    # "Loss" for GPs - the marginal log likelihood
    mll = gpytorch.mlls.ExactMarginalLogLikelihood(likelihood, gp_model)

    prev_loss = np.inf
    num_loss_below_threshold = 0

    for i in range(training_iterations):
        optimizer.zero_grad()
        output = likelihood(gp_model(train_x))
        loss = -mll(output, train_y.reshape((train_y.numel(),)))
        loss = loss.sum()

        num_loss_below_threshold = (
            num_loss_below_threshold + 1
            if torch.any(torch.abs(loss - prev_loss)) < 5e-5
            else 0
        )

        if num_loss_below_threshold > 5:
            print(f"stopping GP optimization early after {i} iterations.")
            break

        prev_loss = loss

        loss.backward()
        if (i + 1) % 20 == 0:
            print("Iter %d/%d - Loss: %.3f" % (i + 1, training_iterations, loss.item()))

        optimizer.step()

    gp_model.eval()
    likelihood.eval()


def test_inducing_point_gp_from_file(num_tests: int = 6) -> None:
    """Test initialization of the GP from a file"""
    add_data_times_ms = []
    eval_times_ms = []
    current_dir = os.getcwd()
    x_data_path = os.path.join(current_dir, "tmp_x_data.txt")
    y_data_path = os.path.join(current_dir, "tmp_y_data.txt")

    for curr_test_num in range(num_tests):
        print(f"\n\nPerforming run {curr_test_num}/{num_tests}")
        # Test random input size and random jacobian
        state_dimension = np.random.randint(1, 10)
        residual_dimension = np.random.randint(1, 5)
        input_feature_selection = np.random.randint(0, 2, size=(state_dimension))
        input_feature_selection[0] = 1  # Make sure input dimension to GP is at least 1
        input_dimension = np.sum(input_feature_selection)

        if (curr_test_num % 2) == 0:
            input_feature_selection = None
            input_dimension = state_dimension

        num_fake_datapoints = np.random.randint(100, 1000)

        generate_fake_data(
            x_data_path,
            y_data_path,
            state_dimension,
            residual_dimension,
            num_fake_datapoints,
        )

        max_num_datapoints = (
            None
            if np.random.randint(2) == 0
            else np.random.randint(50, num_fake_datapoints)
        )
        print(
            f"Setting max num datapoints to {max_num_datapoints} out of "
            f"{num_fake_datapoints} datapoints."
        )

        num_iducing_points = 50

        train_x_tensor, train_y_tensor = load_data(x_data_path, y_data_path)

        # Generate a random permutation of indices
        if np.random.randint(2):
            print("Permuting dataset")
            # Set manual seed for reproducibility
            indices = torch.randperm(train_x_tensor.size(0))

            train_x_tensor = train_x_tensor[indices]
            train_y_tensor = train_y_tensor[indices]

        if max_num_datapoints is not None:
            train_x_tensor = train_x_tensor[:max_num_datapoints, :]
            train_y_tensor = train_y_tensor[:max_num_datapoints, :]

        print(
            f"Loaded training data sucessfully with x and y shapes "
            f"{train_x_tensor.shape} {train_y_tensor.shape}"
        )

        input_selection = PyTorchFeatureSelector(input_feature_selection)

        gpytorch_model = gpytorch_gp.BatchIndependentInducingPointGpModel(
            input_selection(train_x_tensor),
            train_y_tensor,
            gpytorch.likelihoods.MultitaskGaussianLikelihood(
                num_tasks=train_y_tensor.shape[1],
                has_task_noise=True,
                has_global_noise=False,
            ),
            num_iducing_points,
            use_ard=False,
        )

        gpytorch_model.train()
        gpytorch_model.likelihood.train()
        train_gp_model(gpytorch_model)
        gpytorch_model.eval()
        gpytorch_model.likelihood.eval()

        print(gpytorch_model.covar_module.inducing_points)
        print(gpytorch_model.covar_module.base_kernel.outputscale)
        print(gpytorch_model.covar_module.base_kernel.base_kernel.lengthscale)

        gp = GPyTorchResidualModel(
            gp_model=gpytorch_model,
            feature_selector=input_selection,
            data_processing_strategy=RecordDataStrategy(x_data_path, y_data_path),
        )

        for _ in range(20):
            x_rand_data = np.random.rand(state_dimension)
            y_rand_data = np.random.rand(residual_dimension)

            time_before = perf_counter()
            gp.record_datapoint(x_rand_data, y_rand_data)
            add_data_times_ms.append((perf_counter() - time_before) * 1e3)

            num_query_points = np.random.randint(1, 40)
            x_rand_eval = torch.Tensor(
                np.random.rand(num_query_points, state_dimension)
            )

            time_before = perf_counter()
            val, jac = gp.value_and_jacobian(x_rand_eval)
            assert val.shape == torch.Size([num_query_points, residual_dimension])
            assert jac.shape == torch.Size(
                [residual_dimension, num_query_points, state_dimension]
            )
            eval_times_ms.append((perf_counter() - time_before) * 1e3)

    print(
        f"data adding avg over {len(add_data_times_ms)} "
        f"trials: {np.average(add_data_times_ms):.4f} ms."
    )
    print(
        f"gp eval times over {len(eval_times_ms)} "
        f"trials: {np.average(eval_times_ms):.4f} ms."
    )

    os.remove(x_data_path)
    os.remove(y_data_path)


if __name__ == "__main__":
    torch.random.manual_seed(42)
    np.random.seed(42)
    test_unconditioned_gp()
    print(5 * "\n")
    test_load_gp_from_file()
    print(5 * "\n")
    test_incorporate_new_data()
    print(5 * "\n")
    test_inducing_point_gp_from_file()
