"""
    ENet for image segmentation, implemented in PyTorch.
    Original paper: 'ENet: A Deep Neural Network Architecture for Real-Time Semantic Segmentation,'
    https://arxiv.org/abs/1606.02147.
"""

import torch
import torch.nn as nn
from common import ConvBlock, AsymConvBlock, DeconvBlock, NormActivation, conv1x1_block


class InitBlock(nn.Module):
    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 padding,
                 bias,
                 bn_eps,
                 activation):
        super(InitBlock, self).__init__()
        self.main_branch = nn.Conv2d(
            in_channels=in_channels,
            out_channels=(out_channels - in_channels),
            kernel_size=kernel_size,
            stride=2,
            padding=padding,
            bias=bias)
        self.ext_branch = nn.MaxPool2d(
            kernel_size=kernel_size,
            stride=2,
            padding=padding)
        self.norm_activ = NormActivation(
            in_channels=out_channels,
            bn_eps=bn_eps,
            activation=activation)

    def forward(self, x):
        x1 = self.main_branch(x)
        x2 = self.ext_branch(x)
        x = torch.cat((x1, x2), dim=1)
        x = self.norm_activ(x)
        return x


class DownBlock(nn.Module):
    def __init__(self,
                 ext_channels,
                 kernel_size,
                 padding):
        super().__init__()
        self.ext_channels = ext_channels

        self.pool = nn.MaxPool2d(
            kernel_size=kernel_size,
            stride=2,
            padding=padding,
            return_indices=True)

    def forward(self, x):
        x, max_indices = self.pool(x)
        branch, _, height, width = x.size()
        pad = torch.zeros(branch, self.ext_channels, height, width, dtype=x.dtype, device=x.device)
        x = torch.cat((x, pad), dim=1)
        return x, max_indices


class UpBlock(nn.Module):
    def __init__(self,
                 in_channels,
                 out_channels,
                 bias):
        super().__init__()
        self.conv = conv1x1_block(
            in_channels=in_channels,
            out_channels=out_channels,
            bias=bias,
            activation=None)
        self.unpool = nn.MaxUnpool2d(kernel_size=2)

    def forward(self, x, max_indices):
        x = self.conv(x)
        x = self.unpool(x, max_indices)
        return x


class ENetUnit(nn.Module):
    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 padding,
                 dilation,
                 use_asym_conv,
                 dropout_rate,
                 bias,
                 activation,
                 down,
                 bottleneck_factor=4):
        super().__init__()
        self.resize_identity = (in_channels != out_channels)
        self.down = down
        mid_channels = in_channels // bottleneck_factor

        if not self.resize_identity:
            self.conv1 = conv1x1_block(
                in_channels=in_channels,
                out_channels=mid_channels,
                bias=bias,
                activation=activation)
            if use_asym_conv:
                self.conv2 = AsymConvBlock(
                    channels=mid_channels,
                    kernel_size=kernel_size,
                    padding=padding,
                    dilation=dilation,
                    bias=bias,
                    lw_activation=activation,
                    rw_activation=activation)
            else:
                self.conv2 = ConvBlock(
                    in_channels=mid_channels,
                    out_channels=mid_channels,
                    kernel_size=kernel_size,
                    stride=1,
                    padding=padding,
                    dilation=dilation,
                    bias=bias,
                    activation=activation)
        elif self.down:
            self.identity_block = DownBlock(
                ext_channels=(out_channels - in_channels),
                kernel_size=kernel_size,
                padding=padding)
            self.conv1 = ConvBlock(
                in_channels=in_channels,
                out_channels=mid_channels,
                kernel_size=2,
                stride=2,
                padding=0,
                dilation=1,
                bias=bias,
                activation=activation)
            self.conv2 = ConvBlock(
                in_channels=mid_channels,
                out_channels=mid_channels,
                kernel_size=kernel_size,
                stride=1,
                padding=padding,
                dilation=dilation,
                bias=bias,
                activation=activation)
        else:
            self.identity_block = UpBlock(
                in_channels=in_channels,
                out_channels=out_channels,
                bias=bias)
            self.conv1 = conv1x1_block(
                in_channels=in_channels,
                out_channels=mid_channels,
                bias=bias,
                activation=activation)
            self.conv2 = DeconvBlock(
                in_channels=mid_channels,
                out_channels=mid_channels,
                kernel_size=kernel_size,
                stride=2,
                padding=padding,
                out_padding=1,
                dilation=dilation,
                bias=bias,
                activation=activation)
        self.conv3 = conv1x1_block(
            in_channels=mid_channels,
            out_channels=out_channels,
            bias=bias,
            activation=activation)
        self.dropout = nn.Dropout2d(p=dropout_rate)
        self.activ = activation()

    def forward(self, x, max_indices=None):
        if not self.resize_identity:
            identity = x
        elif self.down:
            identity, max_indices = self.identity_block(x)
        else:
            identity = self.identity_block(x, max_indices)

        x = self.conv1(x)
        x = self.conv2(x)
        x = self.conv3(x)
        x = self.dropout(x)

        x = x + identity
        x = self.activ(x)

        if self.resize_identity and self.down:
            return x, max_indices
        else:
            return x


class ENetStage(nn.Module):
    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_sizes,
                 paddings,
                 dilations,
                 use_asym_convs,
                 dropout_rate,
                 bias,
                 activation,
                 down):
        super().__init__()
        self.down = down

        units = nn.Sequential()
        for i, kernel_size in enumerate(kernel_sizes):
            unit = ENetUnit(
                in_channels=in_channels,
                out_channels=out_channels,
                kernel_size=kernel_size,
                padding=paddings[i],
                dilation=dilations[i],
                use_asym_conv=(use_asym_convs[i] == 1),
                dropout_rate=dropout_rate,
                bias=bias,
                activation=activation,
                down=down)
            if i == 0:
                self.scale_unit = unit
            else:
                units.add_module("unit{}".format(i + 1), unit)
            in_channels = out_channels
        self.units = units

    def forward(self, x, max_indices=None):
        if self.down:
            x, max_indices = self.scale_unit(x)
        else:
            x = self.scale_unit(x, max_indices)

        x = self.units(x)

        if self.down:
            return x, max_indices
        else:
            return x


class ENet(nn.Module):
    def __init__(self,
                 bn_eps=1e-5,
                 aux=False,
                 fixed_size=False,
                 in_channels=3,
                 in_size=(1024, 2048),
                 num_classes=19):
        super().__init__()
        assert (aux is not None)
        assert (fixed_size is not None)
        assert ((in_size[0] % 8 == 0) and (in_size[1] % 8 == 0))
        self.in_size = in_size
        self.num_classes = num_classes
        self.fixed_size = fixed_size

        bias = False
        encoder_activation = (lambda: nn.PReLU(1))
        decoder_activation = (lambda: nn.ReLU(inplace=True))

        out_channels = 16
        self.steam = InitBlock(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=3,
            padding=1,
            bias=bias,
            bn_eps=bn_eps,
            activation=encoder_activation)
        in_channels = out_channels

        channels = [64, 128, 64, 16]
        kernel_sizes = [[3, 3, 3, 3, 3], [3, 3, 3, 5, 3, 3, 3, 5, 3, 3, 3, 5, 3, 3, 3, 5, 3], [3, 3, 3], [3, 3]]
        paddings = [[1, 1, 1, 1, 1], [1, 1, 2, 2, 4, 1, 8, 2, 16, 1, 2, 2, 4, 1, 8, 2, 16], [1, 1, 1], [1, 1]]
        dilations = [[1, 1, 1, 1, 1], [1, 1, 2, 1, 4, 1, 8, 1, 16, 1, 2, 1, 4, 1, 8, 1, 16], [1, 1, 1], [1, 1]]
        use_asym_convs = [[0, 0, 0, 0, 0], [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0], [0, 0, 0], [0, 0]]
        dropout_rates = [0.01, 0.1, 0.1, 0.1]
        downs = [1, 1, 0, 0]

        for i, channels_per_stage in enumerate(channels):
            setattr(self, "stage{}".format(i + 1), ENetStage(
                in_channels=in_channels,
                out_channels=channels_per_stage,
                kernel_sizes=kernel_sizes[i],
                paddings=paddings[i],
                dilations=dilations[i],
                use_asym_convs=use_asym_convs[i],
                dropout_rate=dropout_rates[i],
                bias=bias,
                activation=(encoder_activation if downs[i] == 1 else decoder_activation),
                down=(downs[i] == 1)))
            in_channels = channels_per_stage

        self.head = nn.ConvTranspose2d(
            in_channels,
            num_classes,
            kernel_size=3,
            stride=2,
            padding=1,
            output_padding=1,
            bias=False)

    def forward(self, x):
        x = self.steam(x)
        x, max_indices1 = self.stage1(x)
        x, max_indices2 = self.stage2(x)
        x = self.stage3(x, max_indices2)
        x = self.stage4(x, max_indices1)
        x = self.head(x)
        return x


def oth_enet_cityscapes(num_classes=19, pretrained=False, **kwargs):
    return ENet(num_classes=num_classes, **kwargs)


def _calc_width(net):
    import numpy as np
    net_params = filter(lambda p: p.requires_grad, net.parameters())
    weight_count = 0
    for param in net_params:
        weight_count += np.prod(param.size())
    return weight_count


def _test():
    pretrained = False
    # fixed_size = True
    in_size = (1024, 2048)
    classes = 19

    models = [
        oth_enet_cityscapes,
    ]

    for model in models:

        # from torchsummary import summary
        # device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        # model = ENet(num_classes=19).to(device)
        # summary(model, (3, 512, 1024))

        net = model(pretrained=pretrained)

        # net.train()
        net.eval()
        weight_count = _calc_width(net)
        print("m={}, {}".format(model.__name__, weight_count))
        # assert (model != oth_enet_cityscapes or weight_count == 360422)
        assert (model != oth_enet_cityscapes or weight_count == 358060)

        batch = 4
        x = torch.randn(batch, 3, in_size[0], in_size[1])
        y = net(x)
        # y.sum().backward()
        assert (tuple(y.size()) == (batch, classes, in_size[0], in_size[1]))


if __name__ == "__main__":
    _test()
