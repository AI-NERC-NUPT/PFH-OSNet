from __future__ import division, absolute_import
import warnings
import torch
from torch import nn
from torch.nn import functional as F

__all__ = ['pfh_osnet']

pretrained_urls = {
    'osnet_x1_0':
    'https://drive.google.com/uc?id=1LaG1EJpHrxdAxKnSCJ_i0u-nbxSAeiFY',
    'osnet_x0_75':
    'https://drive.google.com/uc?id=1uwA9fElHOk3ZogwbeY5GkLI6QPTX70Hq',
    'osnet_x0_5':
    'https://drive.google.com/uc?id=16DGLbZukvVYgINws8u8deSaOqjybZ83i',
    'osnet_x0_25':
    'https://drive.google.com/uc?id=1rb8UN5ZzPKRc_xvtHlyDh-cSz88YX9hs',
    'osnet_ibn_x1_0':
    'https://drive.google.com/uc?id=2sr90V6irlYYDd4_4ISU2iruoRG8J__6l'
}


##########
# Basic layers
##########
class ConvLayer(nn.Module):
    """Convolution layer (conv + bn + relu)."""
    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride=1,
                 padding=0,
                 groups=1,
                 IN=False):
        super(ConvLayer, self).__init__()
        self.conv = nn.Conv2d(in_channels,
                              out_channels,
                              kernel_size,
                              stride=stride,
                              padding=padding,
                              bias=False,
                              groups=groups)
        if IN:
            self.bn = nn.InstanceNorm2d(out_channels, affine=True)
        else:
            self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.relu(x)
        return x


class Conv1x1(nn.Module):
    """1x1 convolution + bn + relu."""
    def __init__(self, in_channels, out_channels, stride=1, groups=1):
        super(Conv1x1, self).__init__()
        self.conv = nn.Conv2d(in_channels,
                              out_channels,
                              1,
                              stride=stride,
                              padding=0,
                              bias=False,
                              groups=groups)
        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.relu(x)
        return x


class Conv1x1Linear(nn.Module):
    """1x1 convolution + bn (w/o non-linearity)."""
    def __init__(self, in_channels, out_channels, stride=1):
        super(Conv1x1Linear, self).__init__()
        self.conv = nn.Conv2d(in_channels,
                              out_channels,
                              1,
                              stride=stride,
                              padding=0,
                              bias=False)
        self.bn = nn.BatchNorm2d(out_channels)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        return x


class Conv3x3(nn.Module):
    """3x3 convolution + bn + relu."""
    def __init__(self, in_channels, out_channels, stride=1, groups=1):
        super(Conv3x3, self).__init__()
        self.conv = nn.Conv2d(in_channels,
                              out_channels,
                              3,
                              stride=stride,
                              padding=1,
                              bias=False,
                              groups=groups)
        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.relu(x)
        return x


class LightConv3x3(nn.Module):
    """Lightweight 3x3 convolution.

    1x1 (linear) + dw 3x3 (nonlinear).
    """
    def __init__(self, in_channels, out_channels):
        super(LightConv3x3, self).__init__()
        self.conv1 = nn.Conv2d(in_channels,
                               out_channels,
                               1,
                               stride=1,
                               padding=0,
                               bias=False)
        self.conv2 = nn.Conv2d(out_channels,
                               out_channels,
                               3,
                               stride=1,
                               padding=1,
                               bias=False,
                               groups=out_channels)
        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.bn(x)
        x = self.relu(x)
        return x


##########
# Building blocks for omni-scale feature learning
##########
class ChannelGate(nn.Module):
    """A mini-network that generates channel-wise gates conditioned on input tensor."""
    def __init__(self,
                 in_channels,
                 num_gates=None,
                 return_gates=False,
                 gate_activation='sigmoid',
                 reduction=16,
                 layer_norm=False):
        super(ChannelGate, self).__init__()
        if num_gates is None:
            num_gates = in_channels
        self.return_gates = return_gates
        self.global_avgpool = nn.AdaptiveAvgPool2d(1)
        self.fc1 = nn.Conv2d(in_channels,
                             in_channels // reduction,
                             kernel_size=1,
                             bias=True,
                             padding=0)
        self.norm1 = None
        if layer_norm:
            self.norm1 = nn.LayerNorm((in_channels // reduction, 1, 1))
        self.relu = nn.ReLU(inplace=True)
        self.fc2 = nn.Conv2d(in_channels // reduction,
                             num_gates,
                             kernel_size=1,
                             bias=True,
                             padding=0)
        if gate_activation == 'sigmoid':
            self.gate_activation = nn.Sigmoid()
        elif gate_activation == 'relu':
            self.gate_activation = nn.ReLU(inplace=True)
        elif gate_activation == 'linear':
            self.gate_activation = None
        else:
            raise RuntimeError(
                "Unknown gate activation: {}".format(gate_activation))

    def forward(self, x):
        input = x
        x = self.global_avgpool(x)
        x = self.fc1(x)
        if self.norm1 is not None:
            x = self.norm1(x)
        x = self.relu(x)
        x = self.fc2(x)
        if self.gate_activation is not None:
            x = self.gate_activation(x)
        if self.return_gates:
            return x
        return input * x


class OSBlock(nn.Module):
    """Omni-scale feature learning block."""
    def __init__(self,
                 in_channels,
                 out_channels,
                 IN=False,
                 bottleneck_reduction=4,
                 **kwargs):
        super(OSBlock, self).__init__()
        mid_channels = out_channels // bottleneck_reduction
        self.conv1 = Conv1x1(in_channels, mid_channels)
        self.conv2a = LightConv3x3(mid_channels, mid_channels)
        self.conv2b = nn.Sequential(
            LightConv3x3(mid_channels, mid_channels),
            LightConv3x3(mid_channels, mid_channels),
        )
        self.conv2c = nn.Sequential(
            LightConv3x3(mid_channels, mid_channels),
            LightConv3x3(mid_channels, mid_channels),
            LightConv3x3(mid_channels, mid_channels),
        )
        self.conv2d = nn.Sequential(
            LightConv3x3(mid_channels, mid_channels),
            LightConv3x3(mid_channels, mid_channels),
            LightConv3x3(mid_channels, mid_channels),
            LightConv3x3(mid_channels, mid_channels),
        )
        self.gate = ChannelGate(mid_channels)
        self.conv3 = Conv1x1Linear(mid_channels, out_channels)
        self.downsample = None
        if in_channels != out_channels:
            self.downsample = Conv1x1Linear(in_channels, out_channels)
        self.IN = None
        if IN:
            self.IN = nn.InstanceNorm2d(out_channels, affine=True)

    def forward(self, x):
        identity = x
        x1 = self.conv1(x)
        x2a = self.conv2a(x1)
        x2b = self.conv2b(x1)
        x2c = self.conv2c(x1)
        x2d = self.conv2d(x1)
        x2 = self.gate(x2a) + self.gate(x2b) + self.gate(x2c) + self.gate(x2d)
        x3 = self.conv3(x2)
        if self.downsample is not None:
            identity = self.downsample(identity)
        out = x3 + identity
        if self.IN is not None:
            out = self.IN(out)
        return F.relu(out), x2


##########
# Network architecture
##########
class OSNet(nn.Module):
    def __init__(self,
                 num_classes,
                 blocks,
                 layers,
                 channels,
                 feature_dim=512,
                 loss='softmax',
                 IN=False,
                 **kwargs):
        super(OSNet, self).__init__()
        num_blocks = len(blocks)
        assert num_blocks == len(layers)
        assert num_blocks == len(channels) - 1
        self.loss = loss

        self.conv1 = ConvLayer(3, channels[0], 7, stride=2, padding=3, IN=IN)
        self.maxpool = nn.MaxPool2d(3, stride=2, padding=1)

        self.conv2_0 = OSBlock(64, 256, IN=IN)
        self.conv2_1 = OSBlock(256, 256, IN=IN)
        self.conv2_2 = Conv1x1(256, 256)
        self.conv2_3 = nn.AvgPool2d(2, stride=2)

        self.conv3_0 = OSBlock(256, 384, IN=IN)
        self.conv3_1 = OSBlock(384, 384, IN=IN)
        self.conv3_2 = Conv1x1(384, 384)
        self.conv3_3 = nn.AvgPool2d(2, stride=2)

        self.conv4_0 = OSBlock(384, 512, IN=IN)
        self.conv4_1 = OSBlock(512, 512, IN=IN)

        self.conv_a = Conv1x1(64, 64)
        self.conv_b = Conv1x1(96, 96)
        self.conv_c = Conv1x1(128, 128)
        self.conv5 = Conv1x1(channels[3], channels[3])
        self.global_avgpool = nn.AdaptiveAvgPool2d(1)
        # self.global_maxpool = nn.AdaptiveMaxPool2d(1)
        # self.gem = GeM()
        # identity classification layer
        self.classifier = nn.Linear(512, num_classes)
        self.classifier_a = nn.Linear(64, num_classes)
        self.classifier_b = nn.Linear(96, num_classes)
        self.classifier_c = nn.Linear(128, num_classes)

        self.bn = nn.BatchNorm1d(512)
        self.bn_a = nn.BatchNorm1d(64)
        self.bn_b = nn.BatchNorm1d(96)
        self.bn_c = nn.BatchNorm1d(128)

        self._init_params()

    def _construct_fc_layer(self, fc_dims, input_dim, dropout_p=None):
        if fc_dims is None or fc_dims < 0:
            self.feature_dim = input_dim
            return None

        if isinstance(fc_dims, int):
            fc_dims = [fc_dims]

        layers = []
        for dim in fc_dims:
            layers.append(nn.Linear(input_dim, dim))
            layers.append(nn.BatchNorm1d(dim))
            layers.append(nn.ReLU(inplace=True))
            if dropout_p is not None:
                layers.append(nn.Dropout(p=dropout_p))
            input_dim = dim

        self.feature_dim = fc_dims[-1]

        return nn.Sequential(*layers)

    def _init_params(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight,
                                        mode='fan_out',
                                        nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

            elif isinstance(m, nn.BatchNorm1d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

    def featuremaps(self, x):
        x = self.conv1(x)  # [B, 64, 128, 64]
        x = self.maxpool(x)  # [B, 64, 64, 32]
        x, _ = self.conv2_0(x)  # [B, 256, 64, 32]
        # print('a0.shape:', a0.shape) # [B, 64, 64, 32]
        x, a1 = self.conv2_1(x)  # [B, 256, 64, 32]
        # print('a1.shape:', a1.shape) # [B, 64, 64, 32]
        x = self.conv2_2(x)  # [B, 256, 64, 32]
        x = self.conv2_3(x)  # [B, 256, 32, 16]
        x, _ = self.conv3_0(x)  # [B, 384, 32, 16]
        # print('b0.shape:', b0.shape)  # [B, 96, 32, 16]
        x, b1 = self.conv3_1(x)  # [B, 384, 32, 16]
        # print('b1.shape:', b1.shape)  # [B, 96, 32, 16]
        x = self.conv3_2(x)  # [B, 384, 32, 16]
        x = self.conv3_3(x)  # [B, 384, 16, 8]
        x, _ = self.conv4_0(x)  # [B, 512, 16, 8]
        # print('c0.shape:', c0.shape)  # [B, 128, 16, 8]
        x, c1 = self.conv4_1(x)  # [B, 512, 16, 8]
        # print('c1.shape:', c1.shape)  # [B, 128, 16, 8]
        a1 = self.conv_a(a1)
        b1 = self.conv_b(b1)
        c1 = self.conv_c(c1)
        x = self.conv5(x)  # [B, 512, 16, 8]
        return x, a1, b1, c1

    def forward(self, x, return_featuremaps=False):
        x, a, b, c = self.featuremaps(x)
        if return_featuremaps:
            return c
        v = self.global_avgpool(x)
        va = self.global_avgpool(a)
        vb = self.global_avgpool(b)
        vc = self.global_avgpool(c)
        # v = self.global_maxpool(x)
        # v = self.gem(x)
        v = v.view(v.size(0), -1)
        va = va.view(va.size(0), -1)
        vb = vb.view(vb.size(0), -1)
        vc = vc.view(vc.size(0), -1)

        v_ = [v, va, vb, vc]

        v = self.bn(v)
        va = self.bn_a(va)
        vb = self.bn_b(vb)
        vc = self.bn_c(vc)

        if not self.training:
            v = F.normalize(v, p=2, dim=1)
            va = F.normalize(va, p=2, dim=1)
            vb = F.normalize(vb, p=2, dim=1)
            vc = F.normalize(vc, p=2, dim=1)
            # return v, va, vb, vc
            return torch.cat([v, va, vb, vc], dim=1)
        y = self.classifier(v)
        ya = self.classifier_a(va)
        yb = self.classifier_b(vb)
        yc = self.classifier_c(vc)
        y = [y, ya, yb, yc]
        v = [v, va, vb, vc]
        if self.loss == 'softmax':
            return y
        elif self.loss == 'triplet':
            return y, v_
        else:
            raise KeyError("Unsupported loss: {}".format(self.loss))


def init_pretrained_weights(model, key=''):
    """Initializes model with pretrained weights.
    
    Layers that don't match with pretrained layers in name or size are kept unchanged.
    """
    import os
    import errno
    import gdown
    from collections import OrderedDict

    def _get_torch_home():
        ENV_TORCH_HOME = 'TORCH_HOME'
        ENV_XDG_CACHE_HOME = 'XDG_CACHE_HOME'
        DEFAULT_CACHE_DIR = '~/.cache'
        torch_home = os.path.expanduser(
            os.getenv(
                ENV_TORCH_HOME,
                os.path.join(os.getenv(ENV_XDG_CACHE_HOME, DEFAULT_CACHE_DIR),
                             'torch')))
        return torch_home

    torch_home = _get_torch_home()
    model_dir = os.path.join(torch_home, 'checkpoints')
    try:
        os.makedirs(model_dir)
    except OSError as e:
        if e.errno == errno.EEXIST:
            # Directory already exists, ignore.
            pass
        else:
            # Unexpected OSError, re-raise.
            raise
    filename = key + '_imagenet.pth'
    cached_file = os.path.join(model_dir, filename)

    if not os.path.exists(cached_file):
        gdown.download(pretrained_urls[key], cached_file, quiet=False)

    state_dict = torch.load(cached_file)
    model_dict = model.state_dict()
    new_state_dict = OrderedDict()
    matched_layers, discarded_layers = [], []

    for k, v in state_dict.items():
        if k.startswith('module.'):
            k = k[7:]  # discard module.

        if k.startswith('conv2.0'):
            k = k.replace('conv2.0', 'conv2_0')
        if k.startswith('conv2.1'):
            k = k.replace('conv2.1', 'conv2_1')
        if k.startswith('conv2.2.0'):
            k = k.replace('conv2.2.0', 'conv2_2')

        if k.startswith('conv3.0'):
            k = k.replace('conv3.0', 'conv3_0')
        if k.startswith('conv3.1'):
            k = k.replace('conv3.1', 'conv3_1')
        if k.startswith('conv3.2.0'):
            k = k.replace('conv3.2.0', 'conv3_2')

        if k.startswith('conv4.0'):
            k = k.replace('conv4.0', 'conv4_0')
        if k.startswith('conv4.1'):
            k = k.replace('conv4.1', 'conv4_1')

        if k in model_dict and model_dict[k].size() == v.size():
            new_state_dict[k] = v
            matched_layers.append(k)
        else:
            discarded_layers.append(k)

    model_dict.update(new_state_dict)
    model.load_state_dict(model_dict)

    if len(matched_layers) == 0:
        warnings.warn('The pretrained weights from "{}" cannot be loaded, '
                      'please check the key names manually '
                      '(** ignored and continue **)'.format(cached_file))
    else:
        print(
            'Successfully loaded imagenet pretrained weights from "{}"'.format(
                cached_file))
        if len(discarded_layers) > 0:
            print('** The following layers are discarded '
                  'due to unmatched keys or layer size: {}'.format(
                      discarded_layers))


##########
# Instantiation
##########
def pfh_osnet(num_classes=1000, pretrained=True, loss='softmax', **kwargs):
    # standard size (width x1.0)
    model = OSNet(num_classes,
                  blocks=[OSBlock, OSBlock, OSBlock],
                  layers=[2, 2, 2],
                  channels=[64, 256, 384, 512],
                  loss=loss,
                  **kwargs)
    if pretrained:
        init_pretrained_weights(model, key='osnet_x1_0')
    else:
        print('train from scratch...')
    return model


if __name__ == '__main__':
    from torchsummary import summary
    from thop import profile
    model = pfh_osnet(num_classes=0, pretrained=True, loss='softmax')
    print(model)
    input = torch.randn(2, 3, 384, 128)
    flops, params = profile(model, inputs=(input, ))
    print(summary(model, (3, 256, 128), device='cpu'))
    print('flops:', flops / 1000000, 'params:', params / 1000000)
    # ss = 0
    # for name, param in model.named_parameters():
    #     print(name)
    #     ss += param.numel()
    # print('ss: ', ss)