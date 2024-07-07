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
from models.ddpm import UNet
import torch.nn.functional as F
import torch.optim as opt 
from pyDOE import lhs


# Define network structure, specified by a list of layers indicating the number of layers and neurons
# 定义网络结构,由layer列表指定网络层数和神经元数
class PINN_Net(nn.Module):

    """
    Input:  field, t  := (x, y, f) : shape=(B, 3, N, N), (B, )
    Output: field_out := (u, v, p) : shape=(B, 3, N, N)
    """
    def __init__(self, config):
        super(PINN_Net, self).__init__()
        self.device = config.device

        self.model = torch.nn.DataParallel(UNet(config)).to(self.device)
        self.mask_u, self.mask_v, self.mask_p = self.get_mask(config)

    def get_mask(self, config):
        '''for differentiable slicing'''

        B = config.training.batch_size
        N = config.data.image_size
        device = config.device

        zero = torch.zeros(N, N)
        ones = torch.ones(N, N)
        mask1 = torch.stack([ones, zero, zero]).to(device)
        mask2 = torch.stack([zero, ones, zero]).to(device)
        mask3 = torch.stack([zero, zero, ones]).to(device)

        return mask1, mask2, mask3

    def forward(self, field, t):
        predict = self.model(field, t)
        return predict

    # derive loss for data
    # 类内方法：求数据点的loss
    def data_mse(self, prediction, target):
        u, v, p = torch.unbind(target, dim=1)
        u_predict, v_predict, p_predict = torch.unbind(prediction, dim=1)
        mse = torch.nn.MSELoss()
        mse_predict = mse(u_predict, u) + mse(v_predict, v) + mse(p_predict, p)
        return mse_predict

    # derive loss for equation
    def equation_mse_dimensionless(self, x, y, t, prediction, Re):

        # 获得预测的输出u,v,p

        u = self.mask_u * prediction
        v = self.mask_v * prediction
        p = self.mask_p * prediction

        # 通过自动微分计算各个偏导数,其中.sum()将矢量转化为标量，并无实际意义
        # first-order derivative
        # 一阶导

        u_x = torch.autograd.grad(u.sum(), x, create_graph=True)[0]
        u_y = torch.autograd.grad(u.sum(), y, create_graph=True)[0]
        u_t = torch.autograd.grad(u.sum(), t, create_graph=True)[0]
        v_x = torch.autograd.grad(v.sum(), x, create_graph=True)[0]
        v_y = torch.autograd.grad(v.sum(), y, create_graph=True)[0]
        v_t = torch.autograd.grad(v.sum(), t, create_graph=True)[0]
        p_x = torch.autograd.grad(p.sum(), x, create_graph=True)[0]
        p_y = torch.autograd.grad(p.sum(), y, create_graph=True)[0]

        # second-order derivative
        u_xx = torch.autograd.grad(u_x.sum(), x, retain_graph=True)[0]
        u_yy = torch.autograd.grad(u_y.sum(), y, retain_graph=True)[0]
        v_xx = torch.autograd.grad(v_x.sum(), x, retain_graph=True)[0]
        v_yy = torch.autograd.grad(v_y.sum(), y, retain_graph=True)[0]

        # reshape
        u = u[:, 0]
        v = v[:, 1]
        u_t = u_t[:, None, None]
        v_t = v_t[:, None, None]

        # residual
        # 计算偏微分方程的残差
        #print(u_t.shape, u.shape, u_x.shape, p_x.shape)
        f_equation_mass = u_x + v_y
        f_equation_x = u_t + (u * u_x + v * u_y) + p_x - 1.0 / Re * (u_xx + u_yy)
        f_equation_y = v_t + (u * v_x + v * v_y) + p_y - 1.0 / Re * (v_xx + v_yy)
        mse = torch.nn.MSELoss()
        batch_t_zeros = torch.zeros_like(x, dtype=torch.float32, device=self.device)
        mse_equation = mse(f_equation_x, batch_t_zeros) + mse(f_equation_y, batch_t_zeros) + \
                       mse(f_equation_mass, batch_t_zeros)

        return mse_equation

    def data_mse_inner_norm(self, X, t, Y):
        predict_out = self.forward_inner_norm(X, t)
        u, v, p = Y[:, 0], Y[:, 1], Y[:, 2]
        u_predict = predict_out[:, 0].reshape(-1, 1)
        v_predict = predict_out[:, 1].reshape(-1, 1)
        p_predict = predict_out[:, 2].reshape(-1, 1)
        mse = torch.nn.MSELoss()
        mse_predict = mse(u_predict, u) + mse(v_predict, v) + mse(p_predict, p)
        return mse_predict

if __name__ == '__main__':
    import matplotlib.pyplot as plt
    from matplotlib import cm
    from configs.pinn.pinn_pde import get_config

    config = get_config()

    Reynolds_number = 100  # 例子中的雷诺兹数

    # 创建设备（允许CPU或GPU）
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    # 初始模型、优化器和损失函数
    model = PINN_Net(config)
    state = torch.load("pinn_ns_model.pth", map_location=device)
    model.load_state_dict(state)

    # 可视化网格的设置
    num_points = 100  # 可视化时每个维度的数据点数量
    x_vis = np.linspace(0, 1, num_points)
    y_vis = np.linspace(0, 1, num_points)
    t_vis = np.array([1])  # 假设我们评估特定时间点t=1

    # 创建网格数据
    X_vis, Y_vis = np.meshgrid(x_vis, y_vis)
    T_vis = np.full(X_vis.shape, t_vis)

    # 将numpy数组转化为张量
    X_tensor = torch.from_numpy(X_vis.reshape(-1, 1)).float().to(device).requires_grad_()
    Y_tensor = torch.from_numpy(Y_vis.reshape(-1, 1)).float().to(device).requires_grad_()
    T_tensor = torch.from_numpy(T_vis.reshape(-1, 1)).float().to(device).requires_grad_()

    # 对网格上的点进行预测
    with torch.no_grad():
        u_pred, v_pred, p_pred = model.predict(X_tensor, Y_tensor, T_tensor)

    # 将预测结果转换成numpy数组，并且调整形状以匹配图形
    u_pred_np = u_pred.cpu().numpy().reshape(X_vis.shape)
    # 使用matplotlib进行绘图
    plt.figure(figsize=(10, 8))
    contour = plt.contourf(X_vis, Y_vis, u_pred_np, 100, cmap=cm.viridis)
    plt.colorbar(contour)
    plt.title('Predicted u velocity field at t=0')
    plt.xlabel('x')
    plt.ylabel('y')
    plt.show()

    #u_gt = torch.autograd.grad(outputs=)