# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
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
# ==============================================================================
"""Model definitions for simple speech recognition.

"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math

import tensorflow as tf


def prepare_model_settings(label_count, sample_rate, clip_duration_ms,
                           window_size_ms, window_stride_ms,
                           dct_coefficient_count):
    """Calculates common settings needed for all models.

    Args:
      label_count: How many classes are to be recognized.
      sample_rate: Number of audio samples per second.
      clip_duration_ms: Length of each audio clip to be analyzed.
      window_size_ms: Duration of frequency analysis window.
      window_stride_ms: How far to move in time between frequency windows.
      dct_coefficient_count: Number of frequency bins to use for analysis.

    Returns:
      Dictionary containing common settings.
    """
    desired_samples = int(sample_rate * clip_duration_ms / 1000)
    window_size_samples = int(sample_rate * window_size_ms / 1000)
    window_stride_samples = int(sample_rate * window_stride_ms / 1000)
    length_minus_window = (desired_samples - window_size_samples)
    if length_minus_window < 0:
        spectrogram_length = 0
    else:
        spectrogram_length = 1 + int(length_minus_window / window_stride_samples)
    fingerprint_size = dct_coefficient_count * spectrogram_length
    return {
        'desired_samples': desired_samples,
        'window_size_samples': window_size_samples,
        'window_stride_samples': window_stride_samples,
        'spectrogram_length': spectrogram_length,
        'dct_coefficient_count': dct_coefficient_count,
        'fingerprint_size': fingerprint_size,
        'label_count': label_count,
        'sample_rate': sample_rate,
    }


def create_model(fingerprint_input, model_settings, model_architecture,
                 is_training, runtime_settings=None):
    """Builds a model of the requested architecture compatible with the settings.

    There are many possible ways of deriving predictions from a spectrogram
    input, so this function provides an abstract interface for creating different
    kinds of models in a black-box way. You need to pass in a TensorFlow node as
    the 'fingerprint' input, and this should output a batch of 1D features that
    describe the audio. Typically this will be derived from a spectrogram that's
    been run through an MFCC, but in theory it can be any feature vector of the
    size specified in model_settings['fingerprint_size'].

    The function will build the graph it needs in the current TensorFlow graph,
    and return the tensorflow output that will contain the 'logits' input to the
    softmax prediction process. If training flag is on, it will also return a
    placeholder node that can be used to control the dropout amount.

    See the implementations below for the possible model architectures that can be
    requested.

    Args:
      fingerprint_input: TensorFlow node that will output audio feature vectors.
      model_settings: Dictionary of information about the model.
      model_architecture: String specifying which kind of model to create.
      is_training: Whether the model is going to be used for training.
      runtime_settings: Dictionary of information about the runtime.

    Returns:
      TensorFlow node outputting logits results, and optionally a dropout
      placeholder.

    Raises:
      Exception: If the architecture type isn't recognized.
    """
    if model_architecture == 'single_fc':
        return create_single_fc_model(fingerprint_input, model_settings,
                                      is_training)
    elif model_architecture == 'conv':
        return create_conv_model(fingerprint_input, model_settings, is_training)
    elif model_architecture == 'low_latency_conv':
        return create_low_latency_conv_model(fingerprint_input, model_settings,
                                             is_training)
    elif model_architecture == 'low_latency_svdf':
        return create_low_latency_svdf_model(fingerprint_input, model_settings,
                                             is_training, runtime_settings)
    elif model_architecture == 'deepear_v01':
        return create_deepear_v01_model(fingerprint_input, model_settings,
                                        is_training, 4)
    elif model_architecture == 'deepear_v02':
        return create_deepear_v01_model(fingerprint_input, model_settings,
                                        is_training, 1)
    elif model_architecture == 'alexnet_v01':
        return create_alexnet_v01_model(fingerprint_input, model_settings,
                                        is_training)
    elif model_architecture == 'alexnet_adapt':
        return create_alexnet_adapt_model(fingerprint_input, model_settings,
                                          is_training)

    else:
        raise Exception('model_architecture argument "' + model_architecture +
                        '" not recognized, should be one of "single_fc", "conv",' +
                        ' "low_latency_conv, or "low_latency_svdf"')


def load_variables_from_checkpoint(sess, start_checkpoint):
    """Utility function to centralize checkpoint restoration.

    Args:
      sess: TensorFlow session.
      start_checkpoint: Path to saved checkpoint on disk.
    """
    saver = tf.train.Saver(tf.global_variables())
    saver.restore(sess, start_checkpoint)


def create_single_fc_model(fingerprint_input, model_settings, is_training):
    """Builds a model with a single hidden fully-connected layer.

    This is a very simple model with just one matmul and bias layer. As you'd
    expect, it doesn't produce very accurate results, but it is very fast and
    simple, so it's useful for sanity testing.

    Here's the layout of the graph:

    (fingerprint_input)
            v
        [MatMul]<-(weights)
            v
        [BiasAdd]<-(bias)
            v

    Args:
      fingerprint_input: TensorFlow node that will output audio feature vectors.
      model_settings: Dictionary of information about the model.
      is_training: Whether the model is going to be used for training.

    Returns:
      TensorFlow node outputting logits results, and optionally a dropout
      placeholder.
    """
    if is_training:
        dropout_prob = tf.placeholder(tf.float32, name='dropout_prob')
    fingerprint_size = model_settings['fingerprint_size']
    label_count = model_settings['label_count']
    weights = tf.Variable(
        tf.truncated_normal([fingerprint_size, label_count], stddev=0.001))
    bias = tf.Variable(tf.zeros([label_count]))
    logits = tf.matmul(fingerprint_input, weights) + bias
    if is_training:
        return logits, dropout_prob
    else:
        return logits


def create_conv_model(fingerprint_input, model_settings, is_training):
    """Builds a standard convolutional model.

    This is roughly the network labeled as 'cnn-trad-fpool3' in the
    'Convolutional Neural Networks for Small-footprint Keyword Spotting' paper:
    http://www.isca-speech.org/archive/interspeech_2015/papers/i15_1478.pdf

    Here's the layout of the graph:

    (fingerprint_input)
            v
        [Conv2D]<-(weights)
            v
        [BiasAdd]<-(bias)
            v
          [Relu]
            v
        [MaxPool]
            v
        [Conv2D]<-(weights)
            v
        [BiasAdd]<-(bias)
            v
          [Relu]
            v
        [MaxPool]
            v
        [MatMul]<-(weights)
            v
        [BiasAdd]<-(bias)
            v

    This produces fairly good quality results, but can involve a large number of
    weight parameters and computations. For a cheaper alternative from the same
    paper with slightly less accuracy, see 'low_latency_conv' below.

    During training, dropout nodes are introduced after each relu, controlled by a
    placeholder.

    Args:
      fingerprint_input: TensorFlow node that will output audio feature vectors.
      model_settings: Dictionary of information about the model.
      is_training: Whether the model is going to be used for training.

    Returns:
      TensorFlow node outputting logits results, and optionally a dropout
      placeholder.
    """
    if is_training:
        dropout_prob = tf.placeholder(tf.float32, name='dropout_prob')
    input_frequency_size = model_settings['dct_coefficient_count']
    input_time_size = model_settings['spectrogram_length']
    fingerprint_4d = tf.reshape(fingerprint_input,
                                [-1, input_time_size, input_frequency_size, 1])
    first_filter_width = 8
    first_filter_height = 20
    first_filter_count = 64
    first_weights = tf.Variable(
        tf.truncated_normal(
            [first_filter_height, first_filter_width, 1, first_filter_count],
            stddev=0.01))
    first_bias = tf.Variable(tf.zeros([first_filter_count]))
    first_conv = tf.nn.conv2d(fingerprint_4d, first_weights, [1, 1, 1, 1],
                              'SAME') + first_bias
    print("first conv shape", first_conv.get_shape())
    first_relu = tf.nn.relu(first_conv)
    if is_training:
        first_dropout = tf.nn.dropout(first_relu, dropout_prob)
    else:
        first_dropout = first_relu
    max_pool = tf.nn.max_pool(first_dropout, [1, 2, 2, 1], [1, 2, 2, 1], 'SAME')
    print("first pool conv shape", max_pool.get_shape())
    second_filter_width = 4
    second_filter_height = 10
    second_filter_count = 64
    second_weights = tf.Variable(
        tf.truncated_normal(
            [
                second_filter_height, second_filter_width, first_filter_count,
                second_filter_count
            ],
            stddev=0.01))
    second_bias = tf.Variable(tf.zeros([second_filter_count]))
    second_conv = tf.nn.conv2d(max_pool, second_weights, [1, 1, 1, 1],
                               'SAME') + second_bias
    print("2nd conv shape", second_conv.get_shape())
    second_relu = tf.nn.relu(second_conv)
    if is_training:
        second_dropout = tf.nn.dropout(second_relu, dropout_prob)
    else:
        second_dropout = second_relu
    second_conv_shape = second_dropout.get_shape()
    second_conv_output_width = second_conv_shape[2]
    second_conv_output_height = second_conv_shape[1]
    second_conv_element_count = int(
        second_conv_output_width * second_conv_output_height *
        second_filter_count)
    flattened_second_conv = tf.reshape(second_dropout,
                                       [-1, second_conv_element_count])
    print("flattened shape", flattened_second_conv.get_shape())
    label_count = model_settings['label_count']
    print("num neurons", second_conv_element_count)
    final_fc_weights = tf.Variable(
        tf.truncated_normal(
            [second_conv_element_count, label_count], stddev=0.01))
    final_fc_bias = tf.Variable(tf.zeros([label_count]))
    final_fc = tf.matmul(flattened_second_conv, final_fc_weights) + final_fc_bias
    print("final fc", final_fc.get_shape())
    if is_training:
        return final_fc, dropout_prob
    else:
        return final_fc


def create_low_latency_conv_model(fingerprint_input, model_settings,
                                  is_training):
    """Builds a convolutional model with low compute requirements.

    This is roughly the network labeled as 'cnn-one-fstride4' in the
    'Convolutional Neural Networks for Small-footprint Keyword Spotting' paper:
    http://www.isca-speech.org/archive/interspeech_2015/papers/i15_1478.pdf

    Here's the layout of the graph:

    (fingerprint_input)
            v
        [Conv2D]<-(weights)
            v
        [BiasAdd]<-(bias)
            v
          [Relu]
            v
        [MatMul]<-(weights)
            v
        [BiasAdd]<-(bias)
            v
        [MatMul]<-(weights)
            v
        [BiasAdd]<-(bias)
            v
        [MatMul]<-(weights)
            v
        [BiasAdd]<-(bias)
            v

    This produces slightly lower quality results than the 'conv' model, but needs
    fewer weight parameters and computations.

    During training, dropout nodes are introduced after the relu, controlled by a
    placeholder.

    Args:
      fingerprint_input: TensorFlow node that will output audio feature vectors.
      model_settings: Dictionary of information about the model.
      is_training: Whether the model is going to be used for training.

    Returns:
      TensorFlow node outputting logits results, and optionally a dropout
      placeholder.
    """
    if is_training:
        dropout_prob = tf.placeholder(tf.float32, name='dropout_prob')
    input_frequency_size = model_settings['dct_coefficient_count']
    input_time_size = model_settings['spectrogram_length']
    fingerprint_4d = tf.reshape(fingerprint_input,
                                [-1, input_time_size, input_frequency_size, 1])
    first_filter_width = 8
    first_filter_height = input_time_size
    first_filter_count = 186
    first_filter_stride_x = 1
    first_filter_stride_y = 1
    first_weights = tf.Variable(
        tf.truncated_normal(
            [first_filter_height, first_filter_width, 1, first_filter_count],
            stddev=0.01))
    first_bias = tf.Variable(tf.zeros([first_filter_count]))
    first_conv = tf.nn.conv2d(fingerprint_4d, first_weights, [
        1, first_filter_stride_y, first_filter_stride_x, 1
    ], 'VALID') + first_bias
    first_relu = tf.nn.relu(first_conv)
    if is_training:
        first_dropout = tf.nn.dropout(first_relu, dropout_prob)
    else:
        first_dropout = first_relu
    first_conv_output_width = math.floor(
        (input_frequency_size - first_filter_width + first_filter_stride_x) /
        first_filter_stride_x)
    first_conv_output_height = math.floor(
        (input_time_size - first_filter_height + first_filter_stride_y) /
        first_filter_stride_y)
    first_conv_element_count = int(
        first_conv_output_width * first_conv_output_height * first_filter_count)
    flattened_first_conv = tf.reshape(first_dropout,
                                      [-1, first_conv_element_count])
    first_fc_output_channels = 128
    first_fc_weights = tf.Variable(
        tf.truncated_normal(
            [first_conv_element_count, first_fc_output_channels], stddev=0.01))
    first_fc_bias = tf.Variable(tf.zeros([first_fc_output_channels]))
    first_fc = tf.matmul(flattened_first_conv, first_fc_weights) + first_fc_bias
    if is_training:
        second_fc_input = tf.nn.dropout(first_fc, dropout_prob)
    else:
        second_fc_input = first_fc
    second_fc_output_channels = 128
    second_fc_weights = tf.Variable(
        tf.truncated_normal(
            [first_fc_output_channels, second_fc_output_channels], stddev=0.01))
    second_fc_bias = tf.Variable(tf.zeros([second_fc_output_channels]))
    second_fc = tf.matmul(second_fc_input, second_fc_weights) + second_fc_bias
    if is_training:
        final_fc_input = tf.nn.dropout(second_fc, dropout_prob)
    else:
        final_fc_input = second_fc
    label_count = model_settings['label_count']
    final_fc_weights = tf.Variable(
        tf.truncated_normal(
            [second_fc_output_channels, label_count], stddev=0.01))
    final_fc_bias = tf.Variable(tf.zeros([label_count]))
    final_fc = tf.matmul(final_fc_input, final_fc_weights) + final_fc_bias
    if is_training:
        return final_fc, dropout_prob
    else:
        return final_fc


def create_low_latency_svdf_model(fingerprint_input, model_settings,
                                  is_training, runtime_settings):
    """Builds an SVDF model with low compute requirements.

    This is based in the topology presented in the 'Compressing Deep Neural
    Networks using a Rank-Constrained Topology' paper:
    https://static.googleusercontent.com/media/research.google.com/en//pubs/archive/43813.pdf

    Here's the layout of the graph:

    (fingerprint_input)
            v
          [SVDF]<-(weights)
            v
        [BiasAdd]<-(bias)
            v
          [Relu]
            v
        [MatMul]<-(weights)
            v
        [BiasAdd]<-(bias)
            v
        [MatMul]<-(weights)
            v
        [BiasAdd]<-(bias)
            v
        [MatMul]<-(weights)
            v
        [BiasAdd]<-(bias)
            v

    This model produces lower recognition accuracy than the 'conv' model above,
    but requires fewer weight parameters and, significantly fewer computations.

    During training, dropout nodes are introduced after the relu, controlled by a
    placeholder.

    Args:
      fingerprint_input: TensorFlow node that will output audio feature vectors.
      The node is expected to produce a 2D Tensor of shape:
        [batch, model_settings['dct_coefficient_count'] *
                model_settings['spectrogram_length']]
      with the features corresponding to the same time slot arranged contiguously,
      and the oldest slot at index [:, 0], and newest at [:, -1].
      model_settings: Dictionary of information about the model.
      is_training: Whether the model is going to be used for training.
      runtime_settings: Dictionary of information about the runtime.

    Returns:
      TensorFlow node outputting logits results, and optionally a dropout
      placeholder.

    Raises:
        ValueError: If the inputs tensor is incorrectly shaped.
    """
    if is_training:
        dropout_prob = tf.placeholder(tf.float32, name='dropout_prob')

    input_frequency_size = model_settings['dct_coefficient_count']
    input_time_size = model_settings['spectrogram_length']

    # Validation.
    input_shape = fingerprint_input.get_shape()
    if len(input_shape) != 2:
        raise ValueError('Inputs to `SVDF` should have rank == 2.')
    if input_shape[-1].value is None:
        raise ValueError('The last dimension of the inputs to `SVDF` '
                         'should be defined. Found `None`.')
    if input_shape[-1].value % input_frequency_size != 0:
        raise ValueError('Inputs feature dimension %d must be a multiple of '
                         'frame size %d', fingerprint_input.shape[-1].value,
                         input_frequency_size)

    # Set number of units (i.e. nodes) and rank.
    rank = 2
    num_units = 1280
    # Number of filters: pairs of feature and time filters.
    num_filters = rank * num_units
    # Create the runtime memory: [num_filters, batch, input_time_size]
    batch = 1
    memory = tf.Variable(tf.zeros([num_filters, batch, input_time_size]),
                         trainable=False, name='runtime-memory')
    # Determine the number of new frames in the input, such that we only operate
    # on those. For training we do not use the memory, and thus use all frames
    # provided in the input.
    # new_fingerprint_input: [batch, num_new_frames*input_frequency_size]
    if is_training:
        num_new_frames = input_time_size
    else:
        window_stride_ms = int(model_settings['window_stride_samples'] * 1000 /
                               model_settings['sample_rate'])
        num_new_frames = tf.cond(
            tf.equal(tf.count_nonzero(memory), 0),
            lambda: input_time_size,
            lambda: int(runtime_settings['clip_stride_ms'] / window_stride_ms))
    new_fingerprint_input = fingerprint_input[
                            :, -num_new_frames * input_frequency_size:]
    # Expand to add input channels dimension.
    new_fingerprint_input = tf.expand_dims(new_fingerprint_input, 2)

    # Create the frequency filters.
    weights_frequency = tf.Variable(
        tf.truncated_normal([input_frequency_size, num_filters], stddev=0.01))
    # Expand to add input channels dimensions.
    # weights_frequency: [input_frequency_size, 1, num_filters]
    weights_frequency = tf.expand_dims(weights_frequency, 1)
    # Convolve the 1D feature filters sliding over the time dimension.
    # activations_time: [batch, num_new_frames, num_filters]
    activations_time = tf.nn.conv1d(
        new_fingerprint_input, weights_frequency, input_frequency_size, 'VALID')
    # Rearrange such that we can perform the batched matmul.
    # activations_time: [num_filters, batch, num_new_frames]
    activations_time = tf.transpose(activations_time, perm=[2, 0, 1])

    # Runtime memory optimization.
    if not is_training:
        # We need to drop the activations corresponding to the oldest frames, and
        # then add those corresponding to the new frames.
        new_memory = memory[:, :, num_new_frames:]
        new_memory = tf.concat([new_memory, activations_time], 2)
        tf.assign(memory, new_memory)
        activations_time = new_memory

    # Create the time filters.
    weights_time = tf.Variable(
        tf.truncated_normal([num_filters, input_time_size], stddev=0.01))
    # Apply the time filter on the outputs of the feature filters.
    # weights_time: [num_filters, input_time_size, 1]
    # outputs: [num_filters, batch, 1]
    weights_time = tf.expand_dims(weights_time, 2)
    outputs = tf.matmul(activations_time, weights_time)
    # Split num_units and rank into separate dimensions (the remaining
    # dimension is the input_shape[0] -i.e. batch size). This also squeezes
    # the last dimension, since it's not used.
    # [num_filters, batch, 1] => [num_units, rank, batch]
    outputs = tf.reshape(outputs, [num_units, rank, -1])
    # Sum the rank outputs per unit => [num_units, batch].
    units_output = tf.reduce_sum(outputs, axis=1)
    # Transpose to shape [batch, num_units]
    units_output = tf.transpose(units_output)

    # Appy bias.
    bias = tf.Variable(tf.zeros([num_units]))
    first_bias = tf.nn.bias_add(units_output, bias)

    # Relu.
    first_relu = tf.nn.relu(first_bias)

    if is_training:
        first_dropout = tf.nn.dropout(first_relu, dropout_prob)
    else:
        first_dropout = first_relu

    first_fc_output_channels = 256
    first_fc_weights = tf.Variable(
        tf.truncated_normal([num_units, first_fc_output_channels], stddev=0.01))
    first_fc_bias = tf.Variable(tf.zeros([first_fc_output_channels]))
    first_fc = tf.matmul(first_dropout, first_fc_weights) + first_fc_bias
    if is_training:
        second_fc_input = tf.nn.dropout(first_fc, dropout_prob)
    else:
        second_fc_input = first_fc
    second_fc_output_channels = 256
    second_fc_weights = tf.Variable(
        tf.truncated_normal(
            [first_fc_output_channels, second_fc_output_channels], stddev=0.01))
    second_fc_bias = tf.Variable(tf.zeros([second_fc_output_channels]))
    second_fc = tf.matmul(second_fc_input, second_fc_weights) + second_fc_bias
    if is_training:
        final_fc_input = tf.nn.dropout(second_fc, dropout_prob)
    else:
        final_fc_input = second_fc
    label_count = model_settings['label_count']
    final_fc_weights = tf.Variable(
        tf.truncated_normal(
            [second_fc_output_channels, label_count], stddev=0.01))
    final_fc_bias = tf.Variable(tf.zeros([label_count]))
    final_fc = tf.matmul(final_fc_input, final_fc_weights) + final_fc_bias
    if is_training:
        return final_fc, dropout_prob
    else:
        return final_fc


def create_alexnet_v01_model(fingerprint_input, model_settings, is_training):
    print('using alexnet v01')

    """
    Here's the layout of the graph:

    (fingerprint_input)
    v
    [Conv2D] < -(weights)
    v
    [BiasAdd] < -(bias)
    v
    [Relu]
    v
    [Normalize] <-TODO
    v
    [MaxPool]
    v
    [Conv2D] < -(weights)
    v
    [BiasAdd] < -(bias)
    v
    [Relu]
    v
    [Normalize] <-TODO
    v
    [MaxPool]
    v
    [Conv2D] < -(weights)
    v
    [BiasAdd] < -(bias)
    v
    [Relu]
    v
    [Conv2D] < -(weights)
    v
    [BiasAdd] < -(bias)
    v
    [Relu]
    v
    [Conv2D] < -(weights)
    v
    [BiasAdd] < -(bias)
    v
    [Relu]
    v
    [MaxPool]
    v
    [MatMul]
    v
    [Relu]
    v
    [MatMul]
    v
    [Relu]
    v
    [MatMul]
    v
    [Softmax]

    Args:
    fingerprint_input: TensorFlow node that will output audio feature vectors.
    model_settings: Dictionary of information about the model.
    is_training: Whether the model is going to be used for training.

    Returns:
    TensorFlow node outputting logits results, and optionally  a dropout placeholder.
    """
    input_frequency_size = model_settings['dct_coefficient_count']
    print('input_frequency_size', input_frequency_size)
    input_time_size = model_settings['spectrogram_length']
    print('input_time_size', input_time_size)
    print("input", fingerprint_input)
    fingerprint_4d = tf.reshape(fingerprint_input,
                                [-1, input_time_size, input_frequency_size, 1])
    print("finger", fingerprint_4d)
    print("finger shape", fingerprint_4d.get_shape())
    fingerprint_size = model_settings['fingerprint_size']
    print("fingersize", fingerprint_size)

    # AlexNet architecture
    # Conv1 - 96 kernels of size 11×11×3 with a stride of 4 pixels
    # Conv2 - 256 kernels of size 5 × 5 × 48
    # Conv3 - 384 kernels of size 3 × 3 × 256
    # Conv4 - 384 kernels of size 3 × 3 × 192
    # Conv5 - 256 kernels of size 3 × 3 × 192
    # FC1 and 2 - 4096 neurons
    # last layer - 1000 neurons

    # dropout 0.5

    num_labels = model_settings['label_count']

    #      W[x,y,input,output]                         no idea for the shape, used the same as above
    weights = {'W_conv1': tf.Variable(tf.truncated_normal([20, 8, 1, 64], stddev=0.01)),
               'W_conv2': tf.Variable(tf.truncated_normal([10, 4, 64, 128], stddev=0.01)),
               'W_conv3': tf.Variable(tf.truncated_normal([10, 4, 128, 128], stddev=0.01)),
               'W_conv4': tf.Variable(tf.truncated_normal([10, 4, 128, 128], stddev=0.01)),
               'W_conv5': tf.Variable(tf.truncated_normal([10, 4, 128, 64], stddev=0.01)),
               'W_fc1': tf.Variable(tf.truncated_normal([25 * 5 * 64, 200], stddev=0.01)),
               'W_fc2': tf.Variable(tf.truncated_normal([200, 200], stddev=0.01)),
               'W_fc3': tf.Variable(tf.truncated_normal([200, num_labels], stddev=0.01))}

    #                                use tf.zeros or random
    biases = {'b_conv1': tf.Variable(tf.constant(0.0, shape=[64])),
              'b_conv2': tf.Variable(tf.constant(1.0, shape=[64])),
              'b_conv3': tf.Variable(tf.constant(0.0, shape=[128])),
              'b_conv4': tf.Variable(tf.constant(1.0, shape=[128])),
              'b_conv5': tf.Variable(tf.constant(1.0, shape=[64])),
              'b_fc1': tf.Variable(tf.constant(1.0, shape=[200])),
              'b_fc2': tf.Variable(tf.constant(1.0, shape=[200])),
              'b_fc3': tf.Variable(tf.constant(1.0, shape=[num_labels]))}

    # is there anyreshaping needed?

    if is_training:
        dropout_prob = tf.placeholder(tf.float32, name='dropout_prob')
        # dropout_prob = tf.placeholder(tf.float32, name='dropout_prob')
        print("Dropout rate", dropout_prob)
    # conv(input, weights, stride, padding)
    print('input shape', fingerprint_4d.get_shape())

    # Convolution layer 1
    conv1 = tf.nn.conv2d(fingerprint_4d, weights['W_conv1'], strides=[1, 1, 1, 1], padding='SAME')
    print('conv1 shape', conv1.get_shape())
    conv1 = tf.nn.bias_add(conv1, biases['b_conv1'])
    conv1 = tf.nn.relu(conv1)
    if is_training:
        conv1 = tf.nn.dropout(conv1, dropout_prob)
    conv1 = tf.nn.local_response_normalization(conv1, depth_radius=5.0, bias=2.0, alpha=1e-4, beta=0.75)
    conv1_pool = tf.nn.max_pool(conv1, ksize=[1, 3, 3, 1], strides=[1, 2, 2, 1],
                                padding='SAME')  ##Should padding ='SAME'?
    print('conv1_pool shape', conv1_pool.get_shape())

    # Convolution layer 2
    conv2 = tf.nn.conv2d(conv1_pool, weights['W_conv2'], strides=[1, 1, 1, 1], padding='SAME')
    conv2 = tf.nn.bias_add(conv2, biases['b_conv2'])
    conv2 = tf.nn.relu(conv2)
    print('conv2 shape', conv2.get_shape())
    if is_training:
        conv2 = tf.nn.dropout(conv2, dropout_prob)
    conv2 = tf.nn.local_response_normalization(conv2, depth_radius=5.0, bias=2.0, alpha=1e-4, beta=0.75)
    conv2_pool = tf.nn.max_pool(conv2, ksize=[1, 3, 3, 1], strides=[1, 2, 2, 1],
                                padding='SAME')  ##Should padding ='SAME'?
    print('conv2_pool shape', conv2_pool.get_shape())

    # Convolution layer 3
    conv3 = tf.nn.conv2d(conv2_pool, weights['W_conv3'], strides=[1, 1, 1, 1], padding='SAME')
    conv3 = tf.nn.bias_add(conv3, biases['b_conv3'])
    conv3 = tf.nn.relu(conv3)
    if is_training:
        conv3 = tf.nn.dropout(conv3, keep_prob=dropout_prob)
    print('conv3 shape', conv3.get_shape())

    # Convolution layer 4
    conv4 = tf.nn.conv2d(conv3, weights['W_conv4'], strides=[1, 1, 1, 1], padding='SAME')
    conv4 = tf.nn.bias_add(conv4, biases['b_conv4'])
    conv4 = tf.nn.relu(conv4)
    if is_training:
        conv4 = tf.nn.dropout(conv4, keep_prob=dropout_prob)
    print('conv4 shape', conv4.get_shape())

    # Convolution layer 5
    conv5 = tf.nn.conv2d(conv4, weights['W_conv5'], strides=[1, 1, 1, 1], padding='SAME')
    conv5 = tf.nn.bias_add(conv5, biases['b_conv5'])
    conv5 = tf.nn.relu(conv5)
    if is_training:
        conv5 = tf.nn.dropout(conv5, keep_prob=dropout_prob)
    conv5_pool = tf.nn.max_pool(conv5, ksize=[1, 3, 3, 1], strides=[1, 2, 2, 1],
                                padding='SAME')  ##Should padding ='SAME'?
    print('conv5_pool shape', conv5_pool.get_shape())

    # Flatten conv5 into
    num_neurons = weights['W_fc1'].get_shape().as_list()[0]
    print(num_neurons)
    # reshape for fully connected layer
    #                                  WHAT SIZE? <- Same as above
    # shape_conv5_pool = conv5_pool.get_shape()
    # height*width*output size
    # would weights['W_conv5'][3] work <---- ?
    # num_elem = int(shape_conv5_pool[1]*shape_conv5_pool[2]*conv5_output_channels)
    # weights['W_fc1']= tf.Variable(tf.truncated_normal([num_elem, 200]))
    # print("weights['W_fc1']", weights['W_fc1'])
    # print('shape_conv5_pool[1]',shape_conv5_pool[1])
    # print('shape_conv5_pool[2]',shape_conv5_pool[2])
    # print("num_elem", num_elem)
    flatten = tf.reshape(conv5_pool, [-1, num_neurons])

    # fully-connected layer 1
    fc1 = tf.nn.bias_add(tf.matmul(flatten, weights['W_fc1']), biases['b_fc1'])
    fc1 = tf.nn.relu(fc1)
    print('fc1 shape', fc1.get_shape())
    if is_training:
        fc1 = tf.nn.dropout(fc1, keep_prob=dropout_prob)

    # fully-connected layer 2
    fc2 = tf.nn.bias_add(tf.matmul(fc1, weights['W_fc2']), biases['b_fc2'])
    fc2 = tf.nn.relu(fc2)
    print('fc2 shape', fc2.get_shape())
    if is_training:
        fc2 = tf.nn.dropout(fc2, keep_prob=dropout_prob)

    # fully-connected layer 3
    fc3 = tf.nn.bias_add(tf.matmul(fc2, weights['W_fc3']), biases['b_fc3'])
    # fc3 = tf.nn.softmax(fc3)
    print("softmax was removed")
    print('fc3 shape', fc3.get_shape)

    logits = fc3
    print('logits', logits)
    if is_training:
        return logits, dropout_prob
    else:
        return logits


""" ONLY CHANGE THIS FUNCTION THE OTHER ONE IS THE REAL ALEXNET """


def create_alexnet_adapt_model(fingerprint_input, model_settings, is_training):
    print('using alexnet adaptation')

    """
    Args:
    fingerprint_input: TensorFlow node that will output audio feature vectors.
    model_settings: Dictionary of information about the model.
    is_training: Whether the model is going to be used for training.

    Returns:
    TensorFlow node outputting logits results, and optionally  a dropout placeholder.
    """
    if is_training:
        dropout_prob = tf.placeholder(tf.float32, name='dropout_prob')
    input_frequency_size = model_settings['dct_coefficient_count']
    input_time_size = model_settings['spectrogram_length']
    fingerprint_4d = tf.reshape(fingerprint_input,
                                [-1, input_time_size, input_frequency_size, 1])

    """Conv layer 1"""
    first_filter_width = 8
    first_filter_height = 20
    first_filter_count = 64
    first_weights = tf.Variable(
        tf.truncated_normal(
            [first_filter_height, first_filter_width, 1, first_filter_count],
            stddev=0.01))
    first_bias = tf.Variable(tf.zeros([first_filter_count]))
    first_conv = tf.nn.conv2d(fingerprint_4d, first_weights, [1, 4, 4, 1],
                              'SAME') + first_bias
    print("first conv shape", first_conv.get_shape())
    first_relu = tf.nn.relu(first_conv)
    if is_training:
        first_dropout = tf.nn.dropout(first_relu, dropout_prob)
    else:
        first_dropout = first_relu
    first_normal = tf.nn.local_response_normalization(first_dropout, depth_radius=5.0, bias=2.0, alpha=1e-4, beta=0.75)
    first_max_pool = tf.nn.max_pool(first_normal, [1, 3, 3, 1], [1, 2, 2, 1], 'SAME')
    print("first pool conv shape", first_max_pool.get_shape())

    """Conv layer 2"""
    second_filter_width = 4
    second_filter_height = 10
    second_filter_count = 128
    second_weights = tf.Variable(
        tf.truncated_normal(
            [
                second_filter_height, second_filter_width, first_filter_count,
                second_filter_count
            ],
            stddev=0.01))
    second_bias = tf.Variable(tf.zeros([second_filter_count]))
    second_conv = tf.nn.conv2d(first_max_pool, second_weights, [1, 1, 1, 1],
                               'SAME') + second_bias
    print("2nd conv shape", second_conv.get_shape())
    second_relu = tf.nn.relu(second_conv)
    if is_training:
        second_dropout = tf.nn.dropout(second_relu, dropout_prob)
    else:
        second_dropout = second_relu
    second_normal = tf.nn.local_response_normalization(second_dropout, depth_radius=5.0, bias=2.0, alpha=1e-4,
                                                       beta=0.75)
    second_max_pool = tf.nn.max_pool(second_normal, [1, 3, 3, 1], [1, 2, 2, 1], 'SAME')
    print("second pool conv shape", second_max_pool.get_shape())

    """Conv layer 3"""
    third_filter_width = 4
    third_filter_height = 10
    third_filter_count = 128
    third_weights = tf.Variable(
        tf.truncated_normal(
            [
                third_filter_height, third_filter_width, second_filter_count,
                third_filter_count
            ],
            stddev=0.01))
    third_bias = tf.Variable(tf.zeros([third_filter_count]))
    third_conv = tf.nn.conv2d(second_max_pool, third_weights, [1, 1, 1, 1],
                              'SAME') + third_bias
    print("3rd conv shape", third_conv.get_shape())
    third_relu = tf.nn.relu(third_conv)
    if is_training:
        third_dropout = tf.nn.dropout(third_relu, dropout_prob)
    else:
        third_dropout = third_relu

    """Conv layer 4"""
    fourth_filter_width = 4
    fourth_filter_height = 10
    fourth_filter_count = 128
    fourth_weights = tf.Variable(
        tf.truncated_normal(
            [
                fourth_filter_height, fourth_filter_width, third_filter_count,
                fourth_filter_count
            ],
            stddev=0.01))
    fourth_bias = tf.Variable(tf.zeros([fourth_filter_count]))
    fourth_conv = tf.nn.conv2d(third_dropout, fourth_weights, [1, 1, 1, 1],
                               'SAME') + fourth_bias
    print("4th conv shape", fourth_conv.get_shape())
    fourth_relu = tf.nn.relu(fourth_conv)
    if is_training:
        fourth_dropout = tf.nn.dropout(fourth_relu, dropout_prob)
    else:
        fourth_dropout = fourth_relu

    """Conv layer 5"""
    fifth_filter_width = 4
    fifth_filter_height = 10
    fifth_filter_count = 128
    fifth_weights = tf.Variable(
        tf.truncated_normal(
            [
                fifth_filter_height, fifth_filter_width, fourth_filter_count,
                fifth_filter_count
            ],
            stddev=0.01))
    fifth_bias = tf.Variable(tf.zeros([fifth_filter_count]))
    fifth_conv = tf.nn.conv2d(fourth_dropout, fifth_weights, [1, 1, 1, 1],
                              'SAME') + fifth_bias
    print("5th conv shape", fifth_conv.get_shape())
    fifth_relu = tf.nn.relu(fifth_conv)
    if is_training:
        fifth_dropout = tf.nn.dropout(fifth_relu, dropout_prob)
    else:
        fifth_dropout = fifth_relu
    fifth_max_pool = tf.nn.max_pool(fifth_dropout, [1, 3, 3, 1], [1, 2, 2, 1], 'SAME')
    print("fifth pool conv shape", fifth_max_pool.get_shape())

    """Flatten conv layers"""
    fifth_conv_shape = fifth_max_pool.get_shape()
    fifth_conv_output_width = fifth_conv_shape[2]
    fifth_conv_output_height = fifth_conv_shape[1]
    fifth_conv_element_count = int(
        fifth_conv_output_width * fifth_conv_output_height *
        fifth_filter_count)
    flattened_fifth_conv = tf.reshape(fifth_max_pool,
                                      [-1, fifth_conv_element_count])
    print("flattened size", fifth_conv_element_count)

    """First fully connected layer"""
    first_fc_output_channels = 512
    first_fc_weights = tf.Variable(
        tf.truncated_normal(
            [fifth_conv_element_count, first_fc_output_channels], stddev=0.01))
    first_fc_bias = tf.Variable(tf.zeros([first_fc_output_channels]))
    first_fc = tf.matmul(flattened_fifth_conv, first_fc_weights) + first_fc_bias
    print("first fc", first_fc.get_shape())
    if is_training:
        first_fc_drop = tf.nn.dropout(first_fc, dropout_prob)
    else:
        first_fc_drop = first_fc

    """Second fully connected layer"""
    second_fc_output_channels = 512
    second_fc_weights = tf.Variable(
        tf.truncated_normal(
            [first_fc_output_channels, second_fc_output_channels], stddev=0.01))
    second_fc_bias = tf.Variable(tf.zeros([second_fc_output_channels]))
    second_fc = tf.matmul(first_fc_drop, second_fc_weights) + second_fc_bias
    print("second fc", second_fc.get_shape())
    if is_training:
        second_fc_drop = tf.nn.dropout(second_fc, dropout_prob)
    else:
        second_fc_drop = second_fc

    """Final fully connected layer"""
    label_count = model_settings['label_count']
    final_fc_weights = tf.Variable(
        tf.truncated_normal(
            [second_fc_output_channels, label_count], stddev=0.01))
    final_fc_bias = tf.Variable(tf.zeros([label_count]))
    final_fc = tf.matmul(second_fc_drop, final_fc_weights) + final_fc_bias
    print("final fc", final_fc.get_shape())
    if is_training:
        return final_fc, dropout_prob
    else:
        return final_fc

def create_deepear_v01_model(fingerprint_input, model_settings, is_training, size_multiplier):
    """

    TODO complete description

    Flags: Namespace(background_frequency=0.8, background_volume=0.1, batch_size=100, check_nans=False,
    clip_duration_ms=1000, data_dir='/tmp/speech_dataset/',
    data_url='http://download.tensorflow.org/data/speech_commands_v0.01.tar.gz',
    dct_coefficient_count=40, eval_step_interval=400, how_many_training_steps='15,3', learning_rate='0.001,0.0001',
    model_architecture='deepear_v01', sample_rate=16000, save_step_interval=100, silence_percentage=10.0,
    start_checkpoint='', summaries_dir='/tmp/retrain_logs', testing_percentage=10, time_shift_ms=100.0,
    train_dir='/tmp/speech_commands_train', unknown_percentage=10.0, validation_percentage=10,
    wanted_words='yes,no,up,down,left,right,on,off,stop,go', window_size_ms=30.0, window_stride_ms=10.0)

    Final test accuracy          Model                          Training Steps     dataset
    ~68.1%                       1 FC Hidden Layer of 1024 Nodes   15000,3000      wanted_words='yes,no,up,down,left,right,on,off,stop,go'
    ~22.1%                       2 FC Hidden Layers of 1024 Nodes  15000,3000      wanted_words='yes,no,up,down,left,right,on,off,stop,go'
     ~8%                         3 FC Hidden Layers of 1024 Nodes  15000,3000      wanted_words='yes,no,up,down,left,right,on,off,stop,go'

    Here's the layout of the graph:

    (fingerprint_input)
            v
        [MatMul]<-(weights)
            v
        [BiasAdd]<-(bias)
            v
            TODO
    Args:
      fingerprint_input: TensorFlow node that will output audio feature vectors.
      model_settings: Dictionary of information about the model.
      is_training: Whether the model is going to be used for training.

    Returns:
      TensorFlow node outputting logits results, and optionally a dropout
      placeholder.
    """
    if is_training:
        dropout_prob = tf.placeholder(tf.float32, name='dropout_prob')
    fingerprint_size = model_settings['fingerprint_size']
    label_count = model_settings['label_count']
    previous_layer_values = fingerprint_input
    tf.logging.info('Number of input values to network (fingerprint_input) %s', str(fingerprint_input.shape))
    previous_layer_size = fingerprint_size
    hidden_units_size = 1024

    nodes_in_layer = [hidden_units_size * size_multiplier, hidden_units_size * size_multiplier, hidden_units_size * size_multiplier]
    layer_number = 0
    hidden0 = tf.nn.relu(
        tf.matmul(
            previous_layer_values,
            tf.Variable(
                tf.truncated_normal(
                    [previous_layer_size, nodes_in_layer[layer_number]],
                    stddev=0.001),
                name='weights' + str(layer_number))
        ) + tf.Variable(tf.zeros([nodes_in_layer[layer_number]]), name='biases' + str(layer_number)))
    if is_training:
        layer0_values = tf.nn.dropout(hidden0, dropout_prob)
    else:
        layer0_values = hidden0
    previous_layer_size = nodes_in_layer[layer_number]

    layer_number = 1
    hidden1 = tf.nn.relu(tf.matmul(layer0_values, tf.Variable(
        tf.truncated_normal([previous_layer_size, nodes_in_layer[layer_number]], stddev=0.001),
        name='weights' + str(layer_number))) + tf.Variable(tf.zeros([nodes_in_layer[layer_number]]),
                                                           name='biases' + str(layer_number)))
    if is_training:
        layer1_values = tf.nn.dropout(hidden1, dropout_prob)
    else:
        layer1_values = hidden1
    previous_layer_size = nodes_in_layer[layer_number]

    layer_number = 2
    hidden2 = tf.nn.relu(tf.matmul(layer1_values, tf.Variable(
        tf.truncated_normal([previous_layer_size, nodes_in_layer[layer_number]], stddev=0.001),
        name='weights' + str(layer_number))) + tf.Variable(tf.zeros([nodes_in_layer[layer_number]]),
                                                           name='biases' + str(layer_number)))
    if is_training:
        layer2_values = tf.nn.dropout(hidden2, dropout_prob)
    else:
        layer2_values = hidden2
    previous_layer_size = nodes_in_layer[layer_number]

    weightsN = tf.Variable(tf.truncated_normal([previous_layer_size, label_count], stddev=0.001), name='weightsN')
    logits = tf.matmul(layer2_values, weightsN) + tf.Variable(tf.zeros([label_count]))

    if is_training:
        return logits, dropout_prob
    else:
        return logits
