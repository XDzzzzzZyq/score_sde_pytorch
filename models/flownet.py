import torch
import torch.nn as nn
import torch.nn.functional as F
from op import correlation

temp_grid = {}
def project(f, u, dt):
    if str(u.shape) not in temp_grid:
        B, C, H, W = u.shape
        grid_h = torch.linspace(-1.0, 1.0, W).view(1, 1, 1, W).expand(B, -1, H, -1)
        grid_v = torch.linspace(-1.0, 1.0, H).view(1, 1, H, 1).expand(B, -1, -1, W)
        temp_grid[str(u.shape)] = torch.cat([grid_h, grid_v], 1)

    grid = temp_grid[str(u.shape)].to(u.device)
    u = torch.cat([
        u[:, 1:2, :, :] / ((f.size(2) - 1.0) / 2.0),
        u[:, 0:1, :, :] / ((f.size(3) - 1.0) / 2.0)
    ], 1)

    return torch.nn.functional.grid_sample(
        input=f,
        grid=(grid - u * dt).permute(0, 2, 3, 1),
        mode='bilinear',
        padding_mode='reflection',
        align_corners=True)

def get_conv_feature_layer(in_channels, out_channels):
    layer = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=3, stride=2, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
                    torch.nn.Conv2d(in_channels=out_channels, out_channels=out_channels, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1))
    return layer

def get_conv_decode_layer(in_channels, out_channels):
    layer = torch.nn.Sequential(
                    torch.nn.Conv2d(in_channels=in_channels, out_channels=out_channels, kernel_size=3, stride=1, padding=1),
                    torch.nn.LeakyReLU(inplace=False, negative_slope=0.1))
    return layer

def get_conv_field_layer(in_channels, out_channels):
    layer = torch.nn.Sequential(torch.nn.Conv2d(in_channels=in_channels, out_channels=128, kernel_size=3, stride=1, padding=1),
        torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
        torch.nn.Conv2d(in_channels=128, out_channels=64, kernel_size=3, stride=1, padding=1),
        torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
        torch.nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, stride=1, padding=1),
        torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
        torch.nn.Conv2d(in_channels=32, out_channels=out_channels, kernel_size=3, stride=1, padding=1))
    return layer

def get_conv_up_layer(out_channels):
    layer = torch.nn.Sequential(torch.nn.Conv2d(in_channels=2+out_channels, out_channels=64, kernel_size=3, stride=1, padding=1),
        torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
        torch.nn.Conv2d(in_channels=64, out_channels=32, kernel_size=3, stride=1, padding=1),
        torch.nn.LeakyReLU(inplace=False, negative_slope=0.1),
        torch.nn.Conv2d(in_channels=32, out_channels=out_channels, kernel_size=3, stride=1, padding=1))
    return layer


class FeatureExtractor(nn.Module):
    def __init__(self, config):
        super(FeatureExtractor, self).__init__()
        self.C, self.H, self.W = config.data.num_channels, config.data.image_size, config.data.image_size
        self.fln = len(config.model.feature_nums)  # num of feature layers

        feature_extractors = []
        ch_i = self.C
        for i in range(self.fln):
            ch_o = config.model.feature_nums[i]
            feature_extractors.append(get_conv_feature_layer(ch_i, ch_o))
            ch_i = ch_o

        self.feature_extractors = nn.ModuleList(feature_extractors)

    def forward(self, x):
        result = []
        for idx, layer in enumerate(self.feature_extractors):
            x = layer(x)
            result.append(x)

        return result


class Matching(nn.Module):
    def __init__(self, config, level):
        super(Matching, self).__init__()
        self.dt = config.data.dt * 0.5**level

        self.flow_upsample = torch.nn.ConvTranspose2d(
                        in_channels=2,
                        out_channels=2,
                        kernel_size=4,
                        stride=2,
                        padding=1,
                        bias=False,
                        groups=2)

        self.corr_conv = get_conv_field_layer(49, 2)

    def forward(self, feature1, feature2, flow=None):

        # backward warping by previous flow field
        if flow is not None:
            flow = self.flow_upsample(flow)
            feature2 = project(feature2, flow, -self.dt)
        else:
            flow = 0.0

        corr = correlation.FunctionCorrelation(feature1, feature2, stride=1)
        corr = torch.nn.functional.leaky_relu(corr)

        return flow + self.corr_conv(corr)

class SubpixelRefinement(nn.Module):
    def __init__(self, config, level):
        super(SubpixelRefinement, self).__init__()

        self.dt = config.data.dt * 0.5 ** (level+1)

        block_depth = config.model.feature_nums[level]*2 + 2  # feature1 + feature2 + flow(2)
        self.flow_conv = get_conv_field_layer(block_depth, 2)

    def forward(self, feature1, feature2, flow):

        # backward warping by vm
        feature2 = project(feature2, flow, -self.dt)

        block = torch.cat([feature1, feature2, flow], dim=1)
        return flow + self.flow_conv(block)

class InferenceUnit(nn.Module):
    def __init__(self, config, level):
        super(InferenceUnit, self).__init__()
        self.level = level
        self.match = Matching(config, level)
        self.refinement = SubpixelRefinement(config, level)

    def forward(self, feature1, feature2, flow=None, p_prev=None):
        flow_m = self.match(feature1, feature2, flow)
        flow_s = self.refinement(feature1, feature2, flow_m)
        return flow_s


class Upsample(nn.Module):
    def __init__(self, size):
        super(Upsample, self).__init__()

        self.up = get_conv_up_layer(2)
        self.size = size

    def forward(self, f1, f2, x):
        x = F.interpolate(input=x, size=self.size, mode='bilinear', align_corners=False)
        block = torch.cat([f1, f2, x], dim=1)

        return x + self.up(block)


class FlowNet(nn.Module):
    def __init__(self, config):
        super(FlowNet, self).__init__()

        self.size = (config.data.image_size, config.data.image_size)
        self.feature_extractor = FeatureExtractor(config)

        levels = [l for l in range(len(config.model.feature_nums))][::-1] # level n-1, n-2, ..., 0
        self.inference_units = nn.ModuleList([InferenceUnit(config, level) for level in levels])

        self.upsample = Upsample(self.size)

    def forward(self, f1, f2, coord, t):
        f1_features = self.feature_extractor(f1)
        f2_features = self.feature_extractor(f2)
        
        cascaded_flow = []
        flow = None
        for unit in self.inference_units:
            feature1 = f1_features[unit.level]
            feature2 = f2_features[unit.level]
            flow= unit(feature1, feature2, flow)
            cascaded_flow.append(flow)
        
        flow = self.upsample(f1, f2, flow)
        cascaded_flow.append(flow)

        return cascaded_flow

    def multiscale_data_mse(self, veloc_pred: list[torch.Tensor], target):
        h, w = veloc_pred[-1].shape[-2], veloc_pred[-1].shape[-1]

        weights = [12.7, 5.5, 4.35, 3.9, 3.4, 1.1][:len(veloc_pred)]
        error_fn = torch.nn.MSELoss()

        v_loss = 0
        for i, weight in enumerate(weights):
            scale_factor = 1.0 / (2 ** i)

            flow = veloc_pred[-1 - i]
            losses_flow = error_fn(flow * scale_factor, target[:, :2] * scale_factor)

            v_l = weight * losses_flow

            v_loss += v_l

            h = h // 2
            w = w // 2

            target = F.interpolate(target, (h, w), mode='bilinear', align_corners=False)

        return v_loss

from . import layers
def get_double_conv(in_channels, out_channels):
    layer = nn.Sequential(nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1, bias=False),
        nn.ReLU(inplace=True),
        nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1, bias=False),
        nn.ReLU(inplace=True))

    return layer
def get_down_layer(in_channels, out_channels):
    layer = nn.Sequential(
            nn.MaxPool2d(2),
            layers.ResidualBlock(in_channels, out_channels)
        )
    return layer
def get_up_layer(in_channels, out_channels):
    return nn.Sequential(
            nn.ConvTranspose2d(in_channels, out_channels, kernel_size=2, stride=2)
        )

class PressureNet(nn.Module):
    def __init__(self, config):
        super(PressureNet, self).__init__()

        channels = config.model.feature_nums
        self.first = get_double_conv(2, channels[0])

        ch_i = channels[0]
        self.down = []
        for ch_o in channels[1:]:
            self.down.append(get_down_layer(ch_i, ch_o))
            ch_i = ch_o
        self.down = nn.ModuleList(self.down)

        ch_i = channels[-1]
        self.up = []
        self.up_conv = []
        for ch_o in channels[-2::-1]:
            self.up.append(get_up_layer(ch_i, ch_o))
            self.up_conv.append(layers.ResidualBlock(ch_o*2 + 3, ch_o))
            ch_i = ch_o
        self.up = nn.ModuleList(self.up)
        self.up_conv = nn.ModuleList(self.up_conv)

        self.end = get_double_conv(channels[0], 1)

    def forward(self, cascaded_flow):
        x = self.first(cascaded_flow[-1].detach())
        features = [x]

        for down in self.down:
            x = down(x)
            features.append(x)
        features.pop(-1)

        for idx in range(len(features)):
            feature = features[-1-idx]

            flow = cascaded_flow[idx+2].detach().clone()
            flow_norm = -(flow ** 2).sum(dim=1).unsqueeze(1)

            up = self.up[idx]
            up_conv = self.up_conv[idx]

            x = up(x)
            block = torch.cat([feature, x, flow, flow_norm], dim=1)
            x = up_conv(block)

        x = self.end(x)
        return x

    def data_mse(self, pressure, target):
        error_fn = torch.nn.MSELoss()
        return error_fn(pressure, target[:,2:3]) * 0.005


if __name__ == '__main__':
    a = torch.randn(1, 1, 224, 224,device='cuda:0')
    b = torch.randn(1, 1, 224, 224,device='cuda:0')

    c = correlation.FunctionCorrelation(a, b, stride=1)
    print(c.shape)