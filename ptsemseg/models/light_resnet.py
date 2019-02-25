"""RefineNet-LightWeight

RefineNet-LigthWeight PyTorch for non-commercial purposes

Copyright (c) 2018, Vladimir Nekrasov (vladimir.nekrasov@adelaide.edu.au)
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

import torch.nn as nn
import torch.nn.functional as F
import torch

import numpy as np

from ptsemseg.models.utils import maybe_download
from ptsemseg.models.layer_factory import conv1x1, conv3x3, CRPBlock
from ptsemseg.models.utils import freeze_weights, \
                                  masked_embeddings, \
                                  compute_weight

import copy

data_info = {
    7 : 'Person',
    21: 'VOC',
    40: 'NYU',
    60: 'Context'
    }

models_urls = {
    '50_person'  : 'https://cloudstor.aarnet.edu.au/plus/s/mLA7NxVSPjNL7Oo/download',
    '101_person' : 'https://cloudstor.aarnet.edu.au/plus/s/f1tGGpwdCnYS3xu/download',
    '152_person' : 'https://cloudstor.aarnet.edu.au/plus/s/Ql64rWqiTvWGAA0/download',

    '50_voc'     : 'https://cloudstor.aarnet.edu.au/plus/s/2E1KrdF2Rfc5khB/download',
    '101_voc'    : 'https://cloudstor.aarnet.edu.au/plus/s/CPRKWiaCIDRdOwF/download',
    '152_voc'    : 'https://cloudstor.aarnet.edu.au/plus/s/2w8bFOd45JtPqbD/download',

    '50_nyu'     : 'https://cloudstor.aarnet.edu.au/plus/s/gE8dnQmHr9svpfu/download',
    '101_nyu'    : 'https://cloudstor.aarnet.edu.au/plus/s/VnsaSUHNZkuIqeB/download',
    '152_nyu'    : 'https://cloudstor.aarnet.edu.au/plus/s/EkPQzB2KtrrDnKf/download',

    '101_context': 'https://cloudstor.aarnet.edu.au/plus/s/hqmplxWOBbOYYjN/download',
    '152_context': 'https://cloudstor.aarnet.edu.au/plus/s/O84NszlYlsu00fW/download',

    '50_imagenet' : 'https://download.pytorch.org/models/resnet50-19c8e357.pth',
    '101_imagenet': 'https://download.pytorch.org/models/resnet101-5d3b4d8f.pth',
    '152_imagenet': 'https://download.pytorch.org/models/resnet152-b121ed2d.pth',
    }

stages_suffixes = {0 : '_conv',
                   1 : '_conv_relu_varout_dimred'}

class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(BasicBlock, self).__init__()
        self.conv1 = conv3x3(inplanes, planes, stride)
        self.bn1 = nn.BatchNorm2d(planes)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = conv3x3(planes, planes)
        self.bn2 = nn.BatchNorm2d(planes)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None):
        super(Bottleneck, self).__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride,
                               padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv3 = nn.Conv2d(planes, planes * 4, kernel_size=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * 4)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        if self.downsample is not None:
            residual = self.downsample(x)

        out += residual
        out = self.relu(out)

        return out


class ResNetLW(nn.Module):

    def __init__(self, block, layers, n_classes=21):
        self.inplanes = 64
        super(ResNetLW, self).__init__()
        self.do = nn.Dropout(p=0.5)
        self.conv1 = nn.Conv2d(3, 64, kernel_size=7, stride=2, padding=3,
                               bias=False)
        self.bn1 = nn.BatchNorm2d(64)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        self.layer1 = self._make_layer(block, 64, layers[0])
        self.layer2 = self._make_layer(block, 128, layers[1], stride=2)
        self.layer3 = self._make_layer(block, 256, layers[2], stride=2)
        self.layer4 = self._make_layer(block, 512, layers[3], stride=2)
        self.p_ims1d2_outl1_dimred = conv1x1(2048, 512, bias=False)
        self.mflow_conv_g1_pool = self._make_crp(512, 512, 4)
        self.mflow_conv_g1_b3_joint_varout_dimred = conv1x1(512, 256, bias=False)
        self.p_ims1d2_outl2_dimred = conv1x1(1024, 256, bias=False)
        self.adapt_stage2_b2_joint_varout_dimred = conv1x1(256, 256, bias=False)
        self.mflow_conv_g2_pool = self._make_crp(256, 256, 4)
        self.mflow_conv_g2_b3_joint_varout_dimred = conv1x1(256, 256, bias=False)

        self.p_ims1d2_outl3_dimred = conv1x1(512, 256, bias=False)
        self.adapt_stage3_b2_joint_varout_dimred = conv1x1(256, 256, bias=False)
        self.mflow_conv_g3_pool = self._make_crp(256, 256, 4)
        self.mflow_conv_g3_b3_joint_varout_dimred = conv1x1(256, 256, bias=False)

        self.p_ims1d2_outl4_dimred = conv1x1(256, 256, bias=False)
        self.adapt_stage4_b2_joint_varout_dimred = conv1x1(256, 256, bias=False)
        self.mflow_conv_g4_pool = self._make_crp(256, 256, 4)

        self.clf_conv = nn.Conv2d(256, n_classes, kernel_size=1, stride=1,
                                  padding=1, bias=False)
        self.clf_l4_conv = nn.Conv2d(256, n_classes, kernel_size=1, stride=1,
                                  padding=1, bias=False)
        self.clf_l3_conv = nn.Conv2d(256, n_classes, kernel_size=1, stride=1,
                                  padding=1, bias=False)
        self.clf_l2_conv = nn.Conv2d(256, n_classes, kernel_size=1, stride=1,
                                  padding=1, bias=False)
        self.n_classes = n_classes

    def _make_crp(self, in_planes, out_planes, stages):
        layers = [CRPBlock(in_planes, out_planes,stages)]
        return nn.Sequential(*layers)

    def _make_layer(self, block, planes, blocks, stride=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)

    def forward(self, x):
        x_shape = x.size()
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        l1 = self.layer1(x)
        l2 = self.layer2(l1)
        l3 = self.layer3(l2)
        l4 = self.layer4(l3)

        l4 = self.do(l4)
        l3 = self.do(l3)

        x4 = self.p_ims1d2_outl1_dimred(l4)
        x4 = self.relu(x4)
        x4 = self.mflow_conv_g1_pool(x4)
        x4 = self.mflow_conv_g1_b3_joint_varout_dimred(x4)
        x4 = nn.Upsample(size=l3.size()[2:], mode='bilinear', align_corners=True)(x4)
        x4_out = self.clf_l4_conv(x4)

        x3 = self.p_ims1d2_outl2_dimred(l3)
        x3 = self.adapt_stage2_b2_joint_varout_dimred(x3)
        x3 = x3 + x4
        x3 = F.relu(x3)
        x3 = self.mflow_conv_g2_pool(x3)
        x3 = self.mflow_conv_g2_b3_joint_varout_dimred(x3)
        x3 = nn.Upsample(size=l2.size()[2:], mode='bilinear', align_corners=True)(x3)
        x3_out = self.clf_l3_conv(x3)

        x2 = self.p_ims1d2_outl3_dimred(l2)
        x2 = self.adapt_stage3_b2_joint_varout_dimred(x2)
        x2 = x2 + x3
        x2 = F.relu(x2)
        x2 = self.mflow_conv_g3_pool(x2)
        x2 = self.mflow_conv_g3_b3_joint_varout_dimred(x2)
        x2 = nn.Upsample(size=l1.size()[2:], mode='bilinear', align_corners=True)(x2)
        x2_out = self.clf_l2_conv(x2)

        x1 = self.p_ims1d2_outl4_dimred(l1)
        x1 = self.adapt_stage4_b2_joint_varout_dimred(x1)
        x1 = x1 + x2
        x1 = F.relu(x1)
        x1 = self.mflow_conv_g4_pool(x1)

        out = self.clf_conv(x1)

        if not self.training:
            out = F.upsample(out, x_shape[2:])
            out += F.upsample(x4_out, x_shape[2:])
            out += F.upsample(x3_out, x_shape[2:])
            out += F.upsample(x2_out, x_shape[2:])
            return out
        else:
            return [out, x4_out, x3_out, x2_out]

    def extract(self, x, label, layer=None):
        x_shape = x.size()
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        l1 = self.layer1(x)
        l2 = self.layer2(l1)
        l3 = self.layer3(l2)
        l4 = self.layer4(l3)

        l4 = self.do(l4)
        l3 = self.do(l3)

        x4 = self.p_ims1d2_outl1_dimred(l4)
        x4 = self.relu(x4)
        x4 = self.mflow_conv_g1_pool(x4)
        x4 = self.mflow_conv_g1_b3_joint_varout_dimred(x4)
        x4 = nn.Upsample(size=l3.size()[2:], mode='bilinear', align_corners=True)(x4)
        x4_pooled = masked_embeddings(x4.shape, label, x4, self.n_classes)

        x3 = self.p_ims1d2_outl2_dimred(l3)
        x3 = self.adapt_stage2_b2_joint_varout_dimred(x3)
        x3 = x3 + x4
        x3 = F.relu(x3)
        x3 = self.mflow_conv_g2_pool(x3)
        x3 = self.mflow_conv_g2_b3_joint_varout_dimred(x3)
        x3 = nn.Upsample(size=l2.size()[2:], mode='bilinear', align_corners=True)(x3)
        x3_pooled = masked_embeddings(x3.shape, label, x3, self.n_classes)

        x2 = self.p_ims1d2_outl3_dimred(l2)
        x2 = self.adapt_stage3_b2_joint_varout_dimred(x2)
        x2 = x2 + x3
        x2 = F.relu(x2)
        x2 = self.mflow_conv_g3_pool(x2)
        x2 = self.mflow_conv_g3_b3_joint_varout_dimred(x2)
        x2 = nn.Upsample(size=l1.size()[2:], mode='bilinear', align_corners=True)(x2)
        x2_pooled = masked_embeddings(x2.shape, label, x2, self.n_classes)

        x1 = self.p_ims1d2_outl4_dimred(l1)
        x1 = self.adapt_stage4_b2_joint_varout_dimred(x1)
        x1 = x1 + x2
        x1 = F.relu(x1)
        x1 = self.mflow_conv_g4_pool(x1)
        x1_pooled = masked_embeddings(x1.shape, label, x1, self.n_classes)

        return x1_pooled, x2_pooled, x3_pooled, x4_pooled

    def imprint(self, images, labels, alpha):
        with torch.no_grad():
            embeddings = None
            for ii, ll in zip(images, labels):
                #ii = ii.unsqueeze(0)
                ll = ll[0]
                if embeddings is None:
                    e_1, e_2, e_3, e_4 = self.extract(ii, ll)
                else:
                    e_1_, e_2_, e_3_, e_4_ = self.extract(ii, ll)
                    e_1 = torch.cat((e_1, e_1_), 0)
                    e_2 = torch.cat((e_2, e_2_), 0)
                    e_3 = torch.cat((e_3, e_3_), 0)
                    e_4 = torch.cat((e_4, e_4_), 0)

            # Imprint weights for last score layer
            nclasses = self.n_classes
            self.n_classes = 17

            nchannels = e_1.shape[2]
            weight1 = compute_weight(e_1, nclasses, labels,
                                    self.clf_conv.weight.data, alpha=alpha)
            self.clf_conv = nn.Conv2d(nchannels, self.n_classes, 1, bias=False)
            self.clf_conv.weight.data = weight1

            assert self.clf_conv.weight.is_cuda
            assert self.clf_conv.weight.data.shape[1] == 256

            nchannels = e_2.shape[2]
            weight2 = compute_weight(e_2, nclasses, labels,
                                    self.clf_l2_conv.weight.data, alpha=alpha)
            self.clf_l2_conv = nn.Conv2d(nchannels, self.n_classes, 1, bias=False)
            self.clf_l2_conv.weight.data = weight2

            assert self.clf_l2_conv.weight.is_cuda
            assert self.clf_l2_conv.weight.data.shape[1] == 256

            nchannels = e_3.shape[2]
            weight3 = compute_weight(e_3, nclasses, labels,
                    self.clf_l3_conv.weight.data, alpha=alpha)
            self.clf_l3_conv = nn.Conv2d(nchannels, self.n_classes, 1, bias=False)
            self.clf_l3_conv.weight.data = weight3

            assert self.clf_l3_conv.weight.is_cuda
            assert self.clf_l3_conv.weight.data.shape[1] == 256

            nchannels = e_4.shape[2]
            weight4 = compute_weight(e_4, nclasses, labels,
                                    self.clf_l4_conv.weight.data, alpha=alpha)
            self.clf_l4_conv = nn.Conv2d(nchannels, self.n_classes, 1, bias=False)
            self.clf_l4_conv.weight.data = weight4

            assert self.clf_l4_conv.weight.is_cuda
            assert self.clf_l4_conv.weight.data.shape[1] == 256


    def save_original_weights(self):
        self.original_weights = []
        self.original_weights.append(copy.deepcopy(self.clf_conv.weight.data))
        self.original_weights.append(copy.deepcopy(self.clf_l2_conv.weight.data))
        self.original_weights.append(copy.deepcopy(self.clf_l3_conv.weight.data))
        self.original_weights.append(copy.deepcopy(self.clf_l4_conv.weight.data))

    def reverse_imprinting(self, cl=False):
        self.n_classes = 16

        nchannels = self.clf_conv.weight.data.shape[1]
        self.clf_conv = nn.Conv2d(nchannels, self.n_classes, 1, bias=False)
        self.clf_conv.weight.data = copy.deepcopy(self.original_weights[0])
        assert self.clf_conv.weight.data.shape[1] == 256

        nchannels = self.clf_l2_conv.weight.data.shape[1]
        self.clf_l2_conv = nn.Conv2d(nchannels, self.n_classes, 1, bias=False)
        self.clf_l2_conv.weight.data = copy.deepcopy(self.original_weights[1])
        assert self.clf_l2_conv.weight.data.shape[1] == 256

        nchannels = self.clf_l3_conv.weight.data.shape[1]
        self.clf_l3_conv = nn.Conv2d(nchannels, self.n_classes, 1, bias=False)
        self.clf_l3_conv.weight.data = copy.deepcopy(self.original_weights[2])
        assert self.clf_l3_conv.weight.data.shape[1] == 256

        nchannels = self.clf_l4_conv.weight.data.shape[1]
        self.clf_l4_conv = nn.Conv2d(nchannels, self.n_classes, 1, bias=False)
        self.clf_l4_conv.weight.data = copy.deepcopy(self.original_weights[3])
        assert self.clf_l4_conv.weight.data.shape[1] == 256

def rf_lw50(n_classes, imagenet=False, pretrained=True, **kwargs):
    model = ResNetLW(Bottleneck, [3, 4, 6, 3], n_classes=n_classes, **kwargs)
    if imagenet:
        key = '50_imagenet'
        url = models_urls[key]
        model.load_state_dict(maybe_download(key, url), strict=False)
    elif pretrained:
        dataset = data_info.get(n_classes, None)
        if dataset:
            bname = '50_' + dataset.lower()
            key = 'rf_lw' + bname
            url = models_urls[bname]
            model.load_state_dict(maybe_download(key, url), strict=False)
    return model

def rf_lw101(n_classes, imagenet=False, pretrained=True, **kwargs):
    model = ResNetLW(Bottleneck, [3, 4, 23, 3], n_classes=n_classes, **kwargs)
    if imagenet:
        key = '101_imagenet'
        url = models_urls[key]
        model.load_state_dict(maybe_download(key, url), strict=False)
    elif pretrained:
        dataset = data_info.get(n_classes, None)
        if dataset:
            bname = '101_' + dataset.lower()
            key = 'rf_lw' + bname
            url = models_urls[bname]
            model.load_state_dict(maybe_download(key, url), strict=False)
    return model

def rf_lw152(n_classes, imagenet=False, pretrained=True, **kwargs):
    model = ResNetLW(Bottleneck, [3, 8, 36, 3], n_classes=n_classes, **kwargs)
    if imagenet:
        key = '152_imagenet'
        url = models_urls[key]
        model.load_state_dict(maybe_download(key, url), strict=False)
    elif pretrained:
        dataset = data_info.get(n_classes, None)
        if dataset:
            bname = '152_' + dataset.lower()
            key = 'rf_lw' + bname
            url = models_urls[bname]
            model.load_state_dict(maybe_download(key, url), strict=False)
    return model