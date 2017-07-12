# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not
# use this file except in compliance with the License. A copy of the License
# is located at
#
#     http://aws.amazon.com/apache2.0/
# 
# or in the "license" file accompanying this file. This file is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

"""
Defines Encoder interface and various implementations.
"""
import logging
from typing import Callable, List

import mxnet as mx

import sockeye.rnn
import sockeye.utils
from sockeye.initializer import PositionalEncodingInitializer
from sockeye.layers import FFNRelu, LayerNormalization, MultiHeadAttention
from sockeye.utils import check_condition
from . import constants as C

logger = logging.getLogger(__name__)


def get_encoder_rnn(num_embed: int,
                    vocab_size: int,
                    num_layers: int,
                    rnn_num_hidden: int,
                    cell_type: str,
                    residual: bool,
                    dropout: float,
                    forget_bias: float,
                    fused: bool = False) -> 'Encoder':
    """
    Returns an encoder with embedding, batch2time-major conversion, and bidirectional RNN encoder.
    If num_layers > 1, adds uni-directional RNNs.

    :param num_embed: Size of embedding layer.
    :param vocab_size: Source vocabulary size.
    :param num_layers: Number of encoder layers.
    :param rnn_num_hidden: Number of hidden units for RNN cells.
    :param cell_type: RNN cell type.
    :param residual: Whether to add residual connections to multi-layered RNNs.
    :param dropout: Dropout probability for encoders (RNN and embedding).
    :param forget_bias: Initial value of RNN forget biases.
    :param fused: Whether to use FusedRNNCell (CuDNN). Only works with GPU context.
    :return: Encoder instance.
    """
    # TODO give more control on encoder architecture
    encoders = list()
    encoders.append(Embedding(num_embed=num_embed,
                              vocab_size=vocab_size,
                              prefix=C.SOURCE_EMBEDDING_PREFIX,
                              dropout=dropout))
    encoders.append(BatchMajor2TimeMajor())

    encoder_class = FusedRecurrentEncoder if fused else RecurrentEncoder
    encoders.append(BiDirectionalRNNEncoder(num_hidden=rnn_num_hidden,
                                            num_layers=1,
                                            dropout=dropout,
                                            layout=C.TIME_MAJOR,
                                            cell_type=cell_type,
                                            encoder_class=encoder_class,
                                            forget_bias=forget_bias))

    if num_layers > 1:
        encoders.append(encoder_class(num_hidden=rnn_num_hidden,
                                      num_layers=num_layers - 1,
                                      dropout=dropout,
                                      layout=C.TIME_MAJOR,
                                      cell_type=cell_type,
                                      residual=residual,
                                      forget_bias=forget_bias))

    return EncoderSequence(encoders)


def get_encoder_transformer(model_size: int,
                            vocab_size: int,
                            num_layers: int,
                            attention_heads: int,
                            feed_forward_num_hidden: int,
                            dropout: float) -> 'Encoder':
    """
    Returns a Transformer encoder.

    :param model_size: Size of all layers and embeddings (dimension of model).
    :param vocab_size: Source vocabulary size.
    :param num_layers: Number of encoder layers.
    :param attention_heads: Number of attention heads.
    :param feed_forward_num_hidden: number of hidden units in FFN layers.
    :param dropout: Dropout probability for encoders.
    :return: Encoder instance.
    """
    encoders = list()
    encoders.append(Embedding(num_embed=model_size,
                              vocab_size=vocab_size,
                              prefix=C.SOURCE_EMBEDDING_PREFIX,
                              dropout=dropout,
                              add_positional_encoding=True))

    encoders.append(TransformerEncoder(model_size=model_size,
                                       num_layers=num_layers,
                                       attention_heads=attention_heads,
                                       feed_forward_num_hidden=feed_forward_num_hidden,
                                       dropout=dropout,
                                       prefix=C.TRANSFORMER_ENCODER_PREFIX))

    encoders.append(BatchMajor2TimeMajor(num_hidden=model_size))

    return EncoderSequence(encoders)


class Encoder:
    """
    Generic encoder interface.
    """

    def encode(self, data: mx.sym.Symbol, data_length: mx.sym.Symbol, seq_len: int) -> mx.sym.Symbol:
        """
        Encodes data given sequence lengths of individual examples and maximum sequence length.
        
        :param data: Input data.
        :param data_length: Vector with sequence lengths.
        :param seq_len: Maximum sequence length.
        :return: Encoded input data.
        """
        raise NotImplementedError()

    def get_num_hidden(self) -> int:
        """
        Return the representation size of this encoder.
        """
        raise NotImplementedError()

    def get_rnn_cells(self) -> List[mx.rnn.BaseRNNCell]:
        """
        Returns a list of RNNCells used by this encoder.
        """
        raise NotImplementedError()


class BatchMajor2TimeMajor(Encoder):
    """
    Converts batch major data to time major
    """
    def __init__(self, num_hidden: int = 0):
        # TODO(fhieber): lets allow BatchMajor2TimeMajor to pass on number of hidden units if used as last encoder.
        # we need a better strategy for this though.
        self.num_hidden = num_hidden

    def encode(self, data: mx.sym.Symbol, data_length: mx.sym.Symbol, seq_len: int) -> mx.sym.Symbol:
        """
        Encodes data given sequence lengths of individual examples (data_length) and maximum sequence length (seq_len).

        :param data: Input data.
        :param data_length: Vector with sequence lengths.
        :param seq_len: Maximum sequence length.
        :return: Encoded input data.
        """
        with mx.AttrScope(__layout__=C.TIME_MAJOR):
            return mx.sym.swapaxes(data=data, dim1=0, dim2=1)

    def get_num_hidden(self) -> int:
        """
        Return the representation size of this encoder.
        """
        return self.num_hidden

    def get_rnn_cells(self) -> List[mx.rnn.BaseRNNCell]:
        """
        Returns a list of RNNCells used by this encoder.
        """
        return []


class Embedding(Encoder):
    """
    Thin wrapper around MXNet's Embedding symbol. Works with both time- and batch-major data layouts.

    :param num_embed: Embedding size.
    :param vocab_size: Source vocabulary size.
    :param prefix: Name prefix for symbols of this encoder.
    :param dropout: Dropout probability.
    """

    def __init__(self,
                 num_embed: int,
                 vocab_size: int,
                 prefix: str,
                 dropout: float,
                 add_positional_encoding: bool = False):
        self.num_embed = num_embed
        self.vocab_size = vocab_size
        self.prefix = prefix
        self.dropout = dropout
        self.embed_weight = mx.sym.Variable(prefix + "weight")
        self.add_positional_encoding = add_positional_encoding

    def encode(self, data: mx.sym.Symbol, data_length: mx.sym.Symbol, seq_len: int) -> mx.sym.Symbol:
        """
        Encodes data given sequence lengths of individual examples and maximum sequence length.

        :param data: Input data.
        :param data_length: Vector with sequence lengths.
        :param seq_len: Maximum sequence length.
        :return: Encoded input data.
        """
        embedding = mx.sym.Embedding(data=data,
                                     input_dim=self.vocab_size,
                                     weight=self.embed_weight,
                                     output_dim=self.num_embed,
                                     name=self.prefix + "embed")
        if self.add_positional_encoding:
            embedding = mx.sym.broadcast_add(embedding,
                                             self._get_positional_encoding(seq_len),
                                             name='%sadd_encoding' % self.prefix)
        if self.dropout > 0:
            embedding = mx.sym.Dropout(data=embedding, p=self.dropout, name="source_embed_dropout")
        return embedding

    def _get_positional_encoding(self, seq_len: int) -> mx.sym.Symbol:
        """
        Returns a variable initialized with positional encodings as in Vaswani et al.

        :param seq_len: Maximum sequence length
        :return: Symbol(1, max_seq_len, self.num_embed)
        """
        #FIXME: we cant have variable-sized variables if they are not batch-major.... :/
        # Either we go back to original version (including cosines) or we find a better way with this initialization.
        max_seq_len = 500
        encodings = mx.sym.Variable("%spositional_encodings" % self.prefix,
                                    shape=(max_seq_len, self.num_embed),
                                    init=PositionalEncodingInitializer(max_seq_len, self.num_embed))
        encodings = mx.sym.slice_axis(encodings, axis=0, begin=0, end=seq_len)
        encodings = mx.sym.BlockGrad(mx.sym.expand_dims(encodings, axis=0))
        return encodings

    def get_num_hidden(self) -> int:
        """
        Return the representation size of this encoder.
        """
        return self.num_embed

    def get_rnn_cells(self) -> List[mx.rnn.BaseRNNCell]:
        """
        Returns a list of RNNCells used by this encoder.
        """
        return []


class EncoderSequence(Encoder):
    """
    A sequence of encoders is itself an encoder.

    :param encoders: List of encoders.
    """

    def __init__(self, encoders: List[Encoder]):
        self.encoders = encoders

    def encode(self, data: mx.sym.Symbol, data_length: mx.sym.Symbol, seq_len: int) -> mx.sym.Symbol:
        """
        Encodes data given sequence lengths of individual examples and maximum sequence length.

        :param data: Input data.
        :param data_length: Vector with sequence lengths.
        :param seq_len: Maximum sequence length.
        :return: Encoded input data.
        """
        for encoder in self.encoders:
            data = encoder.encode(data, data_length, seq_len)
        return data

    def get_num_hidden(self) -> int:
        """
        Return the representation size of this encoder.
        """
        return self.encoders[-1].get_num_hidden()

    def get_rnn_cells(self) -> List[mx.rnn.BaseRNNCell]:
        """
        Returns a list of RNNCells used by this encoder.
        """
        cells = []
        for encoder in self.encoders:
            for cell in encoder.get_rnn_cells():
                cells.append(cell)
        return cells


class RecurrentEncoder(Encoder):
    """
    Uni-directional (multi-layered) recurrent encoder
    """

    def __init__(self,
                 num_hidden: int,
                 num_layers: int,
                 prefix: str = C.STACKEDRNN_PREFIX,
                 dropout: float = 0.,
                 layout: str = C.TIME_MAJOR,
                 cell_type: str = C.LSTM_TYPE,
                 residual: bool = False,
                 forget_bias=0.0):
        self.layout = layout
        self.num_hidden = num_hidden
        self.rnn = sockeye.rnn.get_stacked_rnn(cell_type, num_hidden,
                                               num_layers, dropout, prefix,
                                               residual, forget_bias)

    def encode(self, data: mx.sym.Symbol, data_length: mx.sym.Symbol, seq_len: int) -> mx.sym.Symbol:
        """
        Encodes data given sequence lengths of individual examples and maximum sequence length.

        :param data: Input data.
        :param data_length: Vector with sequence lengths.
        :param seq_len: Maximum sequence length.
        :return: Encoded input data.
        """
        outputs, _ = self.rnn.unroll(seq_len, inputs=data, merge_outputs=True, layout=self.layout)

        return outputs

    def get_rnn_cells(self):
        """
        Returns RNNCells used in this encoder.
        """
        return [self.rnn]

    def get_num_hidden(self):
        """
        Return the representation size of this encoder.
        """
        return self.num_hidden


class FusedRecurrentEncoder(Encoder):
    """
    Uni-directional (multi-layered) recurrent encoder
    """

    def __init__(self,
                 num_hidden: int,
                 num_layers: int,
                 prefix: str = C.STACKEDRNN_PREFIX,
                 dropout: float = 0.,
                 layout: str = C.TIME_MAJOR,
                 cell_type: str = C.LSTM_TYPE,
                 residual: bool = False,
                 forget_bias=0.0):
        self.layout = layout
        self.num_hidden = num_hidden
        logger.warning("%s: FusedRNNCell uses standard MXNet Orthogonal initializer w/ rand_type=uniform", prefix)
        self.rnn = [mx.rnn.FusedRNNCell(num_hidden,
                                        num_layers=num_layers,
                                        mode=cell_type,
                                        bidirectional=False,
                                        dropout=dropout,
                                        forget_bias=forget_bias,
                                        prefix=prefix)]

    def encode(self, data: mx.sym.Symbol, data_length: mx.sym.Symbol, seq_len: int) -> mx.sym.Symbol:
        """
        Encodes data given sequence lengths of individual examples and maximum sequence length.

        :param data: Input data.
        :param data_length: Vector with sequence lengths.
        :param seq_len: Maximum sequence length.
        :return: Encoded input data.
        """
        outputs = data
        for cell in self.rnn:
            outputs, _ = cell.unroll(seq_len, inputs=outputs, merge_outputs=True, layout=self.layout)

        return outputs

    def get_rnn_cells(self):
        """
        Returns RNNCells used in this encoder.
        """
        return self.rnn

    def get_num_hidden(self):
        """
        Return the representation size of this encoder.
        """
        return self.num_hidden


class BiDirectionalRNNEncoder(Encoder):
    """
    An encoder that runs a forward and a reverse RNN over input data.
    States from both RNNs are concatenated together.

    :param num_hidden: Number of hidden units for final, concatenated encoder states. Must be a multiple of 2.
    :param num_layers: Number of RNN layers.
    :param prefix: Name prefix for symbols of this encoder.
    :param dropout: Dropout probability.
    :param layout: Input data layout. Default: time-major.
    :param cell_type: RNN cell type.
    :param fused: Whether to use FusedRNNCell (CuDNN). Only works with GPU context.
    :param forget_bias: Initial value of RNN forget biases.
    """

    def __init__(self,
                 num_hidden: int,
                 num_layers: int,
                 prefix=C.BIDIRECTIONALRNN_PREFIX,
                 dropout: float = 0.,
                 layout=C.TIME_MAJOR,
                 cell_type=C.LSTM_TYPE,
                 encoder_class: Callable = RecurrentEncoder,
                 forget_bias: float = 0.0):
        check_condition(num_hidden % 2 == 0, "num_hidden must be a multiple of 2 for BiDirectionalRNNEncoders.")
        self.num_hidden = num_hidden
        if layout[0] == 'N':
            logger.warning("Batch-major layout for encoder input. Consider using time-major layout for faster speed")

        # time-major layout as _encode needs to swap layout for SequenceReverse
        self.forward_rnn = encoder_class(num_hidden=num_hidden // 2, num_layers=num_layers,
                                         prefix=prefix + C.FORWARD_PREFIX, dropout=dropout,
                                         layout=C.TIME_MAJOR, cell_type=cell_type,
                                         forget_bias=forget_bias)
        self.reverse_rnn = encoder_class(num_hidden=num_hidden // 2, num_layers=num_layers,
                                         prefix=prefix + C.REVERSE_PREFIX, dropout=dropout,
                                         layout=C.TIME_MAJOR, cell_type=cell_type,
                                         forget_bias=forget_bias)
        self.layout = layout
        self.prefix = prefix

    def encode(self, data: mx.sym.Symbol, data_length: mx.sym.Symbol, seq_len: int) -> mx.sym.Symbol:
        """
        Encodes data given sequence lengths of individual examples and maximum sequence length.

        :param data: Input data.
        :param data_length: Vector with sequence lengths.
        :param seq_len: Maximum sequence length.
        :return: Encoded input data.
        """
        if self.layout[0] == 'N':
            data = mx.sym.swapaxes(data=data, dim1=0, dim2=1)
        data = self._encode(data, data_length, seq_len)
        if self.layout[0] == 'N':
            data = mx.sym.swapaxes(data=data, dim1=0, dim2=1)
        return data

    def _encode(self, data: mx.sym.Symbol, data_length: mx.sym.Symbol, seq_len: int) -> mx.sym.Symbol:
        """
        Bidirectionally encodes time-major data.
        """
        # (seq_len, batch_size, num_embed)
        data_reverse = mx.sym.SequenceReverse(data=data, sequence_length=data_length,
                                              use_sequence_length=True)
        # (seq_length, batch, cell_num_hidden)
        hidden_forward = self.forward_rnn.encode(data, data_length, seq_len)
        # (seq_length, batch, cell_num_hidden)
        hidden_reverse = self.reverse_rnn.encode(data_reverse, data_length, seq_len)
        # (seq_length, batch, cell_num_hidden)
        hidden_reverse = mx.sym.SequenceReverse(data=hidden_reverse, sequence_length=data_length,
                                                use_sequence_length=True)
        # (seq_length, batch, 2 * cell_num_hidden)
        hidden_concat = mx.sym.concat(hidden_forward, hidden_reverse, dim=2, name="%s_rnn" % self.prefix)

        return hidden_concat

    def get_num_hidden(self) -> int:
        """
        Return the representation size of this encoder.
        """
        return self.num_hidden

    def get_rnn_cells(self) -> List[mx.rnn.BaseRNNCell]:
        """
        Returns a list of RNNCells used by this encoder.
        """
        return self.forward_rnn.get_rnn_cells() + self.reverse_rnn.get_rnn_cells()


class TransformerEncoder(Encoder):
    """
    Non-recurrent encoder based on the transformer architecture in:

    Attention Is All You Need, Figure 1 (left)
    Vaswani et al. (https://arxiv.org/pdf/1706.03762.pdf)

    :param model_size: Size of all layers and embeddings (dimension of model).
    :param num_layers: Number of encoder layers.
    :param attention_heads: Number of attention heads.
    :param feed_forward_num_hidden: number of hidden units in FFN layers.
    :param dropout: Dropout probability for encoders (RNN and embedding).
    :param prefix: Name prefix for symbols of this encoder.
    """

    def __init__(self,
                 model_size: int = 512,
                 num_layers: int = 6,
                 attention_heads: int = 8,
                 feed_forward_num_hidden: int = 2048,
                 dropout: float = 0.,
                 prefix=C.TRANSFORMER_ENCODER_PREFIX):
        self.model_size = model_size
        self.num_layers = num_layers
        self.attention_heads = attention_heads
        self.feed_forward_num_hidden = feed_forward_num_hidden
        self.dropout = dropout
        self.prefix = prefix

        # TODO: make these available to the decoder.  Borrow/update get_rnn_cells?
        self.layers = list()
        for i in range(self.num_layers):
            # Self-attention sub-layer
            attention = MultiHeadAttention(depth_att=self.model_size,
                                           heads=self.attention_heads,
                                           depth_out=self.model_size,
                                           dropout=self.dropout,
                                           prefix="%sattn_%d_" % (self.prefix, i))
            # Layer normalization after attention
            attention_ln = LayerNormalization(num_hidden=self.model_size,
                                              prefix="%sattn_ln_%d_" % (self.prefix, i))
            # Feed-forward sub-layer
            feed_forward = FFNRelu(prefix="%sffn_%d_" % (self.prefix, i),
                                   num_hidden=self.feed_forward_num_hidden,
                                   num_model=self.model_size,
                                   dropout=self.dropout)
            # Layer normalization after feed-forward
            feed_forward_ln = LayerNormalization(num_hidden=self.model_size,
                                                 prefix="%sffn_ln_%d_" % (self.prefix, i))

            # Apply one layer of the encoder
            def apply(data: mx.sym.Symbol, data_length: mx.sym.Symbol, seq_len: int):
                encoded_attn = attention_ln.normalize(data + attention.on(data, data_length, seq_len))
                encoded_attn_ffn = feed_forward_ln.normalize(encoded_attn + feed_forward.apply(encoded_attn, seq_len))
                return encoded_attn_ffn

            self.layers.append(apply)

    def encode(self, data: mx.sym.Symbol, data_length: mx.sym.Symbol, seq_len: int) -> mx.sym.Symbol:
        """
        Encodes data given sequence lengths of individual examples and maximum sequence length.

        :param data: Input data (batch_size, seq_len, num_embed).
        :param data_length: Vector with sequence lengths (batch_size).
        :param seq_len: Maximum sequence length.
        :return: Encoded input data.
        """
        encoded = data
        # Encoder layers
        # All symbols (batch_size, seq_len, num_embed=model_size)
        for i, layer in enumerate(self.layers):
            encoded = layer(encoded, data_length, seq_len)
        return encoded

    def get_num_hidden(self) -> int:
        """
        Return the representation size of this encoder.
        """
        return self.model_size

    def get_rnn_cells(self) -> List[mx.rnn.BaseRNNCell]:
        """
        Returns a list of RNNCells used by this encoder.
        """
        return []
