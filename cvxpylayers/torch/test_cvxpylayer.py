import unittest

import cvxpy as cp
import numpy as np
import numpy.random as npr
import torch
from torch.autograd import grad

from cvxpylayers.torch import CvxpyLayer
import diffcp


def set_seed(x):
    npr.seed(x)
    torch.manual_seed(x)


def sigmoid(z):
    return 1 / (1 + np.exp(-z))


class TestCvxpyLayer(unittest.TestCase):

    def test_example(self):
        n, m = 2, 3
        x = cp.Variable(n)
        A = cp.Parameter((m, n))
        b = cp.Parameter(m)
        constraints = [x >= 0]
        objective = cp.Minimize(0.5 * cp.pnorm(A @ x - b, p=1))
        problem = cp.Problem(objective, constraints)
        assert problem.is_dpp()

        cvxpylayer = CvxpyLayer(problem, parameters=[A, b], variables=[x])
        A_tch = torch.randn(m, n, requires_grad=True)
        b_tch = torch.randn(m, requires_grad=True)

        # solve the problem
        solution, = cvxpylayer(A_tch, b_tch)

        # compute the gradient of the sum of the solution with respect to A, b
        solution.sum().backward()

    def test_simple_batch_socp(self):
        set_seed(243)
        n = 5
        m = 1
        batch_size = 4

        P_sqrt = cp.Parameter((n, n), name='P_sqrt')
        q = cp.Parameter((n, 1), name='q')
        A = cp.Parameter((m, n), name='A')
        b = cp.Parameter((m, 1), name='b')

        x = cp.Variable((n, 1), name='x')

        objective = 0.5 * cp.sum_squares(P_sqrt @ x) + q.T @ x
        constraints = [A@x == b, cp.norm(x) <= 1]
        prob = cp.Problem(cp.Minimize(objective), constraints)

        prob_tch = CvxpyLayer(prob, [P_sqrt, q, A, b], [x])

        torch.set_default_tensor_type(torch.DoubleTensor)
        P_sqrt_tch = torch.randn(batch_size, n, n, requires_grad=True)
        q_tch = torch.randn(batch_size, n, 1, requires_grad=True)
        A_tch = torch.randn(batch_size, m, n, requires_grad=True)
        b_tch = torch.randn(batch_size, m, 1, requires_grad=True)

        torch.autograd.gradcheck(prob_tch, (P_sqrt_tch, q_tch, A_tch, b_tch))

    def test_least_squares(self):
        set_seed(243)
        m, n = 100, 20

        A = cp.Parameter((m, n))
        b = cp.Parameter(m)
        x = cp.Variable(n)
        obj = cp.sum_squares(A@x - b) + cp.sum_squares(x)
        prob = cp.Problem(cp.Minimize(obj))
        prob_th = CvxpyLayer(prob, [A, b], [x])

        A_th = torch.randn(m, n).double().requires_grad_()
        b_th = torch.randn(m).double().requires_grad_()

        x = prob_th(A_th, b_th, solver_args={"eps": 1e-10})[0]

        def lstsq(
            A,
            b): return torch.solve(
            (A_th.t() @ b_th).unsqueeze(1),
            A_th.t() @ A_th +
            torch.eye(n).double())[0]
        x_lstsq = lstsq(A_th, b_th)

        grad_A_cvxpy, grad_b_cvxpy = grad(x.sum(), [A_th, b_th])
        grad_A_lstsq, grad_b_lstsq = grad(x_lstsq.sum(), [A_th, b_th])

        self.assertAlmostEqual(
            torch.norm(
                grad_A_cvxpy -
                grad_A_lstsq).item(),
            0.0)
        self.assertAlmostEqual(
            torch.norm(
                grad_b_cvxpy -
                grad_b_lstsq).item(),
            0.0)

    def test_logistic_regression(self):
        set_seed(243)
        N, n = 10, 2
        X_np = np.random.randn(N, n)
        a_true = np.random.randn(n, 1)
        y_np = np.round(sigmoid(X_np @ a_true + np.random.randn(N, 1) * 0.5))

        X_tch = torch.from_numpy(X_np)
        X_tch.requires_grad_(True)
        lam_tch = 0.1 * torch.ones(1, requires_grad=True, dtype=torch.double)

        a = cp.Variable((n, 1))
        X = cp.Parameter((N, n))
        lam = cp.Parameter(1, nonneg=True)
        y = y_np

        log_likelihood = cp.sum(
            cp.multiply(y, X @ a) -
            cp.log_sum_exp(cp.hstack([np.zeros((N, 1)), X @ a]).T, axis=0,
                           keepdims=True).T
        )
        prob = cp.Problem(
            cp.Minimize(-log_likelihood + lam * cp.sum_squares(a)))

        fit_logreg = CvxpyLayer(prob, [X, lam], [a])

        def layer_eps(*x):
            return fit_logreg(*x, solver_args={"eps": 1e-12})

        torch.autograd.gradcheck(layer_eps,
                                 (X_tch,
                                  lam_tch),
                                 eps=1e-4,
                                 atol=1e-3,
                                 rtol=1e-3)

    def test_entropy_maximization(self):
        set_seed(243)
        n, m, p = 5, 3, 2

        tmp = np.random.rand(n)
        A_np = np.random.randn(m, n)
        b_np = A_np.dot(tmp)
        F_np = np.random.randn(p, n)
        g_np = F_np.dot(tmp) + np.random.rand(p)

        x = cp.Variable(n)
        A = cp.Parameter((m, n))
        b = cp.Parameter(m)
        F = cp.Parameter((p, n))
        g = cp.Parameter(p)
        obj = cp.Maximize(cp.sum(cp.entr(x)) - .01 * cp.sum_squares(x))
        constraints = [A * x == b,
                       F * x <= g]
        prob = cp.Problem(obj, constraints)
        layer = CvxpyLayer(prob, [A, b, F, g], [x])

        A_tch, b_tch, F_tch, g_tch = map(
            lambda x: torch.from_numpy(x).requires_grad_(True), [
                A_np, b_np, F_np, g_np])
        torch.autograd.gradcheck(lambda *x: layer(*x,
                                                  solver_args={"eps": 1e-12, "max_iters": 10000}),
                                 (A_tch,
                                  b_tch,
                                  F_tch,
                                  g_tch),
                                 eps=1e-4,
                                 atol=1e-3,
                                 rtol=1e-3)

    def test_lml(self):
        set_seed(1)
        k = 2
        x = cp.Parameter(4)
        y = cp.Variable(4)
        obj = -x * y - cp.sum(cp.entr(y)) - cp.sum(cp.entr(1. - y))
        cons = [cp.sum(y) == k]
        prob = cp.Problem(cp.Minimize(obj), cons)
        lml = CvxpyLayer(prob, [x], [y])

        x_th = torch.DoubleTensor([1., -1., -1., -1.])
        x_th.requires_grad_(True)
        torch.autograd.gradcheck(lml, x_th, eps=1e-5, atol=1e-4, rtol=1e-4)

    def test_sdp(self):
        set_seed(2)

        n = 3
        p = 3
        C = cp.Parameter((n, n))
        A = [cp.Parameter((n, n)) for _ in range(p)]
        b = [cp.Parameter((1, 1)) for _ in range(p)]

        C_tch = torch.randn(n, n, requires_grad=True).double()
        A_tch = [torch.randn(n, n, requires_grad=True).double()
                 for _ in range(p)]
        b_tch = [torch.randn(1, 1, requires_grad=True).double()
                 for _ in range(p)]

        X = cp.Variable((n, n), symmetric=True)
        constraints = [X >> 0]
        constraints += [
            cp.trace(A[i]@X) == b[i] for i in range(p)
        ]
        prob = cp.Problem(cp.Minimize(cp.trace(C@X) + cp.sum_squares(X)),
                          constraints)
        layer = CvxpyLayer(prob, [C] + A + b, [X])
        torch.autograd.gradcheck(lambda *x: layer(*x,
                                                  solver_args={"eps": 1e-12}),
                                 [C_tch] + A_tch + b_tch,
                                 eps=1e-6,
                                 atol=1e-3,
                                 rtol=1e-3)

    def test_not_enough_parameters(self):
        x = cp.Variable(1)
        lam = cp.Parameter(1, nonneg=True)
        lam2 = cp.Parameter(1, nonneg=True)
        objective = lam * cp.norm(x, 1) + lam2 * cp.sum_squares(x)
        prob = cp.Problem(cp.Minimize(objective))
        with self.assertRaises(AssertionError):
            layer = CvxpyLayer(prob, [lam], [x])  # noqa: F841

    def test_too_many_variables(self):
        x = cp.Variable(1)
        y = cp.Variable(1)
        lam = cp.Parameter(1, nonneg=True)
        objective = lam * cp.norm(x, 1)
        prob = cp.Problem(cp.Minimize(objective))
        with self.assertRaises(AssertionError):
            layer = CvxpyLayer(prob, [lam], [x, y])  # noqa: F841

    def test_infeasible(self):
        x = cp.Variable(1)
        param = cp.Parameter(1)
        prob = cp.Problem(cp.Minimize(param), [x >= 1, x <= -1])
        layer = CvxpyLayer(prob, [param], [x])
        param_tch = torch.ones(1)
        with self.assertRaises(diffcp.SolverError):
            layer(param_tch)

    def test_unbounded(self):
        x = cp.Variable(1)
        param = cp.Parameter(1)
        prob = cp.Problem(cp.Minimize(x), [x <= param])
        layer = CvxpyLayer(prob, [param], [x])
        param_tch = torch.ones(1)
        with self.assertRaises(diffcp.SolverError):
            layer(param_tch)

    def test_incorrect_parameter_shape(self):
        set_seed(243)
        m, n = 100, 20

        A = cp.Parameter((m, n))
        b = cp.Parameter(m)
        x = cp.Variable(n)
        obj = cp.sum_squares(A@x - b) + cp.sum_squares(x)
        prob = cp.Problem(cp.Minimize(obj))
        prob_th = CvxpyLayer(prob, [A, b], [x])

        A_th = torch.randn(32, m, n).double().requires_grad_()
        b_th = torch.randn(20, m).double().requires_grad_()

        with self.assertRaises(RuntimeError):
            prob_th(A_th, b_th)

        A_th = torch.randn(32, m, n).double().requires_grad_()
        b_th = torch.randn(32, 2*m).double().requires_grad_()

        with self.assertRaises(RuntimeError):
            prob_th(A_th, b_th)

        A_th = torch.randn(m, n).double().requires_grad_()
        b_th = torch.randn(2*m).double().requires_grad_()

        with self.assertRaises(RuntimeError):
            prob_th(A_th, b_th)


if __name__ == '__main__':
    unittest.main()
