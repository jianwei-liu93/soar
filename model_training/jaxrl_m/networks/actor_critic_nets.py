from typing import Optional

import distrax
import flax.linen as nn
import jax.numpy as jnp

from jaxrl_m.common.common import default_init


class ValueCritic(nn.Module):
    encoder: nn.Module
    network: nn.Module
    init_final: Optional[float] = None

    @nn.compact
    def __call__(self, observations: jnp.ndarray, train: bool = False) -> jnp.ndarray:
        outputs = self.network(self.encoder(observations), train=train)
        if self.init_final is not None:
            value = nn.Dense(
                1,
                kernel_init=nn.initializers.uniform(-self.init_final, self.init_final),
            )(outputs)
        else:
            value = nn.Dense(1, kernel_init=default_init())(outputs)
        return jnp.squeeze(value, -1)


class Critic(nn.Module):
    encoder: Optional[nn.Module]
    network: nn.Module
    init_final: Optional[float] = None
    network_separate_action_input: bool = False  # for PTR, input action to every layer

    @nn.compact
    def __call__(
        self, observations: jnp.ndarray, actions: jnp.ndarray, train: bool = False
    ) -> jnp.ndarray:
        if self.encoder is None:
            obs_enc = observations
        else:
            obs_enc = self.encoder(observations)

        if self.network_separate_action_input:
            outputs = self.network(obs_enc, actions, train=train)
        else:
            inputs = jnp.concatenate([obs_enc, actions], -1)
            outputs = self.network(inputs, train=train)
        if self.init_final is not None:
            value = nn.Dense(
                1,
                kernel_init=nn.initializers.uniform(-self.init_final, self.init_final),
            )(outputs)
        else:
            value = nn.Dense(1, kernel_init=default_init())(outputs)
        return jnp.squeeze(value, -1)


def ensemblize(cls, num_qs, out_axes=0):
    return nn.vmap(
        cls,
        variable_axes={"params": 0},
        split_rngs={"params": True},
        in_axes=None,
        out_axes=out_axes,
        axis_size=num_qs,
    )


class Policy(nn.Module):
    encoder: Optional[nn.Module]
    network: nn.Module
    action_dim: int
    init_final: Optional[float] = None
    std_parameterization: str = "exp"  # "exp", "softplus", "fixed", or "uniform"
    std_min: Optional[float] = 1e-5
    std_max: Optional[float] = 10.0
    tanh_squash_distribution: bool = False
    fixed_std: Optional[jnp.ndarray] = None

    @nn.compact
    def __call__(
        self, observations: jnp.ndarray, temperature: float = 1.0, train: bool = False
    ) -> distrax.Distribution:
        if self.encoder is None:
            obs_enc = observations
        else:
            obs_enc = self.encoder(observations)

        outputs = self.network(obs_enc, train=train)

        means = nn.Dense(self.action_dim, kernel_init=default_init())(outputs)
        if self.fixed_std is None:
            if self.std_parameterization == "exp":
                log_stds = nn.Dense(self.action_dim, kernel_init=default_init())(
                    outputs
                )
                stds = jnp.exp(log_stds)
            elif self.std_parameterization == "softplus":
                stds = nn.Dense(self.action_dim, kernel_init=default_init())(outputs)
                stds = nn.softplus(stds)
            elif self.std_parameterization == "uniform":
                log_stds = self.param(
                    "log_stds", nn.initializers.zeros, (self.action_dim,)
                )
                stds = jnp.exp(log_stds)
            else:
                raise ValueError(
                    f"Invalid std_parameterization: {self.std_parameterization}"
                )
        else:
            assert self.std_parameterization == "fixed"
            stds = jnp.array(self.fixed_std)

        # Clip stds to avoid numerical instability
        # For a normal distribution under MaxEnt, optimal std scales with sqrt(temperature)
        stds = jnp.clip(stds, self.std_min, self.std_max) * jnp.sqrt(temperature)
        # stds = jnp.concatenate([stds[:, :6], jnp.ones((len(stds), 1)) * jnp.log(0.3)], axis=-1)

        if self.tanh_squash_distribution:
            distribution = TanhMultivariateNormalDiag(
                loc=means,
                scale_diag=stds,
            )
        else:
            distribution = distrax.MultivariateNormalDiag(
                loc=means,
                scale_diag=stds,
            )

        return distribution


class TanhMultivariateNormalDiag(distrax.Transformed):
    def __init__(
        self,
        loc: jnp.ndarray,
        scale_diag: jnp.ndarray,
        low: Optional[jnp.ndarray] = None,
        high: Optional[jnp.ndarray] = None,
    ):
        distribution = distrax.MultivariateNormalDiag(loc=loc, scale_diag=scale_diag)

        layers = []

        if not (low is None or high is None):

            def rescale_from_tanh(x):
                x = (x + 1) / 2  # (-1, 1) => (0, 1)
                return x * (high - low) + low

            def forward_log_det_jacobian(x):
                high_ = jnp.broadcast_to(high, x.shape)
                low_ = jnp.broadcast_to(low, x.shape)
                return jnp.sum(jnp.log(0.5 * (high_ - low_)), -1)

            layers.append(
                distrax.Lambda(
                    rescale_from_tanh,
                    forward_log_det_jacobian=forward_log_det_jacobian,
                    event_ndims_in=1,
                    event_ndims_out=1,
                )
            )

        layers.append(distrax.Block(distrax.Tanh(), 1))

        bijector = distrax.Chain(layers)

        super().__init__(distribution=distribution, bijector=bijector)

    def mode(self) -> jnp.ndarray:
        return self.bijector.forward(self.distribution.mode())

    def stddev(self) -> jnp.ndarray:
        return self.bijector.forward(self.distribution.stddev())
