# Copyright (c) 2019  PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import sys
sys.path.append("../../")
import unittest
import time
import numpy as np
import paddle
from paddle.static import InputSpec as Input
from paddleslim.dygraph import L1NormFilterPruner, L2NormFilterPruner, FPGMFilterPruner
from paddleslim.dygraph import Status


class TestStatus(unittest.TestCase):
    def runTest(self):
        status = Status()
        status.sensitivies = {
            "conv2d_1.weights": {
                0.1: 0.11,
                0.2: 0.22,
                0.3: 0.33
            }
        }
        local_file = "./sen_{}.pickle".format(time.time())
        status.save(local_file)
        status1 = Status(local_file)
        for _name in status.sensitivies:
            for _ratio, _loss in status.sensitivies[_name].items():
                self.assertTrue(status1.sensitivies[_name][_ratio], _loss)


class TestFilterPruner(unittest.TestCase):
    def __init__(self, methodName='runTest', param_names=[]):
        super(TestFilterPruner, self).__init__(methodName)
        self._param_names = param_names
        transform = paddle.vision.transforms.Compose([
            paddle.vision.transforms.Transpose(),
            paddle.vision.transforms.Normalize([127.5], [127.5])
        ])
        self.train_dataset = paddle.vision.datasets.MNIST(
            mode="train", backend="cv2", transform=transform)
        self.val_dataset = paddle.vision.datasets.MNIST(
            mode="test", backend="cv2", transform=transform)

        def _reader():
            for data in self.val_dataset:
                yield data

        self.val_reader = _reader

    def runTest(self):
        paddle.disable_static()
        net = paddle.vision.models.LeNet()
        optimizer = paddle.optimizer.Adam(
            learning_rate=0.001, parameters=net.parameters())
        inputs = [Input([None, 1, 28, 28], 'float32', name='image')]
        labels = [Input([None, 1], 'int64', name='label')]
        model = paddle.Model(net, inputs, labels)
        model.prepare(
            optimizer,
            paddle.nn.CrossEntropyLoss(),
            paddle.metric.Accuracy(topk=(1, 5)))
        model.fit(self.train_dataset, epochs=1, batch_size=128, verbose=1)
        pruners = []
        pruner = L1NormFilterPruner(net, [1, 1, 28, 28], opt=optimizer)
        pruners.append(pruner)
        pruner = FPGMFilterPruner(net, [1, 1, 28, 28], opt=optimizer)
        pruners.append(pruner)
        pruner = L2NormFilterPruner(net, [1, 1, 28, 28], opt=optimizer)
        pruners.append(pruner)

        def eval_fn():
            result = model.evaluate(self.val_dataset, batch_size=128, verbose=1)
            return result['acc_top1']

        sen_file = "_".join(["./dygraph_sen_", str(time.time())])
        for pruner in pruners:
            sen = pruner.sensitive(
                eval_func=eval_fn,
                sen_file=sen_file,
                target_vars=self._param_names)
            model.fit(self.train_dataset, epochs=1, batch_size=128, verbose=1)
            base_acc = eval_fn()
            plan = pruner.sensitive_prune(0.01)
            pruner.restore()
            restore_acc = eval_fn()
            self.assertTrue(restore_acc == base_acc)

            plan = pruner.sensitive_prune(0.01, align=4)
            for param in net.parameters():
                if param.name in self._param_names:
                    print(f"name: {param.name}; shape: {param.shape}")
                    self.assertTrue(param.shape[0] % 4 == 0)
            pruner.restore()
        paddle.enable_static()


class TestPruningGroupConv2d(unittest.TestCase):
    def __init__(self, methodName='runTest'):
        super(TestPruningGroupConv2d, self).__init__(methodName)

    def runTest(self):
        paddle.disable_static()
        net = paddle.vision.models.mobilenet_v1()
        ratios = {}
        for param in net.parameters():
            if len(param.shape) == 4:
                ratios[param.name] = 0.5
        pruners = []
        pruner = L1NormFilterPruner(net, [1, 3, 128, 128])
        pruners.append(pruner)
        pruner = FPGMFilterPruner(net, [1, 3, 128, 128])
        pruners.append(pruner)
        pruner = L2NormFilterPruner(net, [1, 3, 128, 128])
        pruners.append(pruner)

        shapes = {}
        for pruner in pruners:
            plan = pruner.prune_vars(ratios, 0)
            for param in net.parameters():
                if param.name not in shapes:
                    shapes[param.name] = param.shape
                self.assertTrue(shapes[param.name] == param.shape)
            pruner.restore()
        paddle.enable_static()


class MulNet(paddle.nn.Layer):
    """
    [3, 36] X conv(x)
    """

    def __init__(self):
        super(MulNet, self).__init__()
        self.conv_a = paddle.nn.Conv2D(6, 6, 1)
        self.b = self.create_parameter(
            shape=[3, 36], attr=paddle.ParamAttr(name="b"))

    def forward(self, x):
        conv_a = self.conv_a(x)
        return paddle.fluid.layers.mul(self.b,
                                       conv_a,
                                       x_num_col_dims=1,
                                       y_num_col_dims=3)


class TestPruningMul(unittest.TestCase):
    def __init__(self, methodName='runTest'):
        super(TestPruningMul, self).__init__(methodName)

    def runTest(self):
        paddle.disable_static()
        net = MulNet()
        ratios = {}
        ratios['conv2d_0.w_0'] = 0.5
        pruners = []
        pruner = L1NormFilterPruner(net, [2, 6, 3, 3], skip_leaves=False)
        pruners.append(pruner)
        pruner = FPGMFilterPruner(net, [2, 6, 3, 3], skip_leaves=False)
        pruners.append(pruner)
        pruner = L2NormFilterPruner(net, [2, 6, 3, 3], skip_leaves=False)
        pruners.append(pruner)

        shapes = {
            'b': [3, 18],
            'conv2d_0.w_0': [3, 6, 1, 1],
            'conv2d_0.b_0': [3]
        }
        for pruner in pruners:
            plan = pruner.prune_vars(ratios, 0)
            for param in net.parameters():
                if param.name not in shapes:
                    shapes[param.name] = param.shape
                print(
                    f"name {param.name}: {param.shape}, excepted: {shapes[param.name]}"
                )
                self.assertTrue(shapes[param.name] == param.shape)
            pruner.restore()
        paddle.enable_static()


def add_cases(suite):
    # suite.addTest(TestStatus())
    # suite.addTest(TestFilterPruner(param_names=["conv2d_0.w_0"]))
    # suite.addTest(TestPruningGroupConv2d())
    suite.addTest(TestPruningMul())


def load_tests(loader, standard_tests, pattern):
    suite = unittest.TestSuite()
    add_cases(suite)
    return suite


if __name__ == '__main__':
    unittest.main()
