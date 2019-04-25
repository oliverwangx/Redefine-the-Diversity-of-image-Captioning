import random

import numpy as np

import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F

from .box_attention import Attention
from .baseRNN import BaseRNN

if torch.cuda.is_available():
    import torch.cuda as device
else:
    import torch as device

device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

class DecoderRNN(BaseRNN):
    r"""
    Provides functionality for decoding in a seq2seq framework, with an option for attention.

    Args:
        vocab_size (int): size of the vocabulary
        max_len (int): a maximum allowed length for the sequence to be processed
        hidden_size (int): the number of features in the hidden state `h`
        sos_id (int): index of the start of sentence symbol
        eos_id (int): index of the end of sentence symbol
        n_layers (int, optional): number of recurrent layers (default: 1)
        rnn_cell (str, optional): type of RNN cell (default: gru)
        bidirectional (bool, optional): if the encoder is bidirectional (default False)
        input_dropout_p (float, optional): dropout probability for the input sequence (default: 0)
        dropout_p (float, optional): dropout probability for the output sequence (default: 0)
        use_attention(bool, optional): flag indication whether to use attention mechanism or not (default: false)

    Attributes:
        KEY_ATTN_SCORE (str): key used to indicate attention weights in `ret_dict`
        KEY_LENGTH (str): key used to indicate a list representing lengths of output sequences in `ret_dict`
        KEY_SEQUENCE (str): key used to indicate a list of sequences in `ret_dict`

    Inputs: inputs, encoder_hidden, encoder_outputs, function, teacher_forcing_ratio
        - **inputs** (batch, seq_len, input_size): list of sequences, whose length is the batch size and within which
          each sequence is a list of token IDs.  It is used for teacher forcing when provided. (default `None`)
        - **encoder_hidden** (num_layers * num_directions, batch_size, hidden_size): tensor containing the features in the
          hidden state `h` of encoder. Used as the initial hidden state of the decoder. (default `None`)
        - **encoder_outputs** (batch, seq_len, hidden_size): tensor with containing the outputs of the encoder.
          Used for attention mechanism (default is `None`).
        - **function** (torch.nn.Module): A function used to generate symbols from RNN hidden state
          (default is `torch.nn.functional.log_softmax`).
        - **teacher_forcing_ratio** (float): The probability that teacher forcing will be used. A random number is
          drawn uniformly from 0-1 for every decoding token, and if the sample is smaller than the given value,
          teacher forcing would be used (default is 0).

    Outputs: decoder_outputs, decoder_hidden, ret_dict
        - **decoder_outputs** (seq_len, batch, vocab_size): list of tensors with size (batch_size, vocab_size) containing
          the outputs of the decoding function.
        - **decoder_hidden** (num_layers * num_directions, batch, hidden_size): tensor containing the last hidden
          state of the decoder.
        - **ret_dict**: dictionary containing additional information as follows {*KEY_LENGTH* : list of integers
          representing lengths of output sequences, *KEY_SEQUENCE* : list of sequences, where each sequence is a list of
          predicted token IDs }.
    """

    KEY_ATTN_SCORE = 'attention_score'
    KEY_LENGTH = 'length'
    KEY_SEQUENCE = 'sequence'

    def __init__(self, vocab_size, max_len, hidden_size, embedding_size,
                 sos_id, eos_id, embedding_parameter=None,
                 n_layers=1, rnn_cell='lstm', bidirectional=False,
                 input_dropout_p=0, dropout_p=0, use_attention=False, update_embedding=False, use_prob_vector=False, force_max_len=False, beamSearchMode=False,topk=4):
        super(DecoderRNN, self).__init__(vocab_size, max_len, hidden_size,
                                         input_dropout_p, dropout_p,
                                         n_layers, rnn_cell)

        self.topk = topk
        self.beamSearchMode = beamSearchMode
        self.bidirectional_encoder = bidirectional
        self.rnn = self.rnn_cell(embedding_size, hidden_size, n_layers, batch_first=True, dropout=dropout_p)

        self.output_size = vocab_size
        self.max_length = max_len
        self.use_attention = use_attention
        self.eos_id = eos_id
        self.sos_id = sos_id
        self.force_max_len = force_max_len

        self.init_input = None

        if use_prob_vector:
            self.embedding = nn.Linear(vocab_size,embedding_size,bias=False)

        else:
            self.embedding = nn.Embedding(vocab_size,embedding_size)
        self.use_prob_vector = use_prob_vector
        if embedding_parameter is not None:
            embedding_parameter = torch.FloatTensor(embedding_parameter).to(device)
            if use_prob_vector:
                embedding_parameter = embedding_parameter.t()
            self.embedding.weight = nn.Parameter(embedding_parameter)
        self.embedding.weight.requires_grad = update_embedding

        if use_attention:
            self.attention = Attention(self.hidden_size)

        self.out = nn.Linear(self.hidden_size, self.output_size)

    def forward_step(self, input_var, hidden, encoder_outputs, function, prev_maxes=None):
        batch_size = input_var.size(0)
        output_size = input_var.size(1)
        embedded = self.embedding(input_var)
        embedded = self.input_dropout(embedded)

        output, hidden = self.rnn(embedded, hidden)

        attn = None
        if self.use_attention:
            output, attn = self.attention(output, encoder_outputs)

        logits = self.out(output.contiguous().view(-1, self.hidden_size))
        if output_size == 1 and (prev_maxes is not None):

            for i in range(len(prev_maxes)):

                logits.scatter_(1,prev_maxes[i],1e-18)
            

        predicted_softmax = function(logits, dim=1).view(batch_size, output_size, -1)
        return predicted_softmax, hidden, attn

    def forward(self, inputs=None, beamStates=None, encoder_hidden=None, encoder_outputs=None,
                    function=F.softmax, teacher_forcing_ratio=0, max_len=15, semi_sup=False):
        self.max_length = max_len
        ret_dict = dict()
        if self.use_attention:
            ret_dict[DecoderRNN.KEY_ATTN_SCORE] = list()

        inputs, batch_size, max_length = self._validate_args(inputs, encoder_hidden, encoder_outputs,
                                                             function, teacher_forcing_ratio)
        batch_size = beamStates['topkInds'][0].shape[1]
        decoder_hidden = self._init_state(encoder_hidden)

        use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False

        decoder_outputs = []
        sequence_symbols = []
        lengths = np.array([max_length] * batch_size)
        beamStatesLM = {}
        beamStatesLM['probVec'] = [[]] # sentence len list
        beamStatesLM['hiddens'] = [beamStates['cells'][0].clone()] # seq len, topk, batch
        beamStatesLM['cells'] = [beamStates['cells'][0].clone()] # seq len, topk, batch

        def decode(step, step_output, step_attn):
            decoder_outputs.append(step_output)
            if self.use_attention:
                ret_dict[DecoderRNN.KEY_ATTN_SCORE].append(step_attn)
            symbols = step_output.topk(1)[1]#decoder_outputs[-1].topk(1)[1]
            sequence_symbols.append(symbols)

            eos_batches = symbols.data.eq(self.eos_id)
            if (eos_batches.dim() > 0) and (not self.force_max_len):
                eos_batches = eos_batches.cpu().view(-1).numpy()       
                update_idx = ((lengths > step) & eos_batches) != 0
                lengths[update_idx] = len(sequence_symbols)
            return symbols

        def getTopkIndsnScores(candScores):
            tmp = candScores[:,:,0].permute(1,0,2).contiguous()
            B,K,V = tmp.shape
            zzz = torch.topk(tmp.view(B,-1),self.topk,dim=1,largest=False)
            bmScores_nxt = zzz[0].transpose(0,1) # B,K
            inds = zzz[1].transpose(0,1)
            bmTopkInds_nxt = torch.stack([inds/V,inds%V],dim=2)
            return bmTopkInds_nxt, bmScores_nxt
        # Manual unrolling is used to support random teacher forcing.
        # If teacher_forcing_ratio is True or False instead of a probability, the unrolling can be done in graph

        if use_teacher_forcing:
            decoder_input = inputs[:, :-1]
            decoder_output, decoder_hidden, attn = self.forward_step(decoder_input, decoder_hidden, encoder_outputs,
                                                                     function=function)

            for di in range(decoder_output.size(1)):
                step_output = decoder_output[:, di, :]
                if attn is not None:
                    step_attn = attn[:, di, :]
                else:
                    step_attn = None
                decode(di, step_output, step_attn)
        elif self.beamSearchMode:
            decoder_input = inputs[:, 0].unsqueeze(1)

            for di in range(max_length):
                bmHiddens = beamStatesLM['hiddens'][di]
                bmCells = beamStatesLM['cells'][di]
                bmTopkInds = beamStates['topkInds'][di]
                bmScores = beamStates['newScores'][di]
                bmProbVec_nxt = []
                bmHiddens_nxt = []
                bmCells_nxt = []
                bmTopkInds_nxt = []
                bmScores_nxt = []

                for k in range(len(bmScores)): # iterate over topk. for the 0 step, topk has 1 element. 
                    hidGathInds = bmTopkInds[k][:,0].unsqueeze(0).repeat(1,1024).view(1,batch_size,-1)
                    decoder_output, decoder_hidden, step_attn = self.forward_step(bmTopkInds[k][:,1].unsqueeze(1), (bmHiddens[:,0].gather(0,hidGathInds),bmCells[:,0].gather(0,hidGathInds)), encoder_outputs,function=function, prev_maxes=None)
                    bmProbVec_nxt.append(decoder_output)
                    bmHiddens_nxt.append(decoder_hidden[0])
                    bmCells_nxt.append(decoder_hidden[1])

                bmProbVec_nxt = torch.stack(bmProbVec_nxt,dim=0) #[K,B,1,V]
                bmHiddens_nxt = torch.stack(bmHiddens_nxt,dim=0) #[K,1,B,H]
                bmCells_nxt = torch.stack(bmCells_nxt,dim=0) #[K,1,B,H]
                # extract topk from bmProbVec_nxt list
                candScores = -torch.log(bmProbVec_nxt+1e-18)*(bmScores.unsqueeze(-1).unsqueeze(-1)) # [K,B,V]
                bmTopkInds_nxt, bmScores_nxt = getTopkIndsnScores(candScores)  # [K,B,2], [K,B]
                beamStatesLM['probVec'].append(bmProbVec_nxt)
                beamStatesLM['hiddens'].append(bmHiddens_nxt)
                beamStatesLM['cells'].append(bmCells_nxt)

                






        else:
            decoder_input = inputs[:, 0].unsqueeze(1)
            for di in range(max_length):
                decoder_output, decoder_hidden, step_attn = self.forward_step(decoder_input, decoder_hidden, encoder_outputs,
                                                                         function=function, prev_maxes=sequence_symbols)
                step_output = decoder_output.squeeze(1)
                symbols = decode(di, step_output, step_attn)
                decoder_input = symbols if not self.use_prob_vector else step_output.unsqueeze(1)

        ret_dict[DecoderRNN.KEY_SEQUENCE] = sequence_symbols
        ret_dict[DecoderRNN.KEY_LENGTH] = lengths.tolist()
        ret_dict['beamStates'] = beamStatesLM
        return decoder_outputs, decoder_hidden, ret_dict

    def _init_state(self, encoder_hidden):
        """ Initialize the encoder hidden state. """
        if encoder_hidden is None:
            return None
        if isinstance(encoder_hidden, tuple):
            encoder_hidden = tuple([self._cat_directions(h) for h in encoder_hidden])
        else:
            encoder_hidden = self._cat_directions(encoder_hidden)
        return encoder_hidden

    def _cat_directions(self, h):
        """ If the encoder is bidirectional, do the following transformation.
            (#directions * #layers, #batch, hidden_size) -> (#layers, #batch, #directions * hidden_size)
        """
        if self.bidirectional_encoder:
            h = torch.cat([h[0:h.size(0):2], h[1:h.size(0):2]], 2)
        return h

    def _validate_args(self, inputs, encoder_hidden, encoder_outputs, function, teacher_forcing_ratio):
        if self.use_attention:
            if encoder_outputs is None:
                raise ValueError("Argument encoder_outputs cannot be None when attention is used.")

        # inference batch size
        if inputs is None and encoder_hidden is None:
            batch_size = 1
        else:
            if inputs is not None:
                batch_size = inputs.size(0)
            else:
                if self.rnn_cell is nn.LSTM:
                    batch_size = encoder_hidden[0].size(1)
                elif self.rnn_cell is nn.GRU:
                    batch_size = encoder_hidden.size(1)

        # set default input and max decoding length
        if inputs is None:
            if teacher_forcing_ratio > 0:
                raise ValueError("Teacher forcing has to be disabled (set 0) when no inputs is provided.")
            if not self.use_prob_vector:
                inputs = torch.LongTensor([self.sos_id] * batch_size).view(batch_size, 1)
            else:
                inputs = torch.zeros([batch_size, self.output_size],dtype=torch.float).scatter_(1,torch.LongTensor([[self.sos_id]]*batch_size),1.).unsqueeze(1)

            if torch.cuda.is_available():
                inputs = inputs.to(device)
            max_length = self.max_length
        else:
            max_length = inputs.size(1) - 1 # minus the start of sequence symbol

        return inputs, batch_size, max_length