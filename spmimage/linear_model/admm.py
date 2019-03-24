from logging import getLogger
from abc import abstractmethod

import numpy as np
import scipy as sp

from sklearn.utils import check_array, check_X_y
from sklearn.utils.extmath import safe_sparse_dot
from sklearn.base import RegressorMixin
from sklearn.linear_model.base import LinearModel
from sklearn.linear_model.coordinate_descent import _alpha_grid
from sklearn.externals.joblib import Parallel, delayed

logger = getLogger(__name__)


def _dia_to_tridiagonal(X):
    n_samples = X.shape[0]
    for index, offset in enumerate(X.offsets):
        if offset == 0:
            zero = index
        if offset == -1:
            minusone = index
        if offset == 1:
            one = index
    band = X.data[[one, zero, minusone], :]
    return band


def _soft_threshold(X: np.ndarray, thresh: float) -> np.ndarray:
    return np.where(np.abs(X) <= thresh, 0, X - thresh * np.sign(X))


def _cost_function(X, y, w, z, alpha):
    n_samples = X.shape[0]
    return np.linalg.norm(y - X.dot(w)) / n_samples + alpha * np.sum(np.abs(z))


def _update(X, y_k, D, coef_matrix, inv_Xy_k, inv_D, alpha, rho, max_iter, tol, tridiagonal):
    # Initialize ADMM parameters
    n_samples = X.shape[0]

    w_k = X.T.dot(y_k) / n_samples
    z_k = D.dot(w_k)
    h_k = np.zeros(w_k.shape)

    cost = _cost_function(X, y_k, w_k, z_k, alpha)
    threshold = alpha / rho
    for t in range(max_iter):
        # Update
        if tridiagonal:
            w_k = inv_Xy_k + \
                sp.linalg.solve_banded((1, 1), coef_matrix,
                                        safe_sparse_dot(D.T, rho * z_k - h_k))
        else:
            w_k = inv_Xy_k + inv_D.dot(z_k - h_k / rho)
        Dw_t = D.dot(w_k)
        z_k = _soft_threshold(Dw_t + h_k / rho, threshold)
        h_k += rho * (Dw_t - z_k)

        # after cost
        pre_cost = cost
        cost = _cost_function(X, y_k, w_k, z_k, alpha)
        gap = np.abs(cost - pre_cost)
        if gap < tol:
            break
    # should return z_k as well since it's sparse by soft threshold ?!
    return w_k, t


def admm_path(X, y, Xy=None, alphas=None, eps=1e-3, n_alphas=100, rho=1.0, max_iter=1000, tol=1e-04):
    _, n_features = X.shape
    multi_output = False
    n_iters = []

    if y.ndim != 1:
        multi_output = True
        _, n_outputs = y.shape

    if alphas is None:
        alphas = _alpha_grid(X, y, Xy=Xy, l1_ratio=1.0, eps=eps, n_alphas=n_alphas)
    else:
        alphas = np.sort(alphas)[::-1]
        n_alphas = len(alphas)

    if not multi_output:
        coefs = np.zeros((n_features, n_alphas), dtype=X.dtype)
    else:
        coefs = np.zeros((n_features, n_outputs, n_alphas), dtype=X.dtype)

    for i, alpha in enumerate(alphas):
        clf = LassoADMM(alpha=alpha, rho=rho, max_iter=max_iter, tol=tol)
        clf.fit(X, y)
        coefs[..., i] = clf.coef_
        n_iters.append(clf.n_iter_)

    return alphas, coefs, n_iters


def _admm(
        X: np.ndarray, y: np.ndarray, D: np.ndarray, alpha: float,
        rho: float, tol: float, max_iter: int, tridiagonal: bool):
    """Alternate Direction Multiplier Method(ADMM) for Generalized Lasso.

    Minimizes the objective function::

            1 / (2 * n_samples) * ||y - Xw||^2_2 + alpha * ||z||_1

    where::

            Dw = z

    To solve this problem, ADMM uses augmented Lagrangian

            1 / (2 * n_samples) * ||y - Xw||^2_2 + alpha * ||z||_1
            + h^T (Dw - z) + rho / 2 * ||Dw - z||^2_2

    where h is Lagrange multiplier and rho is tuning parameter.
    """
    n_samples, n_features = X.shape
    n_targets = y.shape[1]

    w_t = np.empty((n_features, n_targets), dtype=X.dtype)

    # Calculate inverse matrix
    if tridiagonal:
        coef_matrix = _dia_to_tridiagonal(sp.sparse.dia_matrix(safe_sparse_dot(X.T, X) / n_samples
                                        + rho * safe_sparse_dot(D.T, D))) # banded form
        inv_Xy = sp.linalg.solve_banded((1, 1), coef_matrix, safe_sparse_dot(X.T, y) / n_samples)
        inv_D = D # does not use this
    else:
        coef_matrix = X.T.dot(X) / n_samples + rho * D.T.dot(D)
        inv_matrix = np.linalg.inv(coef_matrix)
        inv_Xy = inv_matrix.dot(X.T).dot(y) / n_samples
        inv_D = inv_matrix.dot(rho * D.T)

    # Update ADMM parameters by columns
    n_iter_ = np.empty((n_targets,), dtype=int)
    if n_targets == 1:
        if tridiagonal:
            w_t, n_iter_[0] = _update(X, y, D, coef_matrix, inv_Xy, inv_D, alpha, rho, max_iter, tol, tridiagonal)
        else:
            w_t, n_iter_[0] = _update(X, y, D, coef_matrix, inv_Xy, inv_D, alpha, rho, max_iter, tol, tridiagonal)
    else:
        results = Parallel(n_jobs=-1, backend='threading')(
            delayed(_update)(X, y[:, k], D, coef_matrix, inv_Xy[:, k], inv_D, alpha, rho, max_iter, tol, tridiagonal) for k in range(n_targets)
        )
        for k in range(n_targets):
            w_t[:, k], n_iter_[k] = results[k]

    return np.squeeze(w_t.T), n_iter_.tolist()


class GeneralizedLasso(LinearModel, RegressorMixin):
    """Alternate Direction Multiplier Method(ADMM) for Generalized Lasso.
    """

    def __init__(self, alpha=1.0, rho=1.0, fit_intercept=True,
                 normalize=False, copy_X=True, max_iter=1000,
                 tol=1e-4, tridiagonal=False):
        self.alpha = alpha
        self.rho = rho
        self.fit_intercept = fit_intercept
        self.normalize = normalize
        self.copy_X = copy_X
        self.max_iter = max_iter
        self.tol = tol
        self.tridiagonal = tridiagonal

    def fit(self, X, y, check_input=False):
        if self.alpha == 0:
            logger.warning("""
With alpha=0, this algorithm does not converge well. You are advised to use the LinearRegression estimator
""")

        if check_input:
            X, y = check_X_y(X, y, accept_sparse='csc',
                             order='F', dtype=[np.float64, np.float32],
                             copy=self.copy_X and self.fit_intercept,
                             multi_output=True, y_numeric=True)
            y = check_array(y, order='F', copy=False, dtype=X.dtype.type,
                            ensure_2d=False)

        X, y, X_offset, y_offset, X_scale = self._preprocess_data(X, y, fit_intercept=self.fit_intercept,
                                                                  normalize=self.normalize,
                                                                  copy=self.copy_X and not check_input)

        if y.ndim == 1:
            y = y[:, np.newaxis]

        n_features = X.shape[1]
        if self.tridiagonal:
            X = sp.sparse.dia_matrix(X)
        D = self.generate_transform_matrix(n_features)
        self.coef_, self.n_iter_ = _admm(X, y, D, self.alpha, self.rho,
                                        self.tol, self.max_iter, self.tridiagonal)

        if y.shape[1] == 1:
            self.n_iter_ = self.n_iter_[0]

        self._set_intercept(X_offset, y_offset, X_scale)

        # workaround since _set_intercept will cast self.coef_ into X.dtype
        self.coef_ = np.asarray(self.coef_, dtype=X.dtype)

        return self

    @abstractmethod
    def generate_transform_matrix(self, n_features: int) -> np.ndarray:
        """
        :return:
        """


class LassoADMM(GeneralizedLasso):
    """Linear Model trained with L1 prior as regularizer (aka the Lasso)
    The optimization objective for Lasso is::
        (1 / (2 * n_samples)) * ||y - Xw||^2_2 + alpha * ||w||_1
    """

    def __init__(self, alpha=1.0, rho=1.0, fit_intercept=True,
                 normalize=False, copy_X=True, max_iter=1000,
                 tol=1e-4):
        super().__init__(alpha=alpha, rho=rho, fit_intercept=fit_intercept,
                         normalize=normalize, copy_X=copy_X, max_iter=max_iter,
                         tol=tol)

    def generate_transform_matrix(self, n_features: int) -> np.ndarray:
        return np.eye(n_features)


class FusedLassoADMM(GeneralizedLasso):
    """Fused Lasso minimises the following objective function.

1/(2 * n_samples) * ||y - Xw||^2_2 + \lambda_1 \sum_{j=1}^p |w_j| + \lambda_2 \sum_{j=2}^p |w_j - w_{j-1}|
    """

    def __init__(self, alpha=1.0, sparse_coef=1.0, fused_coef=1.0, rho=1.0, fit_intercept=True,
                 normalize=False, copy_X=True, max_iter=1000,
                 tol=1e-4, diagonal=False):
        super().__init__(alpha=alpha, rho=rho, fit_intercept=fit_intercept,
                         normalize=normalize, copy_X=copy_X, max_iter=max_iter,
                         tol=tol, tridiagonal=diagonal)
        self.sparse_coef = sparse_coef
        self.fused_coef = fused_coef
        self.alpha = alpha
        self.diagonal = diagonal

    def generate_transform_matrix(self, n_features: int) -> np.ndarray:
        fused = np.eye(n_features) - np.eye(n_features, k=-1)
        fused[0, 0] = 0
        generated = self.sparse_coef * np.eye(n_features) + self.fused_coef * fused
        if self.diagonal:
            return sp.sparse.dia_matrix(generated)
        return generated

class TrendFilteringADMM(GeneralizedLasso):
    def __init__(self, margin = 1, alpha=1.0, rho=1.0, fit_intercept=True,
                 normalize=False, copy_X=True, max_iter=1000,
                 tol=1e-4):
        super().__init__(alpha=alpha, rho=rho, fit_intercept=fit_intercept,
                         normalize=normalize, copy_X=copy_X, max_iter=max_iter,
                         tol=tol)
        self.margin = margin
        
    def generate_transform_matrix(self, n_features: int) -> np.ndarray:
        D = np.eye(n_features, k=-1) + np.eye(n_features, k=1) - 2*np.eye(n_features)
        D[0:self.margin,0] = -2
        D[0:self.margin,1] = 2
        D[-1:-self.margin,-1] = -2
        D[-1:-self.margin,-2] = 2
        return D