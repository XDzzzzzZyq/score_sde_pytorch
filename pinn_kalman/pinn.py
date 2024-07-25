"""
PINN+incompressible NS equation
2-dimensional unsteady
PINN model +LOSS function
PINN融合不可压缩NS方程
二维非定常流动
PINN模型 + LOSS函数
"""
import os
import numpy as np
import torch
import torch.nn as nn
from models.ddpm import UNet, MLP
from models.flownet import FlowNet, PressureNet
from models.liteflownet import LiteFlowNet
import torch.nn.functional as F

from bayesian_torch.models.dnn_to_bnn import dnn_to_bnn

def get_model(config):
    if config.model.arch == 'flownet':
        return FlowNet(config)
    elif config.model.arch == 'liteflownet':
        return LiteFlowNet(config)
    elif config.model.arch == 'unet':
        return UNet(config)
    elif config.model.arch == 'mlp':
        return MLP(config)
    else:
        raise NotImplementedError

# Define network structure, specified by a list of layers indicating the number of layers and neurons
# 定义网络结构,由layer列表指定网络层数和神经元数
class PINN(nn.Module):

    """
    Input:  field, t  := (x, y, f) : shape=(B, 3, N, N), (B, )
    Output: field_out := (u, v, p) : shape=(B, 3, N, N)
    """
    def __init__(self, config):
        super(PINN, self).__init__()
        self.device = config.device
        flownet = get_model(config).to(self.device)
        #model = torch.nn.DataParallel(model)
        self.flownet = flownet
        self.pressurenet = PressureNet(config).to(self.device)
        self.mask_u, self.mask_v = self.get_mask(config)

    def get_mask(self, config):
        '''for differentiable slicing'''

        N = config.data.image_size
        device = config.device

        zero = torch.zeros(N, N)
        ones = torch.ones(N, N)
        mask1 = torch.stack([ones, zero]).to(device)
        mask2 = torch.stack([zero, ones]).to(device)

        return mask1, mask2

    def forward(self, f1, f2, x, y, t):
        flow = self.flownet(f1, f2, x, y, t)
        pressure = self.pressurenet(flow, x, y, t)
        return flow, pressure

    def advection_mse(self, x, y, t, prediction):
        return None

    # derive loss for equation
    def equation_mse(self, x, y, t, flow, pres, Re):

        # 获得预测的输出u,v,p

        u = (self.mask_u * flow).sum(dim=1).unsqueeze(1)
        v = (self.mask_v * flow).sum(dim=1).unsqueeze(1)
        p = pres

        # 通过自动微分计算各个偏导数,其中.sum()将矢量转化为标量，并无实际意义
        # first-order derivative
        # 一阶导

        u_x, u_y, u_t = torch.autograd.grad(u.sum(), (x, y, t), create_graph=True, retain_graph=True)
        v_x, v_y, v_t = torch.autograd.grad(v.sum(), (x, y, t), create_graph=True, retain_graph=True)
        p_x, p_y      = torch.autograd.grad(p.sum(), (x, y),    create_graph=True, retain_graph=True)

        # second-order derivative
        u_xx = torch.autograd.grad(u_x.sum(), x, retain_graph=True)[0]
        u_yy = torch.autograd.grad(u_y.sum(), y, retain_graph=True)[0]
        v_xx = torch.autograd.grad(v_x.sum(), x, retain_graph=True)[0]
        v_yy = torch.autograd.grad(v_y.sum(), y, retain_graph=True)[0]

        # reshape
        u_t = u_t[:,None,None,None]
        v_t = v_t[:,None,None,None]

        # residual
        # 计算偏微分方程的残差
        #print(u_t.shape, u.shape, u_x.shape, p_x.shape)
        f_equation_x    = u_t + (u * u_x + v * u_y) + p_x - 1.0 / Re * (u_xx + u_yy)
        f_equation_y    = v_t + (u * v_x + v * v_y) + p_y - 1.0 / Re * (v_xx + v_yy)
        f_equation_mass = u_x + v_y

        mse = torch.nn.MSELoss()
        batch_t_zeros = torch.zeros_like(x)
        mse_x    = mse(f_equation_x,    batch_t_zeros)
        mse_y    = mse(f_equation_y,    batch_t_zeros)
        mse_mass = mse(f_equation_mass, batch_t_zeros)

        return mse_x + mse_y + mse_mass

class B_PINN(nn.Module):
    def __init__(self, config, pretrained_pinn=None):
        self.using_pretrained = pretrained_pinn is not None

        super(B_PINN, self).__init__()
        const_bnn_prior_parameters = {
            "prior_mu": 0.0,
            "prior_sigma": 1.0,
            "posterior_mu_init": 0.0,
            "posterior_rho_init": -3.0,
            "type": "Reparameterization",               # Flipout or Reparameterization
            "moped_enable": self.using_pretrained,           # True to initialize mu/sigma from the pretrained dnn weights
            "moped_delta": config.model.bpinn_moped_delta, }

        self.model = pretrained_pinn if self.using_pretrained else PINN(config)
        dnn_to_bnn(self.model, const_bnn_prior_parameters)
        self.model = self.model.to(config.device)

        self.batch = config.training.batch_size

    def forward(self, f1, f2, x, y, t):
        flow, pressure = self.model(f1, f2, x, y, t)
        return flow, pressure

    def predict(self, f1, f2, x, y, t, n=64):
        flow_pred = []
        pres_pred = []
        for mc_run in range(n):
            flow, pressure = self.forward(f1, f2, x, y, t)
            flow_pred.append(flow[-1])
            pres_pred.append(pressure)
        flow_pred = torch.stack(flow_pred, dim=1).mean(dim=1)
        pres_pred = torch.stack(pres_pred, dim=1).mean(dim=1)

        return flow_pred, pres_pred

if __name__ == '__main__':
    print(0)