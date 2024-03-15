import warnings
from typing import Any, Dict, Literal, Optional, Sequence, Union, cast

import numpy as np
import pandas as pd
import pymc as pm
import pytensor
import pytensor.tensor as pt
import xarray
from numpy import exp, log
from pymc.util import RandomState
from pytensor.compile import Mode, get_default_mode
from pytensor.graph import Constant, node_rewriter
from pytensor.scalar import Grad2F1Loop
from pytensor.tensor.elemwise import Elemwise
from scipy.special import betaln, gammaln, hyp2f1
from xarray_einstats.stats import logsumexp as xr_logsumexp

from pymc_marketing.clv.distributions import ParetoNBD
from pymc_marketing.clv.models.basic import CLVModel
from pymc_marketing.clv.utils import to_xarray


@node_rewriter([Elemwise])
def local_reduce_max_num_iters_hyp2f1_grad(fgraph, node):
    """Rewrite that reduces the maximum number of iterations in the hyp2f1 grad scalar loop.

    This is critical to get NUTS to converge in the beginning.
    Otherwise, it can take a long time to get started.
    """
    if not isinstance(node.op.scalar_op, Grad2F1Loop):
        return
    max_steps, *other_args = node.inputs

    # max_steps = switch(skip_loop, 0, 1e6) by default
    if max_steps.owner and max_steps.owner.op == pt.switch:
        cond, zero, max_steps_const = max_steps.owner.inputs
        if (isinstance(zero, Constant) and np.all(zero.data == 0)) and (
            isinstance(max_steps_const, Constant)
            and np.all(max_steps_const.data == 1e6)
        ):
            new_max_steps = pt.switch(cond, zero, np.array(int(1e5), dtype="int32"))
            return node.op.make_node(new_max_steps, *other_args).outputs


pytensor.compile.optdb["specialize"].register(
    "local_reduce_max_num_iters_hyp2f1_grad",
    local_reduce_max_num_iters_hyp2f1_grad,
    use_db_name_as_tag=False,  # Not included by default
)


class ParetoNBDModel(CLVModel):
    """Pareto Negative Binomial Model (Pareto/NBD).

    Model for continuous, non-contractual customers, first introduced by Schmittlein, et al. [1]_,
    with additional derivations and predictive methods by Hardie & Fader [2]_ [3]_ [4]_.

    The Pareto/NBD model assumes the time duration a customer is active follows a Gamma distribution,
    and time between purchases is also Gamma-distributed while the customer is still active.

    This model requires data to be summarized by recency, frequency, and T for each customer,
    using `clv.rfm_summary()` or equivalent. Covariates impacting customer dropouts and transaction rates are optional.

    Parameters
    ----------
    data: pd.DataFrame
        DataFrame containing the following columns:
            * `frequency`: number of repeat purchases
            * `recency`: time between the first and the last purchase
            * `T`: time between the first purchase and the end of the observation period; model assumptions require T >= recency
            * `customer_id`: unique customer identifier
        Along with optional covariate columns.

    model_config: dict, optional
        Dictionary containing model parameters and covariate column names:
            * `r_prior`: Shape parameter of time between purchases; defaults to `Weibull(alpha=2, beta=1)`
            * `alpha_prior`: Scale parameter of time between purchases; defaults to `Weibull(alpha=2, beta=10)`
            * `s_prior`: Shape parameter of time until dropout; defaults to `Weibull(alpha=2, beta=1)`
            * `beta_prior`: Scale parameter of time until dropout; defaults to `Weibull(alpha=2, beta=10)`
            * `purchase_covariates_prior`: Coefficients for purchase rate covariates; defaults to `Normal(0, 3)`
            * `dropout_covariates_prior`: Coefficients for dropout covariates; defaults to `Normal.dist(0, 3)`
            * `purchase_covariate_cols`: List containing column names of covariates for customer purchase rates.
            * `dropout_covariate_cols:`: List containing column names of covariates for customer dropouts.

        If not provided, the model will use default priors specified in the `default_model_config` class attribute.
    sampler_config: dict, optional
        Dictionary of sampler parameters. Defaults to None.

    Examples
    --------
        .. code-block:: python

            import pymc as pm
            from pymc_marketing.clv import ParetoNBDModel, rfm_summary

            rfm_df = rfm_summary(raw_data,'id_col_name','date_col_name')

            # Initialize model with customer data; `model_config` parameter is optional
            model = ParetoNBDModel(
                data=rfm_df,
                model_config={
                    "r_prior": {"dist": "Weibull", "kwargs": {"alpha": 2, "beta": 1}},
                    "alpha_prior": {"dist": "Weibull", "kwargs": {"alpha": 2, "beta": 10}},
                    "s_prior": {"dist": "Weibull", "kwargs": {"alpha": 2, "beta": 1}},
                    "beta_prior": {"dist": "Weibull", "kwargs": {"alpha": 2, "beta": 10}},
                },
            )

            # Fit model quickly to large datasets via the default Maximum a Posteriori method
            model.fit(fit_method='map')
            print(model.fit_summary())

            # Use 'mcmc' for more informative predictions and reliable performance on smaller datasets
            model.fit(fit_method='mcmc')
            print(model.fit_summary())

            # Predict number of purchases for customers over the next 10 time periods
            expected_purchases = model.expected_purchases(
                data=rfm_df,
                future_t=10,
            )

            # Predict probability of customer making 'n' purchases over 't' time periods
            # Data parameter is omitted here because predictions are ran on original dataset
            expected_num_purchases = model.expected_purchase_probability(
                n=[0, 1, 2, 3],
                future_t=[10,20,30,40],
            )

            new_data = pd.DataFrame(
                data = {
                "customer_id": [0, 1, 2, 3],
                "frequency": [5, 2, 1, 8],
                "recency": [7, 4, 2.5, 11],
                "T": [10, 8, 10, 22]
                }
            )

            # Predict probability customers will still be active in 'future_t' time periods
            probability_alive = model.expected_probability_alive(
                data=new_data,
                future_t=[0, 3, 6, 9],
            )

            # Predict number of purchases for a new customer over 't' time periods.
            expected_purchases_new_customer = model.expected_purchases_new_customer(
                t=[2, 5, 7, 10],
            )

    References
    ----------
    .. [1] David C. Schmittlein, Donald G. Morrison and Richard Colombo.
           "Counting Your Customers: Who Are They and What Will They Do Next".
           Management Science,Vol. 33, No. 1 (Jan., 1987), pp. 1-24.
    .. [2] Fader, Peter & G. S. Hardie, Bruce (2005).
           "A Note on Deriving the Pareto/NBD Model and Related Expressions".
           http://brucehardie.com/notes/009/pareto_nbd_derivations_2005-11-05.pdf
    .. [3] Fader, Peter & G. S. Hardie, Bruce (2014).
           "Additional Results for the Pareto/NBD Model".
           https://www.brucehardie.com/notes/015/additional_pareto_nbd_results.pdf
    .. [4] Fader, Peter & G. S. Hardie, Bruce (2014).
           "Deriving the Conditional PMF of the Pareto/NBD Model".
           https://www.brucehardie.com/notes/028/pareto_nbd_conditional_pmf.pdf
    .. [5] Fader, Peter & G. S. Hardie, Bruce (2007).
           "Incorporating Time-Invariant Covariates into the Pareto/NBD and BG/NBD Models".
           https://www.brucehardie.com/notes/019/time_invariant_covariates.pdf
    """

    _model_type = "Pareto/NBD"  # Pareto Negative-Binomial Distribution

    def __init__(
        self,
        data: pd.DataFrame,
        *,
        model_config: Optional[Dict] = None,
        sampler_config: Optional[Dict] = None,
    ):
        super().__init__(
            data=data,
            model_config=model_config,
            sampler_config=sampler_config,
        )
        self.purchase_covariate_cols = list(
            self.model_config["purchase_covariate_cols"]
        )
        self.dropout_covariate_cols = list(self.model_config["dropout_covariate_cols"])
        self.covariate_cols = self.purchase_covariate_cols + self.dropout_covariate_cols
        self._validate_cols(
            data,
            required_cols=["customer_id", "frequency", "recency", "T"]
            + self.covariate_cols,
            must_be_unique=["customer_id"],
        )

    @property
    def default_model_config(self) -> Dict[str, Any]:
        return {
            "r_prior": {"dist": "Weibull", "kwargs": {"alpha": 2, "beta": 1}},
            "alpha_prior": {"dist": "Weibull", "kwargs": {"alpha": 2, "beta": 10}},
            "s_prior": {"dist": "Weibull", "kwargs": {"alpha": 2, "beta": 1}},
            "beta_prior": {"dist": "Weibull", "kwargs": {"alpha": 2, "beta": 10}},
            "purchase_coefficient_prior": {
                "dist": "Normal",
                "kwargs": {"mu": 0, "sigma": 1},
            },
            "dropout_coefficient_prior": {
                "dist": "Normal",
                "kwargs": {"mu": 0, "sigma": 1},
            },
            "purchase_covariate_cols": [],
            "dropout_covariate_cols": [],
        }

    def build_model(self) -> None:  # type: ignore[override]
        r_prior = self._create_distribution(self.model_config["r_prior"])
        alpha_scale_prior = self._create_distribution(self.model_config["alpha_prior"])
        s_prior = self._create_distribution(self.model_config["s_prior"])
        beta_scale_prior = self._create_distribution(self.model_config["beta_prior"])
        if self.purchase_covariate_cols:
            purchase_coefficient_prior = self._create_distribution(
                self.model_config["purchase_coefficient_prior"],
                shape=len(self.purchase_covariate_cols),
            )
        if self.dropout_covariate_cols:
            dropout_coefficient_prior = self._create_distribution(
                self.model_config["dropout_coefficient_prior"],
                shape=len(self.dropout_covariate_cols),
            )

        coords = {
            "purchase_covariate": self.purchase_covariate_cols,
            "dropout_covariate": self.dropout_covariate_cols,
            "obs_var": ["recency", "frequency"],
        }
        mutable_coords = {"customer_id": self.data["customer_id"]}
        with pm.Model(coords=coords, coords_mutable=mutable_coords) as self.model:
            r = self.model.register_rv(r_prior, name="r")

            if self.purchase_covariate_cols:
                purchase_data = pm.MutableData(
                    "purchase_data",
                    self.data[self.purchase_covariate_cols],
                    dims=["customer_id", "purchase_covariate"],
                )

                purchase_coefficient = self.model.register_rv(
                    purchase_coefficient_prior,
                    name="purchase_coefficient",
                    dims=["purchase_covariate"],
                )

                alpha_scale = self.model.register_rv(
                    alpha_scale_prior, name="alpha_scale"
                )
                alpha = pm.Deterministic(
                    "alpha",
                    (
                        alpha_scale
                        * pm.math.exp(-pm.math.dot(purchase_data, purchase_coefficient))
                    ),
                    dims="customer_id",
                )
            else:
                alpha = self.model.register_rv(alpha_scale_prior, name="alpha")

            # churn priors
            s = self.model.register_rv(s_prior, name="s")
            if self.dropout_covariate_cols:
                dropout_data = pm.MutableData(
                    "dropout_data",
                    self.data[self.dropout_covariate_cols],
                    dims=["customer_id", "dropout_covariate"],
                )
                dropout_coefficient = self.model.register_rv(
                    dropout_coefficient_prior,
                    name="dropout_coefficient",
                    dims=["dropout_covariate"],
                )

                beta_scale = self.model.register_rv(beta_scale_prior, name="beta_scale")
                beta = pm.Deterministic(
                    "beta",
                    (
                        beta_scale
                        * pm.math.exp(-pm.math.dot(dropout_data, dropout_coefficient))
                    ),
                    dims="customer_id",
                )
            else:
                beta = self.model.register_rv(beta_scale_prior, name="beta")

            ParetoNBD(
                name="recency_frequency",
                r=r,
                alpha=alpha,
                s=s,
                beta=beta,
                T=self.data["T"],
                observed=np.stack(
                    (self.data["recency"], self.data["frequency"]), axis=1
                ),
                dims=["customer_id", "obs_var"],
            )

    def fit(self, fit_method: str = "map", **kwargs):  # type: ignore
        """Infer posteriors of model parameters to run predictions.

        Parameters
        ----------
        fit_method: str
            Method used to fit the model. Options are:
            * "map": Posterior point estimates via Maximum a Posteriori (default)
            * "mcmc": Full posterior distributions via No U-Turn Sampler (NUTS)
        kwargs:
            Other keyword arguments passed to the underlying PyMC routines
        """

        mode = get_default_mode()
        if fit_method == "mcmc":
            # Include rewrite in mode
            opt_qry = mode.provided_optimizer.including(
                "local_reduce_max_num_iters_hyp2f1_grad"
            )
            mode = Mode(linker=mode.linker, optimizer=opt_qry)

        with pytensor.config.change_flags(mode=mode, on_opt_error="raise"):
            # Suppress annoying warning
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    message="Optimization Warning: The Op hyp2f1 does not provide a C implementation. As well as being potentially slow, this also disables loop fusion.",
                    action="ignore",
                    category=UserWarning,
                )
                return super().fit(fit_method, **kwargs)

    @staticmethod
    def _logp(
        r: xarray.DataArray,
        alpha: xarray.DataArray,
        s: xarray.DataArray,
        beta: xarray.DataArray,
        x: xarray.DataArray,
        t_x: xarray.DataArray,
        T: xarray.DataArray,
    ) -> xarray.DataArray:
        """
        Utility function for using ParetoNBD log-likelihood in predictive methods.
        """
        # Add one dummy dimension to the right of the scalar parameters, so they broadcast with the `T` vector
        pareto_dist = ParetoNBD.dist(
            alpha=alpha.values[..., None]
            if "customer_id" not in alpha.dims
            else alpha.values,
            r=r.values[..., None],
            beta=beta.values[..., None]
            if "customer_id" not in beta.dims
            else beta.values,
            s=s.values[..., None],
            T=T.values,
        )
        values = np.vstack((t_x.values, x.values)).T
        # TODO: Instead of compiling this function everytime this method is called
        #  we could compile it once (with mutable inputs) and cache it for reuse with new inputs.
        loglike = pm.logp(pareto_dist, values).eval()
        return xarray.DataArray(data=loglike, dims=("chain", "draw", "customer_id"))

    def _extract_predictive_variables(
        self,
        data: pd.DataFrame,
        customer_varnames: Sequence[str] = (),
    ) -> xarray.Dataset:
        """Utility function assigning default customer arguments
        for predictive methods and converting to xarrays.
        """
        self._validate_cols(
            data,
            required_cols=[
                "customer_id",
                *customer_varnames,
                *self.purchase_covariate_cols,
                *self.dropout_covariate_cols,
            ],
            must_be_unique=["customer_id"],
        )

        if self.purchase_covariate_cols:
            purchase_xarray = xarray.DataArray(
                data[self.purchase_covariate_cols],
                dims=["customer_id", "purchase_covariate"],
            )
            alpha_scale = self.fit_result["alpha_scale"]
            purchase_coefficient = self.fit_result["purchase_coefficient"]
            alpha = alpha_scale * np.exp(
                -xarray.dot(
                    purchase_coefficient, purchase_xarray, dims="purchase_covariate"
                )
            )
            alpha.name = "alpha"
        else:
            alpha = self.fit_result["alpha"]

        if self.dropout_covariate_cols:
            dropout_xarray = xarray.DataArray(
                data[self.dropout_covariate_cols],
                dims=["customer_id", "dropout_covariate"],
            )
            beta_scale = self.fit_result["beta_scale"]
            dropout_coefficient = self.fit_result["dropout_coefficient"]
            beta = beta_scale * np.exp(
                -xarray.dot(
                    dropout_coefficient, dropout_xarray, dims="dropout_covariate"
                )
            )
            beta.name = "beta"
        else:
            beta = self.fit_result["beta"]

        r = self.fit_result["r"]
        s = self.fit_result["s"]

        customer_vars = to_xarray(
            data["customer_id"],
            *[data[customer_varname] for customer_varname in customer_varnames],
        )
        if len(customer_varnames) == 1:
            customer_vars = [customer_vars]

        return xarray.combine_by_coords(
            (
                r,
                alpha,
                s,
                beta,
                *customer_vars,
            )
        )

    def expected_purchases(
        self,
        data: Optional[pd.DataFrame] = None,
        *,
        future_t: Optional[Union[int, np.ndarray, pd.Series]] = None,
    ) -> xarray.DataArray:
        """
        Given *recency*, *frequency*, and *T* for an individual customer, this method predicts the
        expected number of future purchases across *future_t* time periods.

        Adapted from equation (41) In Bruce Hardie's notes [2]_, and `lifetimes` package:
        https://github.com/CamDavidsonPilon/lifetimes/blob/41e394923ad72b17b5da93e88cfabab43f51abe2/lifetimes/fitters/pareto_nbd_fitter.py#L242

        Parameters
        ----------
        data: pd.DataFrame, optional
            Dataframe containing the following columns:
                * `customer_id`: unique customer identifier
                * `frequency`: number of repeat purchases
                * `recency`: time between the first and the last purchase
                * `T`: time between the first purchase and the end of the observation period, model assumptions require T >= recency
                * `future_t`: Number of time periods to predict expected purchases.
                * covariates: Purchase and dropout covariate columns if original model had any.
            If not provided, the method will use the fit dataset.
        future_t: array_like, optional
            Number of time periods to predict expected purchases.
            Not needed if `data` parameter is provided with a `future_t` column.

        References
        ----------
        .. [2] Fader, Peter & G. S. Hardie, Bruce (2005).
               "A Note on Deriving the Pareto/NBD Model and Related Expressions."
               http://brucehardie.com/notes/009/pareto_nbd_derivations_2005-11-05.pdf
        """
        if data is None:
            data = self.data

        if future_t is not None:
            data = data.assign(future_t=future_t)

        dataset = self._extract_predictive_variables(
            data, customer_varnames=["frequency", "recency", "T", "future_t"]
        )
        r = dataset["r"]
        alpha = dataset["alpha"]
        s = dataset["s"]
        beta = dataset["beta"]
        x = dataset["frequency"]
        t_x = dataset["recency"]
        T = dataset["T"]
        future_t = dataset["future_t"]

        loglike = self._logp(r, alpha, s, beta, x, t_x, T)

        first_term = (
            gammaln(r + x)
            - gammaln(r)
            + r * log(alpha)
            + s * log(beta)
            - (r + x) * log(alpha + T)
            - s * log(beta + T)
        )
        second_term = log(r + x) + log(beta + T) - log(alpha + T)
        third_term = log(
            (1 - ((beta + T) / (beta + T + future_t)) ** (s - 1)) / (s - 1)
        )

        exp_purchases = exp(first_term + second_term + third_term - loglike)

        return exp_purchases.transpose(
            "chain", "draw", "customer_id", missing_dims="ignore"
        )

    def expected_probability_alive(
        self,
        data: Optional[pd.DataFrame] = None,
        *,
        future_t: Optional[Union[int, np.ndarray, pd.Series]] = None,
    ) -> xarray.DataArray:
        """
        Compute the probability that a customer with history *frequency*, *recency*, and *T*
        is currently active. Can also estimate alive probability for *future_t* periods into the future.

        Adapted from equation (18) in Bruce Hardie's notes [3]_.

        Parameters
        ----------
        data: pd.DataFrame, optional
            Dataframe containing the following columns:
                * `customer_id`: unique customer identifier
                * `frequency`: number of repeat purchases
                * `recency`: time between the first and the last purchase
                * `T`: time between the first purchase and the end of the observation period, model assumptions require T >= recency
                * `future_t`: Number of time periods in the future to estimate alive probability; defaults to 0.
                * covariates: Purchase and dropout covariate columns if original model had any.
            If not provided, the method will use the fit dataset.
        future_t: array_like, optional
            Number of time periods in the future to estimate alive probability; defaults to 0.
            Not needed if `data` parameter is provided with a `future_t` column.

        References
        ----------
        .. [3] Fader, Peter & G. S. Hardie, Bruce (2014).
               "Additional Results for the Pareto/NBD Model."
               https://www.brucehardie.com/notes/015/additional_pareto_nbd_results.pdf
        """
        if data is None:
            data = self.data

        if future_t is not None:
            data = data.assign(future_t=future_t)

        if "future_t" not in data:
            data = data.assign(future_t=0)

        dataset = self._extract_predictive_variables(
            data, customer_varnames=["frequency", "recency", "T", "future_t"]
        )
        r = dataset["r"]
        alpha = dataset["alpha"]
        s = dataset["s"]
        beta = dataset["beta"]
        x = dataset["frequency"]
        t_x = dataset["recency"]
        T = dataset["T"]
        future_t = dataset["future_t"]

        loglike = self._logp(r, alpha, s, beta, x, t_x, T)

        term1 = gammaln(r + x) - gammaln(r)
        term2 = r * log(alpha / (alpha + T))
        term3 = -x * log(alpha + T)
        term4 = s * log(beta / (beta + T + future_t))

        prob_alive = exp(term1 + term2 + term3 + term4 - loglike)

        return prob_alive.transpose(
            "chain", "draw", "customer_id", missing_dims="ignore"
        )

    def expected_purchase_probability(
        self,
        data: Optional[pd.DataFrame] = None,
        *,
        n_purchases: Optional[int] = None,
        future_t: Optional[Union[int, np.ndarray, pd.Series]] = None,
    ) -> xarray.DataArray:
        """
        Estimate probability of *n_purchases* over *future_t* time periods,
        given an individual customer's current *frequency*, *recency*, and *T*.


        Adapted from equation (16) in Bruce Hardie's notes [4]_, and `lifetimes` package:
        https://github.com/CamDavidsonPilon/lifetimes/blob/41e394923ad72b17b5da93e88cfabab43f51abe2/lifetimes/fitters/pareto_nbd_fitter.py#L388

        Parameters
        ----------
        data: pd.DataFrame
            Optional dataframe containing the following columns:
                * `customer_id`: unique customer identifier
                * `frequency`: number of repeat purchases
                * `recency`: time between the first and the last purchase
                * `T`: time between the first purchase and the end of the observation period, model assumptions require T >= recency
                * `future_t`: Number of time periods to predict expected purchases.
                * `n_purchases`: Number of purchases to predict probability for. Currently restricted to the same number for all customers.
                * covariates: Purchase and dropout covariate columns if original model had any.
            If not provided, the method will use the fit dataset.
        n_purchases: int, optional
            Number of purchases predicted.
            Not needed if `data` parameter is provided with a `n_purchases` column.
        future_t: array_like, optional
            Time periods over which the probability should be estimated.
            Not needed if `data` parameter is provided with a `future_t` column.

        References
        ----------
        .. [4] Fader, Peter & G. S. Hardie, Bruce (2014).
               "Deriving the Conditional PMF of the Pareto/NBD Model."
               https://www.brucehardie.com/notes/028/pareto_nbd_conditional_pmf.pdf
        """
        if data is None:
            data = self.data

        if n_purchases is not None:
            data = data.assign(n_purchases=n_purchases)

        if future_t is not None:
            data = data.assign(future_t=future_t)

        dataset = self._extract_predictive_variables(
            data,
            customer_varnames=["frequency", "recency", "T", "future_t", "n_purchases"],
        )
        r = dataset["r"]
        alpha = dataset["alpha"]
        s = dataset["s"]
        beta = dataset["beta"]
        x = dataset["frequency"]
        t_x = dataset["recency"]
        T = dataset["T"]
        future_t = cast(xarray.DataArray, dataset["future_t"])
        n_purchases = cast(int, dataset["n_purchases"].values[0].item())
        if not np.all(n_purchases == dataset["n_purchases"].values):
            raise NotImplementedError(
                "expected_purchase_probability with distinct numbers of `n_purchases` not implemented"
            )

        loglike = self._logp(r, alpha, s, beta, x, t_x, T)

        _alpha_less_than_beta = alpha < beta
        min_of_alpha_beta = xarray.where(_alpha_less_than_beta, alpha, beta)
        max_of_alpha_beta = xarray.where(_alpha_less_than_beta, beta, alpha)
        p = xarray.where(_alpha_less_than_beta, r + x + n_purchases, s + 1)

        abs_alpha_beta = max_of_alpha_beta - min_of_alpha_beta

        log_p_zero = (
            gammaln(r + x)
            + r * log(alpha)
            + s * log(beta)
            - (gammaln(r) + (r + x) * log(alpha + T) + s * log(beta + T) + loglike)
        )
        log_B_one = (
            gammaln(r + x + n_purchases)
            + r * log(alpha)
            + s * log(beta)
            - (
                gammaln(r)
                + (r + x + n_purchases) * log(alpha + T + future_t)
                + s * log(beta + T + future_t)
            )
        )
        log_B_two = (
            r * log(alpha)
            + s * log(beta)
            + gammaln(r + s + x)
            + betaln(r + x + n_purchases, s + 1)
            + log(
                hyp2f1(
                    r + s + x,
                    p,
                    r + s + x + n_purchases + 1,
                    abs_alpha_beta / (max_of_alpha_beta + T),
                )
            )
            - (gammaln(r) + gammaln(s) + (r + s + x) * log(max_of_alpha_beta + T))
        )

        def _log_B_three(i):
            return (
                r * log(alpha)
                + s * log(beta)
                + gammaln(r + s + x + i)
                + betaln(r + x + n_purchases, s + 1)
                + log(
                    hyp2f1(
                        r + s + x + i,
                        p,
                        r + s + x + n_purchases + 1,
                        abs_alpha_beta / (max_of_alpha_beta + T + future_t),
                    )
                )
                - (
                    gammaln(r)
                    + gammaln(s)
                    + (r + s + x + i) * log(max_of_alpha_beta + T + future_t)
                )
            )

        zeroth_term = (n_purchases == 0) * (1 - exp(log_p_zero))

        # ignore numerical errors when future_t <= 0,
        # this is an unusual edge case in practice, so refactoring is unwarranted
        with np.errstate(divide="ignore", invalid="ignore"):
            first_term = (
                n_purchases * log(xarray.DataArray(future_t))
                - gammaln(n_purchases + 1)
                + log_B_one
                - loglike
            )
            second_term = log_B_two - loglike

            third_term = xr_logsumexp(
                xarray.concat(
                    [
                        i * log(cast(xarray.DataArray, future_t))
                        - gammaln(i + 1)
                        + _log_B_three(i)
                        - loglike
                        for i in range(n_purchases + 1)
                    ],
                    dim="concat_dim_",
                ),
                dims="concat_dim_",
            )

        purchase_prob = zeroth_term + exp(
            xr_logsumexp(
                xarray.concat([first_term, second_term, third_term], dim="_concat_dim"),
                b=xarray.DataArray(data=[1, 1, -1], dims="_concat_dim"),
                dims="_concat_dim",
            )
        )

        purchase_prob = xarray.where(
            cast(xarray.DataArray, future_t) <= 0,
            purchase_prob.fillna(0),
            purchase_prob,
        )

        return purchase_prob.transpose(
            "chain", "draw", "customer_id", missing_dims="ignore"
        )

    def expected_purchases_new_customer(
        self,
        data: Optional[pd.DataFrame] = None,
        *,
        t: Optional[Union[int, np.ndarray, pd.Series]] = None,
    ) -> xarray.DataArray:
        """
        Expected number of purchases for a new customer across *t* time periods.

        In a model with covariates, if `data` is not specified, the dataset used for fitting will be used.
        A prediction will be computed for a new customer with each set of covariates.
        This is not a conditional prediction on the observed customers!

        Adapted from equation (27) in Bruce Hardie's notes [2]_, and `lifetimes` package:
        https://github.com/CamDavidsonPilon/lifetimes/blob/41e394923ad72b17b5da93e88cfabab43f51abe2/lifetimes/fitters/pareto_nbd_fitter.py#L359

        Parameters
        ----------
        data: pd.DataFrame, optional
            Dataframe containing the following columns:
                * `customer_id`: unique customer identifier
                * `t`: Number of time periods to predict expected purchases.
                * covariates: Purchase and dropout covariate columns if original model had any.
            If not provided, the method will use the fit dataset.
        t: array_like, optional
            Number of time periods over which to estimate purchases.
            Not needed if `data` parameter is provided with a `t` column.

        References
        ----------
        .. [2] Fader, Peter & G. S. Hardie, Bruce (2005).
               "A Note on Deriving the Pareto/NBD Model and Related Expressions."
               http://brucehardie.com/notes/009/pareto_nbd_derivations_2005-11-05.pdf
        """
        if data is None:
            data = self.data

        if t is not None:
            data = data.assign(t=t)

        dataset = self._extract_predictive_variables(data, customer_varnames=["t"])
        r = dataset["r"]
        alpha = dataset["alpha"]
        s = dataset["s"]
        beta = dataset["beta"]
        t = dataset["t"]

        first_term = r * beta / alpha / (s - 1)
        second_term = 1 - (beta / (beta + t)) ** (s - 1)

        return (first_term * second_term).transpose(
            "chain", "draw", "customer_id", missing_dims="ignore"
        )

    def distribution_new_customer(
        self,
        data: Optional[pd.DataFrame] = None,
        *,
        T: Optional[Union[int, np.ndarray, pd.Series]] = None,
        random_seed: Optional[RandomState] = None,
        var_names: Sequence[
            Literal["dropout", "purchase_rate", "recency_frequency"]
        ] = (
            "dropout",
            "purchase_rate",
            "recency_frequency",
        ),
    ) -> xarray.Dataset:
        """Utility function for posterior predictive sampling of dropout, purchase rate and frequency/recency of new customers.

        In a model with covariates, if `data` is not specified, the dataset used for fitting will be used.
        A prediction will be computed for a new customer with each set of covariates.
        This is not a conditional prediction on the observed customers!

        Parameters
        ----------
        data: pd.DataFrame, Optional
            DataFrame containing the following columns:
                * `customer_id`: unique customer identifier
                * `T`: time between the first purchase and the end of the observation period.
                * covariates: Purchase and dropout covariate columns if original model had any.
            If not provided, the method will use the fit dataset.
        T: array_like, optional
            time between the first purchase and the end of the observation period.
            Not needed if `data` parameter is provided with a `T` column.
        random_seed: RandomState, optional
            Random state to use for sampling.
        var_names: Sequence[str]
            Names of the variables to sample from. Defaults to ["dropout", "purchase_rate", "recency_frequency"].

        """
        if data is None:
            data = self.data

        if T is not None:
            data = data.assign(T=T)

        dataset = self._extract_predictive_variables(data, customer_varnames=["T"])
        T = dataset["T"].values
        # Delete "T" so we can pass dataset directly to `sample_posterior_predictive`
        del dataset["T"]

        if dataset.sizes["chain"] == 1 and dataset.sizes["draw"] == 1:
            # For map fit add a dummy draw dimension
            dataset = dataset.squeeze("draw").expand_dims(draw=range(1000))

        coords = self.model.coords.copy()  # type: ignore
        coords["customer_id"] = data["customer_id"]
        with pm.Model(coords=coords) as pred_model:
            if self.purchase_covariate_cols:
                alpha = pm.Flat("alpha", dims=["customer_id"])
            else:
                alpha = pm.Flat("alpha")
            if self.dropout_covariate_cols:
                beta = pm.Flat("beta", dims=["customer_id"])
            else:
                beta = pm.Flat("beta")
            r = pm.Flat("r")
            s = pm.Flat("s")

            pm.Gamma(
                "purchase_rate",
                alpha=r,
                beta=alpha,
                dims=pred_model.named_vars_to_dims.get("alpha"),
            )
            pm.Gamma(
                "dropout",
                alpha=s,
                beta=beta,
                dims=pred_model.named_vars_to_dims.get("beta"),
            )

            ParetoNBD(
                name="recency_frequency",
                r=r,
                alpha=alpha,
                s=s,
                beta=beta,
                T=T,
                dims=["customer_id", "obs_var"],
            )

            return pm.sample_posterior_predictive(
                dataset,
                var_names=var_names,
                random_seed=random_seed,
                predictions=True,
            ).predictions

    def distribution_new_customer_dropout(
        self,
        data: Optional[pd.DataFrame] = None,
        *,
        random_seed: Optional[RandomState] = None,
    ) -> xarray.Dataset:
        """Sample from the Gamma distribution representing dropout times for new customers.

        This is the duration of time a new customer is active before churning, or dropping out.

        Parameters
        ----------
        data: pd.DataFrame, optional
            DataFrame containing the following columns:
                * `customer_id`: unique customer identifier
                * covariates: Purchase and dropout covariate columns if original model had any.
            If not provided, the method will use the fit dataset.
        random_seed: RandomState, optional
            Random state to use for sampling.

        Returns
        -------
        xr.Dataset
            Dataset containing the posterior samples for the population-level dropout rate.
        """
        return self.distribution_new_customer(
            data=data,
            random_seed=random_seed,
            var_names=["dropout"],
        )["dropout"]

    def distribution_new_customer_purchase_rate(
        self,
        data: Optional[pd.DataFrame] = None,
        *,
        random_seed: Optional[RandomState] = None,
    ) -> xarray.Dataset:
        """Sample from the Gamma distribution representing purchase rates for new customers.

        This is the purchase rate for a new customer and determines the time between
        purchases for any new customer.

        Parameters
        ----------
        data: pd.DataFrame, optional
            DataFrame containing the following columns:
                * `customer_id`: unique customer identifier
                * covariates: Purchase and dropout covariate columns if original model had any.
            If not provided, the method will use the fit dataset.
        random_seed : RandomState, optional
            Random state to use for sampling.

        Returns
        -------
        xr.Dataset
            Dataset containing the posterior samples for the population-level purchase rate.
        """
        return self.distribution_new_customer(
            data=data,
            random_seed=random_seed,
            var_names=["purchase_rate"],
        )["purchase_rate"]

    def distribution_new_customer_recency_frequency(
        self,
        data: Optional[pd.DataFrame] = None,
        *,
        T: Optional[Union[int, np.ndarray, pd.Series]] = None,
        random_seed: Optional[RandomState] = None,
    ) -> xarray.Dataset:
        """Pareto/NBD process representing purchases across the customer population.

        This is the distribution of purchase frequencies given 'T' observation periods for each customer.

        Parameters
        ----------
        data: pd.DataFrame, optional
            DataFrame containing the following columns:
                * `customer_id`: unique customer identifier
                * `T`: time between the first purchase and the end of the observation period.
                * covariates: Purchase and dropout covariate columns if original model had any.
            If not provided, the method will use the fit dataset.
        T: array_like, optional
            Number of observation periods for each customer. If not provided, T values from fit dataset will be used.
            Not needed if `data` parameter is provided with a `T` column.
        random_seed : RandomState, optional
            Random state to use for sampling.

        Returns
        -------
        xr.Dataset
            Dataset containing the posterior samples for the customer population.
        """
        return self.distribution_new_customer(
            data=data,
            T=T,
            random_seed=random_seed,
            var_names=["recency_frequency"],
        )["recency_frequency"]
