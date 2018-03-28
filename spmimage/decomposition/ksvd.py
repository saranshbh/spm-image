from logging import getLogger

import numpy as np

from sklearn.base import BaseEstimator
from sklearn.decomposition.dict_learning import SparseCodingMixin, sparse_encode
from sklearn.utils import check_array, check_random_state

logger = getLogger(__name__)


def _ksvd(Y: np.ndarray, n_components: int, k0: int, tol: float, max_iter: int, code_init: np.ndarray = None,
          dict_init: np.ndarray = None, n_jobs: int = 1):
    """_ksvd
    Finds a dictionary that can be used to represent data using a sparse code.
    Solves the optimization problem:
        argmin \sum_{i=1}^M || y_i - Ax_i ||_2^2 such that ||x_i||_0 <= k_0 for all 1 <= i <= M
        (A,{x_i}_{i=1}^M)

    Parameters:
    ------------
        Y : array-like, shape (n_samples, n_features)
            Training vector, where n_samples in the number of samples
            and n_features is the number of features.
        n_components : int,
            number of dictionary elements to extract
        max_iter : int,
            maximum number of iterations to perform
        tol : float,
            tolerance for numerical error
        dict_init : array of shape (n_components, n_features),
            initial values for the dictionary, for warm restart
        code_init : array of shape (n_samples, n_components),
            Initial value for the sparse code for warm restart scenarios.
        n_jobs : int, optional
            Number of parallel jobs to run.
    Returns:
    ---------
        dictionary : array of shape (n_components, n_features),
            The dictionary factor in the matrix factorization.
        code : array of shape (n_samples, n_components)
            The sparse code factor in the matrix factorization.
        errors : array
            Vector of errors at each iteration.
        n_iter : int
            Number of iterations run. Returned only if `return_n_iter` is
            set to True.
    """

    if dict_init is None:
        A = Y[:n_components, :]
    else:
        A = dict_init
    A = np.dot(A, np.diag(1. / np.sqrt(np.diag(np.dot(A.T, A)))))

    if code_init is None:
        X = np.zeros((Y.shape[0], A.shape[1]))
    else:
        X = code_init

    Y = Y.T
    X = X.T
    A = A.T

    errors = [np.linalg.norm(Y - A.dot(X), 'fro')]
    k = -1
    for k in range(max_iter):
        X = sparse_encode(Y.T, A.T, algorithm='omp', n_nonzero_coefs=k0, n_jobs=n_jobs).T

        for j in range(n_components):
            x = X[j, :] != 0
            if np.sum(x) == 0:
                continue
            X[j, x] = 0

            error = Y[:, x] - np.dot(A, X[:, x])
            U, s, V = np.linalg.svd(error)
            A[:, j] = U[:, 0]
            X[j, x] = s[0] * V.T[:, 0]

        errors.append(np.linalg.norm(Y - A.dot(X), 'fro'))
        if np.abs(errors[-1] - errors[-2]) < tol:
            break

    return A.T, X, errors, k + 1


class KSVD(BaseEstimator, SparseCodingMixin):
    """ K-SVD
    Finds a dictionary that can be used to represent data using a sparse code.
    Solves the optimization problem:
        argmin \sum_{i=1}^M || y_i - Ax_i ||_2^2 such that ||x_i||_0 <= k_0 for all 1 <= i <= M
        (A,{x_i}_{i=1}^M)

    Parameters
    ----------
        n_components : int,
            number of dictionary elements to extract
        k0 : int,
            number of non-zero elements of sparse coding
        max_iter : int,
            maximum number of iterations to perform
        tol : float,
            tolerance for numerical error
        transform_algorithm : {'lasso_lars', 'lasso_cd', 'lars', 'omp', 'threshold'}
            Algorithm used to transform the data
            lars: uses the least angle regression method (linear_model.lars_path)
            lasso_lars: uses Lars to compute the Lasso solution
            lasso_cd: uses the coordinate descent method to compute the
            Lasso solution (linear_model.Lasso). lasso_lars will be faster if
            the estimated components are sparse.
            omp: uses orthogonal matching pursuit to estimate the sparse solution
            threshold: squashes to zero all coefficients less than alpha from
            the projection ``dictionary * X'``
            .. versionadded:: 0.17
               *lasso_cd* coordinate descent method to improve speed.
        transform_n_nonzero_coefs : int, ``0.1 * n_features`` by default
            Number of nonzero coefficients to target in each column of the
            solution. This is only used by `algorithm='lars'` and `algorithm='omp'`
            and is overridden by `alpha` in the `omp` case.
        transform_alpha : float, 1. by default
            If `algorithm='lasso_lars'` or `algorithm='lasso_cd'`, `alpha` is the
            penalty applied to the L1 norm.
            If `algorithm='threshold'`, `alpha` is the absolute value of the
            threshold below which coefficients will be squashed to zero.
            If `algorithm='omp'`, `alpha` is the tolerance parameter: the value of
            the reconstruction error targeted. In this case, it overrides
            `n_nonzero_coefs`.
        n_jobs : int,
            number of parallel jobs to run
        split_sign : bool, False by default
            Whether to split the sparse feature vector into the concatenation of
            its negative part and its positive part. This can improve the
            performance of downstream classifiers.
        random_state : int, RandomState instance or None, optional (default=None)
            If int, random_state is the seed used by the random number generator;
            If RandomState instance, random_state is the random number generator;
            If None, the random number generator is the RandomState instance used
            by `np.random`.

    Attributes
    ----------
        components_ : array, [n_components, n_features]
            dictionary atoms extracted from the data
        error_ : array
            vector of errors at each iteration
        n_iter_ : int
            Number of iterations run.
    **References:**
        Elad, Michael, and Michal Aharon.
        "Image denoising via sparse and redundant representations over learned dictionaries."
        IEEE Transactions on Image processing 15.12 (2006): 3736-3745.
    ----------

    """

    def __init__(self, n_components=None, k0=None, max_iter=1000, tol=1e-8,
                 transform_algorithm='omp', transform_n_nonzero_coefs=None,
                 transform_alpha=None, n_jobs=1,
                 split_sign=False, random_state=None):
        self._set_sparse_coding_params(n_components, transform_algorithm,
                                       transform_n_nonzero_coefs,
                                       transform_alpha, split_sign, n_jobs)
        self.k0 = k0
        self.max_iter = max_iter
        self.tol = tol
        self.random_state = random_state

    def fit(self, X, y=None):
        """Fit the model from data in X.
        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            Training vector, where n_samples in the number of samples
            and n_features is the number of features.
        y : Ignored
        Returns
        -------
        self : object
            Returns the object itself
        """

        # Turn seed into a np.random.RandomState instance
        random_state = check_random_state(self.random_state)

        # Input validation on an array, list, sparse matrix or similar.
        # By default, the input is converted to an at least 2D numpy array. If the dtype of the array is object, attempt converting to float, raising on failure.
        X = check_array(X)
        n_samples, n_features = X.shape
        if self.n_components is None:
            n_components = X.shape[1]
        else:
            n_components = self.n_components

        if self.k0 is None:
            k0 = n_features
        elif self.k0 > n_features:
            k0 = n_features
        else:
            k0 = self.k0

        # initialize code
        code_init = random_state.rand(n_samples, n_components)
        # initialize dictionary
        dict_init = random_state.rand(n_components, n_features)

        self.components_, code, self.error_, self.n_iter_ = _ksvd(
            X, n_components, k0,
            tol=self.tol, max_iter=self.max_iter,
            code_init=code_init,
            dict_init=dict_init,
            n_jobs=self.n_jobs)

        return self
