#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Attention-based sequence-to-sequence model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

try:
    from warpctc_pytorch import CTCLoss
except:
    raise ImportError('Install warpctc_pytorch.')

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

from models.pytorch.base import ModelBase
from models.pytorch.encoders.load_encoder import load
from models.pytorch.attention.decoders.rnn_decoder import RNNDecoder
from models.pytorch.attention.attention_layer import AttentionMechanism
from utils.io.variable import var2np

NEG_INF = -float("inf")
LOG_0 = NEG_INF
LOG_1 = 0


def _logsumexp(*args):
    """Stable log sum exp."""
    if all(a == NEG_INF for a in args):
        return NEG_INF
    a_max = np.max(args)
    lsp = np.log(np.sum(np.exp(a - a_max)
                        for a in args))
    return a_max + lsp


class AttentionSeq2seq(ModelBase):
    """The Attention-besed model.
    Args:
        input_size (int): the dimension of input features
        encoder_type (string): the type of the encoder. Set lstm or gru or rnn.
        encoder_bidirectional (bool): if True, create a bidirectional encoder
        encoder_num_units (int): the number of units in each layer of the
            encoder
        encoder_num_proj (int): the number of nodes in the projection layer of
            the encoder.
        encoder_num_layers (int): the number of layers of the encoder
        encoder_dropout (float): the probability to drop nodes of the encoder
        attention_type (string): the type of attention
        attention_dim: (int) the dimension of the attention layer
        decoder_type (string): lstm or gru
        decoder_num_units (int): the number of units in each layer of the
            decoder
        decoder_num_proj (int): the number of nodes in the projection layer of
            the decoder.
        decoder_num_layers (int): the number of layers of the decoder
        decoder_dropout (float): the probability to drop nodes of the decoder
        embedding_dim (int): the dimension of the embedding in target spaces
        embedding_dropout (int): the probability to drop nodes of the
            embedding layer
        num_classes (int): the number of nodes in softmax layer
            (excluding <SOS> and <EOS> classes)
        num_stack (int, optional): the number of frames to stack
        splice (int, optional): the number of frames to splice. This is used
            when using CNN-like encoder. Default is 1 frame.
        parameter_init (float, optional): the range of uniform distribution to
            initialize weight parameters (>= 0)
        subsample_list (list, optional): subsample in the corresponding layers (True)
            ex.) [False, True, True, False] means that subsample is conducted
                in the 2nd and 3rd layers.
        init_dec_state_with_enc_state (bool, optional): if True, initialize
            decoder state with the final encoder state.
        sharpening_factor (float, optional): a sharpening factor in the
            softmax layer for computing attention weights
        logits_temperature (float, optional): a parameter for smoothing the
            softmax layer in outputing probabilities
        sigmoid_smoothing (bool, optional): if True, replace softmax function
            in computing attention weights with sigmoid function for smoothing
        input_feeding_approach (bool, optional): See detail in
            Luong, Minh-Thang, Hieu Pham, and Christopher D. Manning.
            "Effective approaches to attention-based neural machine translation."
                arXiv preprint arXiv:1508.04025 (2015).
        coverage_weight (float, optional): the weight parameter for coverage
            computation.
        ctc_loss_weight (float): A weight parameter for auxiliary CTC loss
    """

    def __init__(self,
                 input_size,
                 encoder_type,
                 encoder_bidirectional,
                 encoder_num_units,
                 encoder_num_proj,
                 encoder_num_layers,
                 encoder_dropout,
                 attention_type,
                 attention_dim,
                 decoder_type,
                 decoder_num_units,
                 decoder_num_proj,
                 decoder_num_layers,
                 decoder_dropout,
                 embedding_dim,
                 embedding_dropout,
                 num_classes,
                 num_stack=1,
                 splice=1,
                 parameter_init=0.1,
                 subsample_list=[],
                 init_dec_state_with_enc_state=True,
                 sharpening_factor=1,
                 logits_temperature=1,
                 sigmoid_smoothing=False,
                 input_feeding_approach=False,
                 coverage_weight=0,
                 ctc_loss_weight=0):

        super(ModelBase, self).__init__()

        # TODO:
        # clip_activation
        # time_major

        assert input_size % 3 == 0, 'input_size must be divisible by 3 (+ delta, double delta features).'
        # NOTE: input features are expected to including Δ and ΔΔ features
        assert splice % 2 == 1, 'splice must be the odd number'

        # Setting for the encoder
        self.input_size = input_size * num_stack * splice
        self.num_channels = input_size // 3
        self.num_stack = num_stack
        self.splice = splice
        self.encoder_type = encoder_type
        self.encoder_bidirectional = encoder_bidirectional
        self.encoder_num_directions = 2 if encoder_bidirectional else 1
        self.encoder_num_units = encoder_num_units
        self.encoder_num_proj = encoder_num_proj
        self.encoder_num_layers = encoder_num_layers
        self.subsample_list = subsample_list
        self.encoder_dropout = encoder_dropout

        # Setting for the attention decoder
        self.attention_type = attention_type
        self.attention_dim = attention_dim
        self.decoder_type = decoder_type
        self.decoder_num_units = decoder_num_units
        self.decoder_num_proj = decoder_num_proj
        self.decoder_num_layers = decoder_num_layers
        self.decoder_dropout = decoder_dropout
        self.embedding_dim = embedding_dim
        self.embedding_dropout = embedding_dropout
        self.num_classes = num_classes + 2
        # NOTE: Add <SOS> and <EOS>
        self.sos_index = num_classes
        self.eos_index = num_classes + 1
        self.init_dec_state_with_enc_state = init_dec_state_with_enc_state
        self.sharpening_factor = sharpening_factor
        self.logits_temperature = logits_temperature
        self.sigmoid_smoothing = sigmoid_smoothing
        self.input_feeding_approach = input_feeding_approach
        self.coverage_weight = coverage_weight

        # Joint CTC-Attention
        self.ctc_loss_weight = ctc_loss_weight

        # Common setting
        self.parameter_init = parameter_init

        ####################
        # Encoder
        ####################
        # Load an instance
        if sum(subsample_list) == 0:
            encoder = load(encoder_type=encoder_type)
        else:
            encoder = load(encoder_type='p' + encoder_type)

        # Call the encoder function
        if encoder_type in ['lstm', 'gru', 'rnn']:
            if sum(subsample_list) == 0:
                self.encoder = encoder(
                    input_size=self.input_size,
                    rnn_type=encoder_type,
                    bidirectional=encoder_bidirectional,
                    num_units=encoder_num_units,
                    num_proj=encoder_num_proj,
                    num_layers=encoder_num_layers,
                    dropout=encoder_dropout,
                    parameter_init=parameter_init,
                    use_cuda=self.use_cuda,
                    batch_first=True)
            else:
                # Pyramidal encoder
                self.encoder = encoder(
                    input_size=self.input_size,
                    rnn_type=encoder_type,
                    bidirectional=encoder_bidirectional,
                    num_units=encoder_num_units,
                    num_proj=encoder_num_proj,
                    num_layers=encoder_num_layers,
                    dropout=encoder_dropout,
                    parameter_init=parameter_init,
                    subsample_list=subsample_list,
                    subsample_type='concat',
                    use_cuda=self.use_cuda,
                    batch_first=True)
        else:
            raise NotImplementedError

        ####################
        # Decoder
        ####################
        self.decoder = RNNDecoder(
            embedding_dim=embedding_dim,
            rnn_type=decoder_type,
            num_units=decoder_num_units,
            num_proj=decoder_num_proj,
            num_layers=decoder_num_layers,
            dropout=decoder_dropout,
            parameter_init=parameter_init,
            use_cuda=self.use_cuda,
            batch_first=True)

        ##############################
        # Attention layer
        ##############################
        self.attend = AttentionMechanism(
            encoder_num_units=encoder_num_units,
            decoder_num_units=decoder_num_units,
            attention_type=attention_type,
            attention_dim=attention_dim,
            sharpening_factor=sharpening_factor,
            sigmoid_smoothing=sigmoid_smoothing)

        ##################################################
        # Bridge layer between the encoder and decoder
        ##################################################
        if encoder_num_units != decoder_num_units:
            self.bridge = nn.Linear(
                encoder_num_units, decoder_num_units)
        else:
            self.bridge = None

        self.embedding = nn.Embedding(self.num_classes, embedding_dim)
        self.embedding_dropout = nn.Dropout(embedding_dropout)

        if input_feeding_approach:
            self.decoder_proj_layer = nn.Linear(
                decoder_num_units * 2, decoder_num_proj)
            # NOTE: input-feeding approach
            self.fc = nn.Linear(decoder_num_proj, self.num_classes)
        else:
            self.fc = nn.Linear(decoder_num_units, self.num_classes)
        # NOTE: <SOS> is removed because the decoder never predict <SOS> class
        # TODO: self.num_classes - 1

        if ctc_loss_weight > 0:
            # self.fc_ctc = nn.Linear(
            # encoder_num_units * self.encoder_num_directions, num_classes + 1)
            self.fc_ctc = nn.Linear(encoder_num_units, num_classes + 1)

    def forward(self, inputs, labels, inputs_seq_len, labels_seq_len,
                volatile=False):
        """Forward computation.
        Args:
            inputs (FloatTensor): A tensor of size `[B, T_in, input_size]`
            labels (LongTensor): A tensor of size `[B, T_out]`
            inputs_seq_len (IntTensor): A tensor of size `[B]`
            labels_seq_len (IntTensor): A tensor of size `[B]`
            volatile (bool): if True, the history will not be saved.
                This should be used in inference model for memory efficiency.
        Returns:
            loss (FloatTensor): A tensor of size `[1]`
        """
        # Encode acoustic features
        encoder_outputs, encoder_final_state, perm_indices = self._encode(
            inputs, inputs_seq_len, volatile=volatile)

        # Permutate indices
        labels = labels[perm_indices]
        inputs_seq_len = inputs_seq_len[perm_indices]
        labels_seq_len = labels_seq_len[perm_indices]

        # Teacher-forcing
        logits, attention_weights = self._decode_train(
            encoder_outputs, labels, encoder_final_state)

        # Output smoothing
        if self.logits_temperature != 1:
            logits /= self.logits_temperature

        batch_size, max_time = encoder_outputs.size()[:2]

        # Compute XE sequence loss
        num_classes = logits.size(2)
        logits = logits.view((-1, num_classes))
        labels_1d = labels[:, 1:].contiguous().view(-1)
        loss = F.cross_entropy(logits, labels_1d,
                               ignore_index=self.sos_index,
                               size_average=False)
        # NOTE: labels are padded by sos_index

        # Add coverage term
        if self.coverage_weight != 0:
            pass
            # coverage = self._compute_coverage(attention_weights)
            # loss += coverage_weight * coverage

        # Auxiliary CTC loss (optional)
        if self.ctc_loss_weight > 0:
            # Convert to 2D tensor
            encoder_outputs = encoder_outputs.contiguous()
            encoder_outputs = encoder_outputs.view(batch_size, max_time, -1)
            logits_ctc = self.fc_ctc(encoder_outputs)

            # Reshape back to 3D tensor
            logits_ctc = logits_ctc.view(batch_size, max_time, -1)

            # Convert to batch-major
            logits_ctc = logits_ctc.transpose(0, 1)

            ctc_loss_fn = CTCLoss()

            # Ignore <SOS> and <EOS>
            labels_seq_len -= 2

            # Concatenate all labels for warpctc_pytorch
            # `[B, T_out]` -> `[1,]`
            total_lables_seq_len = labels_seq_len.data.sum()
            concat_labels = Variable(
                torch.zeros(total_lables_seq_len)).int()
            label_counter = 0
            for i_batch in range(batch_size):
                concat_labels[label_counter:label_counter +
                              labels_seq_len.data[i_batch]] = labels[i_batch][1:labels_seq_len.data[i_batch] + 1]
                label_counter += labels_seq_len.data[i_batch]

            # Subsampling
            inputs_seq_len /= sum(self.subsample_list) * 2
            # NOTE: floor is not needed because inputs_seq_len is IntTensor

            ctc_loss = ctc_loss_fn(logits_ctc, concat_labels.cpu(),
                                   inputs_seq_len.cpu(), labels_seq_len.cpu())

            if self.use_cuda:
                ctc_loss = ctc_loss.cuda()

            # Linearly interpolate XE sequence loss and CTC loss
            loss = self.ctc_loss_weight * ctc_loss + \
                (1 - self.ctc_loss_weight) * loss

        # Average the loss by mini-batch
        loss /= batch_size

        return loss

    def _encode(self, inputs, inputs_seq_len, volatile):
        """Encode acoustic features.
        Args:
            inputs (FloatTensor): A tensor of size `[B, T_in, input_size]`
            inputs_seq_len (IntTensor): A tensor of size `[B]`
            volatile (bool): if True, the history will not be saved.
                This should be used in inference model for memory efficiency.
        Returns:
            encoder_outputs (FloatTensor): A tensor of size
                `[B, T_in, encoder_num_units]`
            encoder_final_state (FloatTensor): A tensor of size
                `[1, B, decoder_num_units (may be equal to encoder_num_units)]`
            perm_indices (FloatTensor):
        """
        encoder_outputs, encoder_final_state, perm_indices = self.encoder(
            inputs, inputs_seq_len, volatile, mask_sequence=True)
        # NOTE: encoder_outputs:
        # `[B, T_in, encoder_num_units * encoder_num_directions]`
        # encoder_final_state: `[1, B, encoder_num_units]`

        batch_size, max_time, encoder_num_units = encoder_outputs.size()

        # Sum bidirectional outputs
        if self.encoder_bidirectional:
            encoder_outputs = encoder_outputs[:, :, :encoder_num_units // 2] + \
                encoder_outputs[:, :, encoder_num_units // 2:]
            # NOTE: encoder_outputs: `[B, T_in, encoder_num_units]`

        # Bridge between the encoder and decoder
        if self.encoder_num_units != self.decoder_num_units:
            # Bridge between the encoder and decoder
            encoder_outputs = self.bridge(encoder_outputs)
            encoder_final_state = self.bridge(
                encoder_final_state.view(-1, encoder_num_units))
            encoder_final_state = encoder_final_state.view(1, batch_size, -1)

        return encoder_outputs, encoder_final_state, perm_indices

    def _compute_coverage(self, attention_weights):
        batch_size, max_time_outputs, max_time_inputs = attention_weights.size()
        raise NotImplementedError

    def _decode_train(self, encoder_outputs, labels, encoder_final_state=None):
        """Decoding when training.
        Args:
            encoder_outputs (FloatTensor): A tensor of size
                `[B, T_in, encoder_num_units]`
            labels (LongTensor): A tensor of size `[B, T_out]`
            encoder_final_state (FloatTensor, optional): A tensor of size
                `[1, B, encoder_num_units]`
        Returns:
            logits (FloatTensor): A tensor of size `[B, T_out, num_classes]`
            attention_weights (FloatTensor): A tensor of size
                `[B, T_out, T_in]`
        """
        ys = self.embedding(labels[:, :-1])
        # NOTE: remove <EOS>
        ys = self.embedding_dropout(ys)
        labels_max_seq_len = labels.size(1)

        # Initialize decoder state
        decoder_state = self._init_decoder_state(encoder_final_state)

        logits = []
        attention_weights = []
        attention_weights_step = None

        for t in range(labels_max_seq_len - 1):
            y = ys[:, t:t + 1, :]

            decoder_outputs, decoder_state, context_vector, attention_weights_step = self._decode_step(
                encoder_outputs,
                y,
                decoder_state,
                attention_weights_step)

            if self.input_feeding_approach:
                # Input-feeding approach
                output = self.decoder_proj_layer(
                    torch.cat([decoder_outputs, context_vector], dim=-1))
                logits_step = self.fc(F.tanh(output))
            else:
                logits_step = self.fc(decoder_outputs + context_vector)

            attention_weights.append(attention_weights_step)
            logits.append(logits_step)

        # Concatenate in T_out-dimension
        logits = torch.cat(logits, dim=1)
        attention_weights = torch.stack(attention_weights, dim=1)
        # NOTE; attention_weights in the training stage may be used for computing the
        # coverage, so do not convert to numpy yet.

        return logits, attention_weights

    def _init_decoder_state(self, encoder_final_state, volatile=False):
        """Initialize decoder state.
        Args:
            encoder_final_state (FloatTensor): A tensor of size
                `[1, B, encoder_num_units]`
            volatile (bool, optional): if True, the history will not be saved.
                This should be used in inference model for memory efficiency.
        Returns:
            decoder_state (FloatTensor): A tensor of size
                `[1, B, decoder_num_units]`
        """
        if self.init_dec_state_with_enc_state and encoder_final_state is None:
            raise ValueError('Set the final state of the encoder.')

        batch_size = encoder_final_state.size()[1]

        h_0 = Variable(torch.zeros(
            self.decoder_num_layers, batch_size, self.decoder_num_units))

        if volatile:
            h_0.volatile = True

        if self.use_cuda:
            h_0 = h_0.cuda()

        if self.init_dec_state_with_enc_state and self.encoder_type == self.decoder_type:
            # Initialize decoder state in the first layer with
            # the final state of the top layer of the encoder (forward)
            h_0[0, :, :] = encoder_final_state

        if self.decoder_type == 'lstm':
            c_0 = Variable(torch.zeros(
                self.decoder_num_layers, batch_size, self.decoder_num_units))

            if volatile:
                c_0.volatile = True

            if self.use_cuda:
                c_0 = c_0.cuda()

            decoder_state = (h_0, c_0)
        else:
            decoder_state = h_0

        return decoder_state

    def _decode_step(self, encoder_outputs, y, decoder_state,
                     attention_weights_step):
        """
        Args:
            encoder_outputs (FloatTensor): A tensor of size
                `[B, T_in, encoder_num_units]`
            y (FloatTensor): A tensor of size `[B, 1, embedding_dim]`
            decoder_state (FloatTensor): A tensor of size
                `[decoder_num_layers, B, decoder_num_units]`
            attention_weights_step (FloatTensor): A tensor of size `[B, T_in]`
        Returns:
            decoder_outputs (FloatTensor): A tensor of size
                `[B, 1, decoder_num_units]`
            decoder_state (FloatTensor): A tensor of size
                `[decoder_num_layers, B, decoder_num_units]`
            content_vector (FloatTensor): A tensor of size
                `[B, 1, encoder_num_units]`
            attention_weights_step (FloatTensor): A tensor of size `[B, T_in]`
        """
        decoder_outputs, decoder_state = self.decoder(y, decoder_state)

        # decoder_outputs: `[B, 1, decoder_num_units]`
        context_vector, attention_weights_step = self.attend(
            encoder_outputs,
            decoder_outputs,
            attention_weights_step)

        return decoder_outputs, decoder_state, context_vector, attention_weights_step

    def _create_token(self, value, batch_size):
        """Create 1 token per batch dimension.
        Args:
            value (int): the  value to pad
            batch_size (int): the size of mini-batch
        Returns:
            y (LongTensor): A tensor of size `[B, 1]`
        """
        y = np.full((batch_size, 1),
                    fill_value=value, dtype=np.int64)
        y = torch.from_numpy(y)
        y = Variable(y, requires_grad=False)
        y.volatile = True
        if self.use_cuda:
            y = y.cuda()
        # NOTE: y: `[B, 1]`

        return y

    def decode_infer(self, inputs, inputs_seq_len, beam_width=1,
                     max_decode_length=100):
        """
        Args:
            inputs (FloatTensor): A tensor of size `[B, T_in, input_size]`
            inputs_seq_len (IntTensor): A tensor of size `[B]`
            beam_width (int, optional): the size of beam
            max_decode_length (int, optional): the length of output sequences
                to stop prediction when EOS token have not been emitted
        Returns:

        """
        if beam_width == 1:
            return self._decode_infer_greedy(inputs, inputs_seq_len, max_decode_length)
        else:
            return self._decode_infer_beam(inputs, inputs_seq_len, beam_width, max_decode_length)

    def _decode_infer_greedy(self, inputs, inputs_seq_len, _max_decode_length):
        """Greedy decoding when inference.
        Args:
            inputs (FloatTensor): A tensor of size `[B, T_in, input_size]`
            inputs_seq_len (IntTensor): A tensor of size `[B]`
            _max_decode_length (int): the length of output sequences
                to stop prediction when EOS token have not been emitted
        Returns:
            argmaxs (np.ndarray): A tensor of size `[B, T_out]`
            attention_weights (np.ndarray): A tensor of size `[B, T_out, T_in]`
        """
        encoder_outputs, encoder_final_state = self._encode(
            inputs, inputs_seq_len, volatile=True)[:2]

        batch_size = inputs.size(0)

        # Start from <SOS>
        y = self._create_token(value=self.sos_index, batch_size=batch_size)

        # Initialize decoder state
        decoder_state = self._init_decoder_state(
            encoder_final_state, volatile=True)

        argmaxs = []
        attention_weights = []
        attention_weights_step = None

        for _ in range(_max_decode_length):
            y = self.embedding(y)
            y = self.embedding_dropout(y)
            # TODO: remove dropout??

            decoder_outputs, decoder_state, context_vector, attention_weights_step = self._decode_step(
                encoder_outputs,
                y,
                decoder_state,
                attention_weights_step)

            if self.input_feeding_approach:
                # Input-feeding approach
                output = self.decoder_proj_layer(
                    torch.cat([decoder_outputs, context_vector], dim=-1))
                logits = self.fc(F.tanh(output))
            else:
                logits = self.fc(decoder_outputs + context_vector)

            logits = logits.squeeze(dim=1)
            # NOTE: `[B, 1, num_classes]` -> `[B, num_classes]`

            # Path through the softmax layer & convert to log-scale
            log_probs = self.log_softmax(logits)

            # Pick up 1-best
            y = torch.max(log_probs, dim=1)[1]
            y = y.unsqueeze(dim=1)
            argmaxs.append(y)
            attention_weights.append(attention_weights_step)

            # Break if <EOS> is outputed in all mini-batch
            if torch.sum(y.data == self.eos_index) == y.numel():
                break

        # Concatenate in T_out dimension
        argmaxs = torch.cat(argmaxs, dim=1)
        attention_weights = torch.stack(attention_weights, dim=1)

        # Convert to numpy
        argmaxs = var2np(argmaxs)
        attention_weights = var2np(attention_weights)

        return argmaxs, attention_weights

    def _decode_infer_beam(self, inputs, inputs_seq_len, beam_width,
                           _max_decode_length):
        """Beam search decoding when inference.
        Args:
            inputs (FloatTensor): A tensor of size `[B, T_in, input_size]`
            inputs_seq_len (IntTensor): A tensor of size `[B]`
            beam_width (int): the size of beam
            _max_decode_length (int, optional): the length of output sequences
                to stop prediction when EOS token have not been emitted
        Returns:

        """
        encoder_outputs, encoder_final_state = self._encode(
            inputs, inputs_seq_len, volatile=True)[:2]

        batch_size = inputs.size(0)

        # Start from <SOS>
        y = self._create_token(value=self.sos_index, batch_size=1)
        ys = [y] * batch_size

        # Initialize decoder state
        decoder_state = self._init_decoder_state(
            encoder_final_state, volatile=True)

        beam = []
        for i_batch in range(batch_size):
            if self.decoder_type == 'lstm':
                h_n = decoder_state[0][:, i_batch:i_batch + 1, :].contiguous()
                c_n = decoder_state[1][:, i_batch:i_batch + 1, :].contiguous()
                beam.append([((self.sos_index,), LOG_1, (h_n, c_n))])
            else:
                h_n = decoder_state[:, i_batch:i_batch + 1, :].contiguous()
                beam.append([((self.sos_index,), LOG_1, h_n)])

        complete = [[]] * batch_size
        attention_weights = [] * batch_size
        attention_weights_step_list = [None] * batch_size

        for t in range(_max_decode_length):
            new_beam = [[]] * batch_size
            for i_batch in range(batch_size):
                for hyp, score, decoder_state in beam[i_batch]:
                    if t == 0:
                        # Start from <SOS>
                        y = ys[i_batch]
                    else:
                        y = self._create_token(value=hyp[-1],
                                               batch_size=1)
                    y = self.embedding(y)
                    y = self.embedding_dropout(y)
                    # TODO: remove dropout??

                    decoder_outputs, decoder_state, context_vector, attention_weights_step = self._decode_step(
                        encoder_outputs[i_batch:i_batch + 1, :, :],
                        y,
                        decoder_state,
                        attention_weights_step_list[i_batch])
                    attention_weights_step_list[i_batch] = attention_weights_step

                    if self.input_feeding_approach:
                        # Input-feeding approach
                        output = self.decoder_proj_layer(
                            torch.cat([decoder_outputs, context_vector], dim=-1))
                        logits = self.fc(F.tanh(output))
                    else:
                        logits = self.fc(decoder_outputs + context_vector)

                    logits = logits.squeeze(dim=1)
                    # NOTE: `[B, 1, num_classes]` -> `[B, num_classes]`

                    # Path through the softmax layer & convert to log-scale
                    log_probs = self.log_softmax(logits)
                    log_probs = var2np(log_probs).tolist()[0]

                    for i, log_prob in enumerate(log_probs):
                        # new_score = score + log_prob
                        new_score = _logsumexp(score, log_prob)
                        new_hyp = hyp + (i,)
                        new_beam[i_batch].append(
                            (new_hyp, new_score, decoder_state))
                new_beam[i_batch] = sorted(
                    new_beam[i_batch], key=lambda x: x[1], reverse=True)

                # Remove complete hypotheses
                for cand in new_beam[i_batch][:beam_width]:
                    if cand[0][-1] == self.eos_index:
                        complete[i_batch].append(cand)
                if len(complete[i_batch]) >= beam_width:
                    complete[i_batch] = complete[i_batch][:beam_width]
                    break
                beam[i_batch] = list(filter(lambda x: x[0][-1] !=
                                            self.eos_index, new_beam[i_batch]))
                beam[i_batch] = beam[i_batch][:beam_width]

        best = []
        for i_batch in range(batch_size):
            complete[i_batch] = sorted(
                complete[i_batch], key=lambda x: x[1], reverse=True)
            if len(complete[i_batch]) == 0:
                complete[i_batch] = beam[i_batch]
            hyp, score, _ = complete[i_batch][0]
            best.append(hyp[1:])
            # NOTE: remove <SOS>

        return np.array(best), attention_weights
