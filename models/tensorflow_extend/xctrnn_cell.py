# -*- coding: utf-8 -*-
"""
This tensorflow extension implements ACTRNN, VCTRNN, AVCTRNN.
Heinrich et al. 2018
Updated Jan 2019 for tf_1.12
"""
import collections

from tensorflow.python.eager import context
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.keras import activations
from tensorflow.python.keras import initializers
from tensorflow.python.keras.utils import tf_utils
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import clip_ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import random_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import rnn_cell_impl
from tensorflow.python.platform import tf_logging as logging

print("Warning: model implemented for python 3.5 + tensorflow 1.12")

_BIAS_VARIABLE_NAME = rnn_cell_impl._BIAS_VARIABLE_NAME
_WEIGHTS_VARIABLE_NAME = rnn_cell_impl._WEIGHTS_VARIABLE_NAME
_MAX_TIMESCALE = 999999
_MAX_SIGMA = 500000
_ALMOST_ONE = 0.999999
_ALMOST_ZERO = 0.000001

_XCTRNNStateTuple = collections.namedtuple("XCTRNNStateTuple", ("z","y"))


@tf_export("nn.rnn_cell.XCTRNNStateTuple")
class XCTRNNStateTuple(_XCTRNNStateTuple):
    """
    Tuple used by XCTRNN Cells for `state_size`,
    `zero_state` (in outher wrappers), and as an output state.

    Stores four elements: `(z, y)`, in that order.
    Where `z` is the internal state,
    `y` is the output.

    Only used when `state_is_tuple=True`.
    """
    __slots__ = ()

    @property
    def dtype(self):
        (z, y) = self
        if z.dtype != y.dtype:
            raise TypeError("Inconsistent internal state: %s vs %s" %
                                            (str(z.dtype), str(y.dtype)))
        return z.dtype


@tf_export("nn.rnn_cell.ACTRNNCell")
class ACTRNNCell(rnn_cell_impl.LayerRNNCell):
    """Adaptive Continuous Time RNN cell.

    Args:
        num_units_v: int, the number of units in the RNN cell;
            or array, the number of units per module in the RNN cell.
        tau_v: timescale (or unit-dependent time constant of leakage).
        num_modules: int, the number of modules - only used when num_units_v not a vector
        connectivity: connection scheme in case of more than one modules
        state_is_tuple: cell maintains two state - an internal state (z).
        kernel_initializer: (optional) The initializer to use for the weight and
        projection matrices.
        bias_initializer: (optional) The initializer to use for the bias.
        w_tau_initializer: (optional) The initializer to use for the w_tau.
        use_bias: enable or disable biases.
        activation: Nonlinearity to use.    Default: `tanh`.
        reuse: (optional) Python boolean describing whether to reuse variables
            in an existing scope.    If not `True`, and the existing scope already has
            the given variables, an error is raised.
        name: String, the name of the layer. Layers with the same name will
            share weights, but to avoid mistakes we require reuse=True in such
            cases.
    """

    def __init__(self, num_units_v,
                 tau_v=1.,
                 num_modules=None,
                 connectivity='dense',
                 state_is_tuple=True,
                 kernel_initializer=None,
                 bias_initializer=None,
                 w_tau_initializer=None,
                 use_bias=True,
                 activation=None,
                 reuse=None,
                 name=None,
                 dtype=None,
                 **kwargs):
        super(ACTRNNCell, self).__init__(
            _reuse=reuse, name=name, dtype=dtype, **kwargs)

        if context.executing_eagerly() and context.num_gpus() > 0:
            logging.warn(
                "%s: Note that this cell is not optimized for performance. "
                , self)

        # Inputs must be 2-dimensional.
        self.input_spec = rnn_cell_impl.base_layer.InputSpec(ndim=2)
        self._connectivity = connectivity
        if isinstance(num_units_v, list):
            self._num_units_v = num_units_v[:]
            self._num_modules = len(num_units_v)
            self._num_units = 0
            for k in range(self._num_modules):
                self._num_units += num_units_v[k]
        else:
            self._num_units = num_units_v
            if num_modules > 1:
                self._num_modules = int(num_modules)
                self._num_units_v = [num_units_v//num_modules for k in range(num_modules)]
            else:
                self._num_modules = 1
                self._num_units_v = [num_units_v]
                self._connectivity = 'dense'

        # smallest timescale should be 1.0
        if isinstance(tau_v, list):
            if len(tau_v) != self._num_modules:
                raise ValueError("vector of tau must be of same size as "
                                 "num_modules or size of vector of num_units")
            self._tau = array_ops.constant(
                [[max(1., tau_v[k])] for k in range(self._num_modules) for n in range(self._num_units_v[k])],
                dtype=self.dtype, shape=[self._num_units],
                name="taus")
        else:
            self._tau = array_ops.constant(
                max(1., tau_v), dtype=self.dtype, shape=[self._num_units],
                name="taus")

        self._state_is_tuple = state_is_tuple
        self._kernel_initializer = initializers.get(kernel_initializer)
        self._bias_initializer = initializers.get(bias_initializer)
        self._w_tau_initializer = initializers.get(w_tau_initializer)
        self._use_bias = use_bias
        if activation:
            self._activation = activations.get(activation)
        else:
            self._activation = math_ops.tanh

        self._state_size = (XCTRNNStateTuple(self._num_units, self._num_units)
            if self._state_is_tuple else 2 * self._num_units)
        self._output_size = self._num_units

    @property
    def state_size(self):
        return self._state_size

    @property
    def output_size(self):
        return self._output_size

    @tf_utils.shape_type_conversion
    def build(self, inputs_shape):
        if inputs_shape[1].value is None:
            raise ValueError("Expected inputs.shape[-1] to be known, saw shape: %s"
                                             % inputs_shape)

        input_depth = inputs_shape[1].value

        if self._connectivity == 'partitioned':
            self._kernel_v = []
            for k in range(self._num_modules):
                self._kernel_v += [self.add_variable(
                    _WEIGHTS_VARIABLE_NAME + str(k),
                    shape=[input_depth + self._num_units_v[k],
                           self._num_units_v[k]],
                    initializer=self._kernel_initializer)]
        elif self._connectivity == 'clocked':
            self._kernel_v = []
            for k in range(self._num_modules):
                self._kernel_v += [self.add_variable(
                    _WEIGHTS_VARIABLE_NAME + str(k),
                    shape=[input_depth + sum(self._num_units_v[k:self._num_modules]),
                           self._num_units_v[k]],
                    initializer=self._initializer)]
        elif self._connectivity == 'adjacent':
            self._kernel_v = []
            for k in range(self._num_modules):
                self._kernel_v += [self.add_variable(
                    _WEIGHTS_VARIABLE_NAME + str(k),
                    shape=[input_depth + sum(self._num_units_v[max(0, k - 1):min(self._num_modules, k + 1 + 1)]),
                           self._num_units_v[k]],
                    initializer=self._kernel_initializer)]
        else:  # == 'dense'
            self._kernel_v = [self.add_variable(
                _WEIGHTS_VARIABLE_NAME,
                shape=[input_depth + self._num_units, self._num_units],
                initializer=self._kernel_initializer)]

        if self._use_bias:
            self._bias = self.add_variable(
                _BIAS_VARIABLE_NAME,
                shape=[self._num_units],
                initializer=(
                    self._bias_initializer
                    if self._bias_initializer is not None
                    else init_ops.zeros_initializer(dtype=self.dtype)))

        self._w_tau = self.add_variable(
            'wtimescales',
            shape=[self._num_units],
            initializer=(
                self._w_tau_initializer
                if self._w_tau_initializer is not None
                else init_ops.zeros_initializer(dtype=self.dtype)))

        self._log_tau = math_ops.log(self._tau - _ALMOST_ONE)

        self.built = True

    def call(self, inputs, state):

        if self._state_is_tuple:
            prev_z, prev_y = state
        else:
            prev_z, prev_y = array_ops.split(
                value=state, num_or_size_splits=2,
                axis=constant_op.constant(1, dtype=dtypes.int32))
        prev_y_v = array_ops.split(prev_y, self._num_units_v, axis=1)

        if self._connectivity == 'partitioned':
            x = array_ops.concat([math_ops.matmul(
                    array_ops.concat([inputs, prev_y_v[k]], 1),
                    self._kernel_v[k]) for k in range(self._num_modules)], 1)
        elif self._connectivity == 'clocked':
            x = array_ops.concat([math_ops.matmul(
                     array_ops.concat([inputs, array_ops.concat(prev_y_v[k:self._num_modules], 1)], 1),
                     self._kernel_v[k]) for k in range(self._num_modules)], 1)
        elif self._connectivity == 'adjacent':
            x = array_ops.concat([math_ops.matmul(
                    array_ops.concat([inputs, array_ops.concat(prev_y_v[max(0, k - 1):min(self._num_modules, k + 1 + 1)], 1)], 1),
                    self._kernel_v[k]) for k in range(self._num_modules)], 1)
        else:  # 'dense'
            x = math_ops.matmul(array_ops.concat([inputs, prev_y], 1), self._kernel_v[0])

        if self._use_bias:
            x = nn_ops.bias_add(x, self._bias)

        # the following part is the novel idea...

        tau_act = math_ops.exp(self._w_tau + self._log_tau) + _ALMOST_ONE

        # ---

        z = (1. - 1. / tau_act) * prev_z + (1. / tau_act) * x

        y = self._activation(z)

        if self._state_is_tuple:
            new_state = XCTRNNStateTuple(z, y)
        else:
            new_state = array_ops.concat([z, y], 1)
        return y, new_state

    def get_tau_sigma(self):
        if not self.built:
            return None
        tau_act = math_ops.exp(
            self._w_tau + math_ops.log(self._tau - _ALMOST_ONE)) + _ALMOST_ONE
        # in this approach we don't need sigma, but should serve the interface
        self._sigma_0 = array_ops.constant(0.,
                                       dtype=array_ops.dtypes.float32,
                                       shape=[self._num_units],
                                       name="sigmas")
        return tau_act, self._w_tau

    def get_tau(self):
        return math_ops.exp(self._w_tau + self._log_tau) + 1. \
            if self.built else None

    def get_config(self):
        config = {
            "num_units": self._num_units_v,
            "num_modules": self._num_modules,
            "tau": self._tau,
            "connectivity": self._connectivity,
            "kernel_initializer": initializers.serialize(self._kernel_initializer),
            "bias_initializer": initializers.serialize(self._bias_initializer),
            "w_tau_initializer": initializers.serialize(self._w_tau_initializer),
            "activation": activations.serialize(self._activation),
            "reuse": self._reuse,
        }
        base_config = super(ACTRNNCell, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


@tf_export("nn.rnn_cell.VCTRNNCell")
class VCTRNNCell(rnn_cell_impl.LayerRNNCell):
    """Variational Continuous Time RNN cell.

    Args:
        num_units_v: int, the number of units in the RNN cell;
            or array, the number of units per module in the RNN cell.
        tau_v: timescale (or unit-dependent time constant of leakage).
        max_sigma_v: maximal deviation of tau (0 >= max_sigma <= tau).
        num_modules: int, the number of modules - only used when num_units_v not a vector
        connectivity: connection scheme in case of more than one modules
        state_is_tuple: cell maintains two state - an internal state (z).
        kernel_initializer: (optional) The initializer to use for the weight and
        projection matrices.
        bias_initializer: (optional) The initializer to use for the bias.
        use_bias: enable or disable biases.
        activation: Nonlinearity to use.    Default: `tanh`.
        reuse: (optional) Python boolean describing whether to reuse variables
            in an existing scope.    If not `True`, and the existing scope already has
            the given variables, an error is raised.
        name: String, the name of the layer. Layers with the same name will
            share weights, but to avoid mistakes we require reuse=True in such
            cases.
    """

    def __init__(self,
                 num_units_v,
                 tau_v=1.,
                 max_sigma_v=1.,
                 num_modules=None,
                 connectivity='dense',
                 state_is_tuple=True,
                 kernel_initializer=None,
                 bias_initializer=None,
                 use_bias=True,
                 activation=None,
                 reuse=None,
                 name=None,
                 dtype=None,
                 **kwargs):
        super(VCTRNNCell, self).__init__(_reuse=reuse, name=name, dtype=dtype, **kwargs)

        if context.executing_eagerly() and context.num_gpus() > 0:
            logging.warn(
                "%s: Note that this cell is not optimized for performance. "
                , self)

        # Inputs must be 2-dimensional.
        self.input_spec = rnn_cell_impl.base_layer.InputSpec(ndim=2)
        self._connectivity = connectivity
        if isinstance(num_units_v, list):
            self._num_units_v = num_units_v[:]
            self._num_modules = len(num_units_v)
            self._num_units = 0
            for k in range(self._num_modules):
                self._num_units += num_units_v[k]
        else:
            self._num_units = num_units_v
            if num_modules > 1:
                self._num_modules = int(num_modules)
                self._num_units_v = [num_units_v//num_modules for k in range(num_modules)]
            else:
                self._num_modules = 1
                self._num_units_v = [num_units_v]
                self._connectivity = 'dense'

        # smallest timescale should be 1.0
        if isinstance(tau_v, list):
            if len(tau_v) != self._num_modules:
                raise ValueError("vector of tau must be of same size as "
                                 "num_modules or size of vector of num_units")
            self._tau = array_ops.constant(
                [[max(1., tau_v[k])] for k in range(self._num_modules) for n in range(self._num_units_v[k])],
                dtype=self.dtype, shape=[self._num_units],
                name="taus")
        else:
            self._tau = array_ops.constant(
                max(1., tau_v), dtype=self.dtype, shape=[self._num_units],
                name="taus")

        if isinstance(max_sigma_v, list):
            if len(max_sigma_v) != self._num_modules:
                raise ValueError("vector of tau must be of same size as "
                                 "num_modules or size of vector of num_units")
            self._sigma = array_ops.constant(
                [[max(0., max_sigma_v[k])*n/max(1., self._num_units_v[k] - 1)] for k in range(self._num_modules) for n in range(self._num_units_v[k])],
                dtype=self.dtype, shape=[self._num_units],
                name="sigmas")
        else:
            self._sigma = array_ops.constant(
                max(0., max_sigma_v), dtype=self.dtype, shape=[self._num_units],
                name="sigmas")

        self._state_is_tuple = state_is_tuple
        self._kernel_initializer = initializers.get(kernel_initializer)
        self._bias_initializer = initializers.get(bias_initializer)
        self._use_bias = use_bias
        if activation:
            self._activation = activations.get(activation)
        else:
            self._activation = math_ops.tanh

        self._state_size = (XCTRNNStateTuple(self._num_units, self._num_units)
            if self._state_is_tuple else 2 * self._num_units)
        self._output_size = self._num_units

    @property
    def state_size(self):
        return self._state_size

    @property
    def output_size(self):
        return self._output_size

    @tf_utils.shape_type_conversion
    def build(self, inputs_shape):
        if inputs_shape[1].value is None:
            raise ValueError("Expected inputs.shape[-1] to be known, saw shape: %s"
                                             % inputs_shape)

        input_depth = inputs_shape[1].value

        if self._connectivity == 'partitioned':
            self._kernel_v = []
            for k in range(self._num_modules):
                self._kernel_v += [self.add_variable(
                    _WEIGHTS_VARIABLE_NAME + str(k),
                    shape=[input_depth + self._num_units_v[k],
                           self._num_units_v[k]],
                    initializer=self._kernel_initializer)]
        elif self._connectivity == 'clocked':
            self._kernel_v = []
            for k in range(self._num_modules):
                self._kernel_v += [self.add_variable(
                    _WEIGHTS_VARIABLE_NAME + str(k),
                    shape=[input_depth + sum(self._num_units_v[k:self._num_modules]),
                           self._num_units_v[k]],
                    initializer=self._kernel_initializer)]
        elif self._connectivity == 'adjacent':
            self._kernel_v = []
            for k in range(self._num_modules):
                self._kernel_v += [self.add_variable(
                    _WEIGHTS_VARIABLE_NAME + str(k),
                    shape=[input_depth + sum(self._num_units_v[max(0, k - 1):min(self._num_modules, k + 1 + 1)]),
                           self._num_units_v[k]],
                    initializer=self._kernel_initializer)]
        else:  # == 'dense'
            self._kernel_v = [self.add_variable(
                _WEIGHTS_VARIABLE_NAME,
                shape=[input_depth + self._num_units, self._num_units],
                initializer=self._kernel_initializer)]

        if self._use_bias:
            self._bias = self.add_variable(
                _BIAS_VARIABLE_NAME,
                shape=[self._num_units],
                initializer=(
                    self._bias_initializer
                    if self._bias_initializer is not None
                    else init_ops.zeros_initializer(dtype=self.dtype)))

        self.built = True

    def call(self, inputs, state):

        if self._state_is_tuple:
            prev_z, prev_y = state
        else:
            prev_z, prev_y = array_ops.split(
                value=state, num_or_size_splits=2,
                axis=constant_op.constant(1, dtype=dtypes.int32))
        prev_y_v = array_ops.split(prev_y, self._num_units_v, axis=1)

        if self._connectivity == 'partitioned':
            x = array_ops.concat([math_ops.matmul(
                    array_ops.concat([inputs, prev_y_v[k]], 1),
                    self._kernel_v[k]) for k in range(self._num_modules)], 1)
        elif self._connectivity == 'clocked':
            x = array_ops.concat([math_ops.matmul(
                     array_ops.concat([inputs, array_ops.concat(prev_y_v[k:self._num_modules], 1)], 1),
                     self._kernel_v[k]) for k in range(self._num_modules)], 1)
        elif self._connectivity == 'adjacent':
            x = array_ops.concat([math_ops.matmul(
                    array_ops.concat([inputs, array_ops.concat(prev_y_v[max(0, k - 1):min(self._num_modules, k + 1 + 1)], 1)], 1),
                    self._kernel_v[k]) for k in range(self._num_modules)], 1)
        else:  # 'dense'
            x = math_ops.matmul(array_ops.concat([inputs, prev_y], 1), self._kernel_v[0])

        if self._use_bias:
            x = nn_ops.bias_add(x, self._bias)

        # the following part is the novel idea...
        epsilon = random_ops.random_normal(array_ops.shape(self._sigma), 0,
                                           1, dtype=self.dtype)

        tau_act = clip_ops.clip_by_value(self._tau + self._sigma * epsilon,
                                         1., _MAX_TIMESCALE)

        # ---

        z = (1. - 1. / tau_act) * prev_z + (1. / tau_act) * x

        y = self._activation(z)

        if self._state_is_tuple:
            new_state = XCTRNNStateTuple(z, y)
        else:
            new_state = array_ops.concat([z, y], 1)
        return y, new_state

    def get_tau_sigma(self):
        return self._tau, self._sigma

    def get_config(self):
        config = {
            "num_units": self._num_units_v,
            "num_modules": self._num_modules,
            "tau": self._tau,
            "max_sigma": self._sigma,
            "connectivity": self._connectivity,
            "kernel_initializer": initializers.serialize(self._kernel_initializer),
            "bias_initializer": initializers.serialize(self._bias_initializer),
            "activation": activations.serialize(self._activation),
            "reuse": self._reuse,
        }
        base_config = super(VCTRNNCell, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


@tf_export("nn.rnn_cell.AVCTRNNCell")
class AVCTRNNCell(rnn_cell_impl.LayerRNNCell):
    """Adaptive Variational Continuous Time RNN cell.

    Args:
        num_units_v: int, the number of units in the RNN cell;
            or array, the number of units per module in the RNN cell.
        tau_v: timescale (or unit-dependent time constant of leakage).
        max_sigma_v: maximal deviation of tau (0 >= max_sigma <= tau).
        num_modules: int, the number of modules - only used when num_units_v not a vector
        connectivity: connection scheme in case of more than one modules
        state_is_tuple: cell maintains two state - an internal state (z).
        kernel_initializer: (optional) The initializer to use for the weight and
        projection matrices.
        bias_initializer: (optional) The initializer to use for the bias.
        w_tau_initializer: (optional) The initializer to use for the w_tau.
        w_sigma_initializer: (optional) The initializer to use for the w_sigma.
        use_bias: enable or disable biases.
        activation: Nonlinearity to use.    Default: `tanh`.
        reuse: (optional) Python boolean describing whether to reuse variables
            in an existing scope.    If not `True`, and the existing scope already has
            the given variables, an error is raised.
        name: String, the name of the layer. Layers with the same name will
            share weights, but to avoid mistakes we require reuse=True in such
            cases.
    """

    def __init__(self,
                 num_units_v,
                 tau_v=1.,
                 max_sigma_v=1.,
                 num_modules=None,
                 connectivity='dense',
                 state_is_tuple=True,
                 kernel_initializer=None,
                 bias_initializer=None,
                 w_tau_initializer=None,
                 w_sigma_initializer=None,
                 use_bias=True,
                 activation=None,
                 reuse=None,
                 name=None,
                 dtype=None,
                 **kwargs):
        super(AVCTRNNCell, self).__init__(
            _reuse=reuse, name=name, dtype=dtype, **kwargs)

        # Inputs must be 2-dimensional.
        self.input_spec = rnn_cell_impl.base_layer.InputSpec(ndim=2)
        self._connectivity = connectivity
        if isinstance(num_units_v, list):
            self._num_units_v = num_units_v[:]
            self._num_modules = len(num_units_v)
            self._num_units = 0
            for k in range(self._num_modules):
                self._num_units += num_units_v[k]
        else:
            self._num_units = num_units_v
            if num_modules > 1:
                self._num_modules = int(num_modules)
                self._num_units_v = [num_units_v//num_modules for k in range(num_modules)]
            else:
                self._num_modules = 1
                self._num_units_v = [num_units_v]
                self._connectivity = 'dense'

        # smallest timescale should be 1.0
        if isinstance(tau_v, list):
            if len(tau_v) != self._num_modules:
                raise ValueError("vector of tau must be of same size as "
                                 "num_modules or size of vector of num_units")
            self._tau = array_ops.constant(
                [[max(1., tau_v[k])] for k in range(self._num_modules) for n in range(self._num_units_v[k])],
                dtype=self.dtype, shape=[self._num_units],
                name="taus")
        else:
            self._tau = array_ops.constant(
                max(1., tau_v), dtype=self.dtype, shape=[self._num_units],
                name="taus")

        if isinstance(max_sigma_v, list):
            if len(max_sigma_v) != self._num_modules:
                raise ValueError("vector of tau must be of same size as "
                                 "num_modules or size of vector of num_units")
            self._sigma = array_ops.constant(
                [[max(0., max_sigma_v[k])*n/max(1., self._num_units_v[k] - 1)] for k in range(self._num_modules) for n in range(self._num_units_v[k])],
                dtype=self.dtype, shape=[self._num_units],
                name="sigmas")
        else:
            self._sigma = array_ops.constant(
                max(0., max_sigma_v), dtype=self.dtype, shape=[self._num_units],
                name="sigmas")

        self._state_is_tuple = state_is_tuple
        self._kernel_initializer = initializers.get(kernel_initializer)
        self._bias_initializer = initializers.get(bias_initializer)
        self._w_tau_initializer = initializers.get(w_tau_initializer)
        self._w_sigma_initializer = initializers.get(w_sigma_initializer)
        self._use_bias = use_bias
        if activation:
            self._activation = activations.get(activation)
        else:
            self._activation = math_ops.tanh

        self._state_size = (XCTRNNStateTuple(self._num_units, self._num_units)
            if self._state_is_tuple else 2 * self._num_units)
        self._output_size = self._num_units

    @property
    def state_size(self):
        return self._state_size

    @property
    def output_size(self):
        return self._output_size

    @tf_utils.shape_type_conversion
    def build(self, inputs_shape):
        if inputs_shape[1].value is None:
            raise ValueError("Expected inputs.shape[-1] to be known, saw shape: %s"
                                             % inputs_shape)

        input_depth = inputs_shape[1].value

        if self._connectivity == 'partitioned':
            self._kernel_v = []
            for k in range(self._num_modules):
                self._kernel_v += [self.add_variable(
                    _WEIGHTS_VARIABLE_NAME + str(k),
                    shape=[input_depth + self._num_units_v[k],
                           self._num_units_v[k]],
                    initializer=self._kernel_initializer)]
        elif self._connectivity == 'clocked':
            self._kernel_v = []
            for k in range(self._num_modules):
                self._kernel_v += [self.add_variable(
                    _WEIGHTS_VARIABLE_NAME + str(k),
                    shape=[input_depth + sum(self._num_units_v[k:self._num_modules]),
                           self._num_units_v[k]],
                    initializer=self._kernel_initializer)]
        elif self._connectivity == 'adjacent':
            self._kernel_v = []
            for k in range(self._num_modules):
                self._kernel_v += [self.add_variable(
                    _WEIGHTS_VARIABLE_NAME + str(k),
                    shape=[input_depth + sum(self._num_units_v[max(0, k - 1):min(self._num_modules, k + 1 + 1)]),
                           self._num_units_v[k]],
                    initializer=self._kernel_initializer)]
        else:  # == 'dense'
            self._kernel_v = [self.add_variable(
                _WEIGHTS_VARIABLE_NAME,
                shape=[input_depth + self._num_units, self._num_units],
                initializer=self._kernel_initializer)]

        if self._use_bias:
            self._bias = self.add_variable(
                _BIAS_VARIABLE_NAME,
                shape=[self._num_units],
                initializer=(
                    self._bias_initializer
                    if self._bias_initializer is not None
                    else init_ops.zeros_initializer(dtype=self.dtype)))

        self._w_tau = self.add_variable(
            'wtimescales',
            shape=[self._num_units],
            initializer=(
                self._w_tau_initializer
                if self._w_tau_initializer is not None
                else init_ops.zeros_initializer(dtype=self.dtype)))

        self._w_sigma = self.add_variable(
            'wsigmas',
            shape=[self._num_units],
            initializer=(
                self._w_sigma_initializer
                if self._w_sigma_initializer is not None
                else init_ops.zeros_initializer(dtype=self.dtype)))

        self.built = True

    def call(self, inputs, state):

        if self._state_is_tuple:
            prev_z, prev_y = state
        else:
            prev_z, prev_y = array_ops.split(
                value=state, num_or_size_splits=2,
                axis=constant_op.constant(1, dtype=dtypes.int32))
        prev_y_v = array_ops.split(prev_y, self._num_units_v, axis=1)

        if self._connectivity == 'partitioned':
            x = array_ops.concat([math_ops.matmul(
                    array_ops.concat([inputs, prev_y_v[k]], 1),
                    self._kernel_v[k]) for k in range(self._num_modules)], 1)
        elif self._connectivity == 'clocked':
            x = array_ops.concat([math_ops.matmul(
                     array_ops.concat([inputs, array_ops.concat(prev_y_v[k:self._num_modules], 1)], 1),
                     self._kernel_v[k]) for k in range(self._num_modules)], 1)
        elif self._connectivity == 'adjacent':
            x = array_ops.concat([math_ops.matmul(
                    array_ops.concat([inputs, array_ops.concat(prev_y_v[max(0, k - 1):min(self._num_modules, k + 1 + 1)], 1)], 1),
                    self._kernel_v[k]) for k in range(self._num_modules)], 1)
        else:  # 'dense'
            x = math_ops.matmul(array_ops.concat([inputs, prev_y], 1), self._kernel_v[0])

        if self._use_bias:
            x = nn_ops.bias_add(x, self._bias)

        # the following part is the novel idea...

        sigma_act = clip_ops.clip_by_value(
            math_ops.exp(
                self._w_sigma + math_ops.log(self._sigma + _ALMOST_ZERO))
            - _ALMOST_ZERO,
            0., _MAX_SIGMA)

        epsilon = random_ops.random_normal(array_ops.shape(sigma_act),
                                           0, 1, dtype=self.dtype)

        tau_act = math_ops.exp(
            self._w_tau + math_ops.log(
                clip_ops.clip_by_value(
                    self._tau + sigma_act * epsilon,
                    1., _MAX_TIMESCALE) - _ALMOST_ONE)) + _ALMOST_ONE

        # ---

        z = (1. - 1. / tau_act) * prev_z + (1. / tau_act) * x

        y = self._activation(z)

        if self._state_is_tuple:
            new_state = XCTRNNStateTuple(z, y)
        else:
            new_state = array_ops.concat([z, y], 1)
        return y, new_state

    def get_tau_sigma(self):
        # here we just return the mean tau_act:
        tau_act = math_ops.exp(
            self._w_tau + math_ops.log(self._tau - _ALMOST_ONE)) + _ALMOST_ONE
        sigma_act = clip_ops.clip_by_value(
            math_ops.exp(
                self._w_sigma + math_ops.log(self._sigma + _ALMOST_ZERO))
            - _ALMOST_ZERO, 0., _MAX_SIGMA)
        return tau_act, sigma_act if self.built else None

    def get_config(self):
        config = {
            "num_units": self._num_units_v,
            "num_modules": self._num_modules,
            "tau": self._tau,
            "sigma": self._sigma,
            "connectivity": self._connectivity,
            "kernel_initializer": initializers.serialize(self._kernel_initializer),
            "bias_initializer": initializers.serialize(self._bias_initializer),
            "w_tau_initializer": initializers.serialize(self._w_tau_initializer),
            "w_sigma_initializer": initializers.serialize(self._w_sigma_initializer),
            "activation": activations.serialize(self._activation),
            "reuse": self._reuse,
        }
        base_config = super(AVCTRNNCell, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))
