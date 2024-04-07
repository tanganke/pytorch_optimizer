import math
from typing import Optional, Tuple

import torch
from torch.optim.optimizer import Optimizer

from pytorch_optimizer.base.exception import NoSparseGradientError
from pytorch_optimizer.base.optimizer import BaseOptimizer
from pytorch_optimizer.base.types import BETAS, CLOSURE, DEFAULTS, LOSS, PARAMETERS
from pytorch_optimizer.optimizer.galore import GaLoreProjector


class AdaFactor(Optimizer, BaseOptimizer):
    r"""Adaptive Learning Rates with Sublinear Memory Cost.

    :param params: PARAMETERS. iterable of parameters to optimize or dicts defining parameter groups.
    :param lr: float. learning rate.
    :param betas: BETAS. coefficients used for computing running averages of gradient and the squared hessian trace.
    :param decay_rate: float. coefficient used to compute running averages of square gradient.
    :param weight_decay: float. weight decay (L2 penalty).
    :param weight_decouple: bool. the optimizer uses decoupled weight decay as in AdamW.
    :param fixed_decay: bool. fix weight decay.
    :param clip_threshold: float. threshold of root-mean-square of final gradient update.
    :param ams_bound: bool. whether to use the AMSBound variant.
    :param scale_parameter: bool. if true, learning rate is scaled by root-mean-square of parameter.
    :param relative_step: bool. if true, time-dependent learning rate is computed instead of external learning rate.
    :param warmup_init: bool. time-dependent learning rate computation depends on whether warm-up initialization
        is being used.
    :param eps1: float. term added to the denominator to improve numerical stability.
    :param eps2: float. term added to the denominator to improve numerical stability.
    :param use_galore: bool. use GaLore variant.
    """

    def __init__(
        self,
        params: PARAMETERS,
        lr: Optional[float] = 1e-3,
        betas: BETAS = (0.9, 0.999),
        decay_rate: float = -0.8,
        weight_decay: float = 0.0,
        weight_decouple: bool = True,
        fixed_decay: bool = False,
        clip_threshold: float = 1.0,
        ams_bound: bool = False,
        scale_parameter: bool = True,
        relative_step: bool = True,
        warmup_init: bool = False,
        eps1: float = 1e-30,
        eps2: float = 1e-3,
        **kwargs,
    ):
        self.validate_learning_rate(lr)
        self.validate_betas(betas)
        self.validate_non_negative(weight_decay, 'weight_decay')
        self.validate_non_negative(eps1, 'eps1')
        self.validate_non_negative(eps2, 'eps2')

        self.decay_rate = decay_rate
        self.clip_threshold = clip_threshold
        self.eps1 = eps1
        self.eps2 = eps2

        defaults: DEFAULTS = {
            'lr': lr,
            'betas': betas,
            'weight_decay': weight_decay,
            'weight_decouple': weight_decouple,
            'fixed_decay': fixed_decay,
            'ams_bound': ams_bound,
            'scale_parameter': scale_parameter,
            'relative_step': relative_step,
            'warmup_init': warmup_init,
            'eps1': eps1,
            'eps2': eps2,
            **kwargs,
        }
        super().__init__(params, defaults)

    def __str__(self) -> str:
        return 'AdaFactor'

    @torch.no_grad()
    def reset(self):
        for group in self.param_groups:
            group['step'] = 0
            for p in group['params']:
                state = self.state[p]

                grad = p.grad

                grad_shape: Tuple[int, ...] = grad.shape
                factored: bool = self.get_options(grad_shape)

                state['exp_avg'] = torch.zeros_like(p)

                if factored:
                    state['exp_avg_sq_row'] = torch.zeros(grad_shape[:-1], dtype=grad.dtype, device=grad.device)
                    state['exp_avg_sq_col'] = torch.zeros(
                        grad_shape[:-2] + grad_shape[-1:], dtype=grad.dtype, device=grad.device
                    )
                else:
                    state['exp_avg_sq'] = torch.zeros_like(grad)

                if group['ams_bound']:
                    state['exp_avg_sq_hat'] = torch.zeros_like(grad)

                state['RMS'] = 0.0

    def get_lr(
        self, lr: float, step: int, rms: float, relative_step: bool, warmup_init: bool, scale_parameter: bool
    ) -> float:
        r"""Get AdaFactor learning rate."""
        relative_step_size: float = lr
        if relative_step:
            min_step: float = 1e-6 * step if warmup_init else 1e-2
            relative_step_size = min(min_step, 1.0 / math.sqrt(step))

        param_scale: float = 1.0 if scale_parameter else max(self.eps2, rms)

        return param_scale * relative_step_size

    @staticmethod
    def get_options(shape: Tuple[int, ...]) -> bool:
        r"""Get `factored`."""
        return len(shape) >= 2

    @staticmethod
    def get_rms(x: torch.Tensor) -> float:
        r"""Get RMS."""
        return x.norm(2) / math.sqrt(x.numel())

    @staticmethod
    def approximate_sq_grad(
        exp_avg_sq_row: torch.Tensor,
        exp_avg_sq_col: torch.Tensor,
        output: torch.Tensor,
    ):
        r"""Get approximation of EMA of squared gradient."""
        r_factor: torch.Tensor = (exp_avg_sq_row / exp_avg_sq_row.mean(dim=-1, keepdim=True)).rsqrt_().unsqueeze(-1)
        c_factor: torch.Tensor = exp_avg_sq_col.unsqueeze(-2).rsqrt()
        torch.mul(r_factor, c_factor, out=output)

    @torch.no_grad()
    def step(self, closure: CLOSURE = None) -> LOSS:
        loss: LOSS = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        for group in self.param_groups:
            if 'step' in group:
                group['step'] += 1
            else:
                group['step'] = 1

            beta1, _ = group['betas']

            beta2_t: float = 1.0 - math.pow(group['step'], self.decay_rate)

            for p in group['params']:
                if p.grad is None:
                    continue

                grad = p.grad
                if grad.is_sparse:
                    raise NoSparseGradientError(str(self))

                state = self.state[p]

                grad_shape: Tuple[int, ...] = grad.shape
                factored: bool = self.get_options(grad_shape)

                if 'rank' in group:
                    if 'projector' not in state:
                        state['projector'] = GaLoreProjector(
                            rank=group['rank'],
                            update_proj_gap=group['update_proj_gap'],
                            scale=group['scale'],
                            projection_type=group['projection_type'],
                        )

                    grad = state['projector'].project(grad, group['step'])

                if len(state) == 0:
                    state['exp_avg'] = torch.zeros_like(p)

                    if factored:
                        state['exp_avg_sq_row'] = torch.zeros(grad_shape[:-1], dtype=grad.dtype, device=grad.device)
                        state['exp_avg_sq_col'] = torch.zeros(
                            grad_shape[:-2] + grad_shape[-1:], dtype=grad.dtype, device=grad.device
                        )
                    else:
                        state['exp_avg_sq'] = torch.zeros_like(grad)

                    if group['ams_bound']:
                        state['exp_avg_sq_hat'] = torch.zeros_like(grad)

                    state['RMS'] = 0.0

                state['RMS'] = self.get_rms(p)

                lr: float = self.get_lr(
                    lr=group['lr'],
                    step=group['step'],
                    rms=state['RMS'],
                    relative_step=group['relative_step'],
                    warmup_init=group['warmup_init'],
                    scale_parameter=group['scale_parameter'],
                )

                update = torch.mul(grad, grad).add_(self.eps1)

                if factored:
                    exp_avg_sq_row, exp_avg_sq_col = state['exp_avg_sq_row'], state['exp_avg_sq_col']

                    exp_avg_sq_row.mul_(beta2_t).add_(update.mean(dim=-1), alpha=1.0 - beta2_t)
                    exp_avg_sq_col.mul_(beta2_t).add_(update.mean(dim=-2), alpha=1.0 - beta2_t)

                    self.approximate_sq_grad(exp_avg_sq_row, exp_avg_sq_col, update)
                else:
                    exp_avg_sq = state['exp_avg_sq']
                    exp_avg_sq.mul_(beta2_t).add_(update, alpha=1.0 - beta2_t)
                    torch.rsqrt(exp_avg_sq, out=update)

                if group['ams_bound']:
                    exp_avg_sq_hat = state['exp_avg_sq_hat']
                    torch.max(exp_avg_sq_hat, 1 / update, out=exp_avg_sq_hat)
                    torch.rsqrt(exp_avg_sq_hat / beta2_t, out=update)

                update.mul_(grad)

                update.div_((self.get_rms(update) / self.clip_threshold).clamp_(min=1.0)).mul_(lr)

                exp_avg = state['exp_avg']
                exp_avg.mul_(beta1).add_(update, alpha=1.0 - beta1)

                if 'rank' in group:
                    exp_avg = state['projector'].project_back(exp_avg)

                self.apply_weight_decay(
                    p=p,
                    grad=None,
                    lr=lr,
                    weight_decay=group['weight_decay'],
                    weight_decouple=group['weight_decouple'],
                    fixed_decay=group['fixed_decay'],
                )

                p.add_(-exp_avg)

        return loss
