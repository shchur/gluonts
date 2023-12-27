# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

from typing import Callable, Dict, List, Optional, Tuple, Type

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import (
    Beta,
    Distribution,
    Gamma,
    Laplace,
    Normal,
    Poisson,
)

from gluonts.core.component import validated
from gluonts.model.forecast_generator import (
    DistributionForecastGenerator,
    ForecastGenerator,
)
from gluonts.torch.distributions import AffineTransformed
from gluonts.torch.modules.lambda_layer import LambdaLayer


class PtArgProj(nn.Module):
    r"""
    A PyTorch module that can be used to project from a dense layer
    to PyTorch distribution arguments.

    Parameters
    ----------
    in_features
        Size of the incoming features.
    dim_args
        Dictionary with string key and int value
        dimension of each arguments that will be passed to the domain
        map, the names are not used.
    domain_map
        Function returning a tuple containing one tensor
        a function or a nn.Module. This will be called with num_args
        arguments and should return a tuple of outputs that will be
        used when calling the distribution constructor.
    """

    def __init__(
        self,
        in_features: int,
        args_dim: Dict[str, int],
        domain_map: Callable[..., Tuple[torch.Tensor]],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.args_dim = args_dim
        self.proj = nn.ModuleList(
            [nn.Linear(in_features, dim) for dim in args_dim.values()]
        )
        self.domain_map = domain_map

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor]:
        params_unbounded = [proj(x) for proj in self.proj]

        return self.domain_map(*params_unbounded)


class Output:
    """
    Class to connect a network to some output.
    """

    in_features: int
    args_dim: Dict[str, int]
    _dtype: Type = np.float32

    @property
    def dtype(self):
        return self._dtype

    @dtype.setter
    def dtype(self, dtype: Type):
        self._dtype = dtype

    def get_args_proj(self, in_features: int) -> nn.Module:
        return PtArgProj(
            in_features=in_features,
            args_dim=self.args_dim,
            domain_map=LambdaLayer(self.domain_map),
        )

    def domain_map(self, *args: torch.Tensor):
        raise NotImplementedError()

    @property
    def forecast_generator(self) -> ForecastGenerator:
        raise NotImplementedError()


class DistributionOutput(Output):
    r"""
    Class to construct a distribution given the output of a network.
    """

    distr_cls: type

    @validated()
    def __init__(self) -> None:
        pass

    def _base_distribution(self, distr_args):
        return self.distr_cls(*distr_args)

    def distribution(
        self,
        distr_args,
        loc: Optional[torch.Tensor] = None,
        scale: Optional[torch.Tensor] = None,
    ) -> Distribution:
        r"""
        Construct the associated distribution, given the collection of
        constructor arguments and, optionally, a scale tensor.

        Parameters
        ----------
        distr_args
            Constructor arguments for the underlying Distribution type.
        loc
            Optional tensor, of the same shape as the
            batch_shape+event_shape of the resulting distribution.
        scale
            Optional tensor, of the same shape as the
            batch_shape+event_shape of the resulting distribution.
        """
        distr = self._base_distribution(distr_args)
        if loc is None and scale is None:
            return distr
        else:
            return AffineTransformed(distr, loc=loc, scale=scale)

    @property
    def event_shape(self) -> Tuple:
        r"""
        Shape of each individual event contemplated by the distributions
        that this object constructs.
        """
        raise NotImplementedError()

    @property
    def event_dim(self) -> int:
        r"""
        Number of event dimensions, i.e., length of the `event_shape` tuple,
        of the distributions that this object constructs.
        """
        return len(self.event_shape)

    @property
    def value_in_support(self) -> float:
        r"""
        A float that will have a valid numeric value when computing the
        log-loss of the corresponding distribution. By default 0.0.
        This value will be used when padding data series.
        """
        return 0.0

    def domain_map(self, *args: torch.Tensor):
        r"""
        Converts arguments to the right shape and domain. The domain depends
        on the type of distribution, while the correct shape is obtained by
        reshaping the trailing axis in such a way that the returned tensors
        define a distribution of the right event_shape.
        """
        raise NotImplementedError()

    @property
    def forecast_generator(self) -> ForecastGenerator:
        return DistributionForecastGenerator(self)


class NormalOutput(DistributionOutput):
    args_dim: Dict[str, int] = {"loc": 1, "scale": 1}
    distr_cls: type = Normal

    @classmethod
    def domain_map(cls, loc: torch.Tensor, scale: torch.Tensor):  # type: ignore
        scale = F.softplus(scale)
        return loc.squeeze(-1), scale.squeeze(-1)

    @property
    def event_shape(self) -> Tuple:
        return ()


class LaplaceOutput(DistributionOutput):
    args_dim: Dict[str, int] = {"loc": 1, "scale": 1}
    distr_cls: type = Laplace

    @classmethod
    def domain_map(cls, loc: torch.Tensor, scale: torch.Tensor):  # type: ignore
        scale = F.softplus(scale)
        return loc.squeeze(-1), scale.squeeze(-1)

    @property
    def event_shape(self) -> Tuple:
        return ()


class BetaOutput(DistributionOutput):
    args_dim: Dict[str, int] = {"concentration1": 1, "concentration0": 1}
    distr_cls: type = Beta

    @classmethod
    def domain_map(  # type: ignore
        cls, concentration1: torch.Tensor, concentration0: torch.Tensor
    ):
        epsilon = np.finfo(cls._dtype).eps  # machine epsilon
        concentration1 = F.softplus(concentration1) + epsilon
        concentration0 = F.softplus(concentration0) + epsilon
        return concentration1.squeeze(dim=-1), concentration0.squeeze(dim=-1)

    @property
    def event_shape(self) -> Tuple:
        return ()

    @property
    def value_in_support(self) -> float:
        return 0.5


class GammaOutput(DistributionOutput):
    args_dim: Dict[str, int] = {"concentration": 1, "rate": 1}
    distr_cls: type = Gamma

    @classmethod
    def domain_map(cls, concentration: torch.Tensor, rate: torch.Tensor):  # type: ignore
        epsilon = np.finfo(cls._dtype).eps  # machine epsilon
        concentration = F.softplus(concentration) + epsilon
        rate = F.softplus(rate) + epsilon
        return concentration.squeeze(dim=-1), rate.squeeze(dim=-1)

    @property
    def event_shape(self) -> Tuple:
        return ()

    @property
    def value_in_support(self) -> float:
        return 0.5


class PoissonOutput(DistributionOutput):
    args_dim: Dict[str, int] = {"rate": 1}
    distr_cls: type = Poisson

    @classmethod
    def domain_map(cls, rate: torch.Tensor):  # type: ignore
        rate_pos = F.softplus(rate).clone()
        return (rate_pos.squeeze(-1),)

    # Overwrites the parent class method. We cannot scale using the affine
    # transformation since Poisson should return integers. Instead we scale
    # the parameters.
    def distribution(
        self,
        distr_args,
        loc: Optional[torch.Tensor] = None,
        scale: Optional[torch.Tensor] = None,
    ) -> Distribution:
        (rate,) = distr_args

        if scale is not None:
            rate *= scale

        return Poisson(rate=rate)

    @property
    def event_shape(self) -> Tuple:
        return ()
