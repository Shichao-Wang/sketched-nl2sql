""" model """
from typing import List, Tuple

import torch
from torch import nn, Tensor
from torch.nn.utils import rnn
from transformers import AutoModel

from sketched_nl2sql.modules.query_predictor import (
    AggregatorPredictor,
    SelectPredictor,
    WhereColumnPredictor,
    WhereNumPredictor,
    WhereOperatorPredictor,
    WhereValuePredictor,
)
from torchnlp.config import Config
from torchnlp.vocab import Vocab


class SketchedTextToSql(nn.Module):
    """ sketched text to sql model """

    PAD_SEG = 0
    Q_SEG = 1
    H_SEG = 2
    CLS_SEG = 3
    SEP_SEG = 4

    def __init__(
        self,
        vocab: Vocab,
        embedder: nn.Module,
        select_column_predictor: SelectPredictor,
        select_aggregator_predictor: AggregatorPredictor,
        where_num_predictor: WhereNumPredictor,
        where_column_predictor: WhereColumnPredictor,
        where_operator_predictor: WhereOperatorPredictor,
        where_value_predictor: WhereValuePredictor,
    ):
        super().__init__()
        self.vocab = vocab
        self.embedder = embedder
        self.scp = select_column_predictor
        self.sap = select_aggregator_predictor
        self.wnp = where_num_predictor
        self.wcp = where_column_predictor
        self.wop = where_operator_predictor
        self.wvp = where_value_predictor

    @classmethod
    def from_config(cls, vocab, config: Config):
        """ create model from config """
        embedder = AutoModel.from_pretrained(config.get("pretrained_model_name"))
        num_agg_op = 6
        num_conds = 5
        num_op = 4
        hidden_dim = config.get("hidden_dim")
        bidirectional = config.get("bidirectional")
        num_layers = config.get("num_layers")
        dropout = config.get("dropout")
        scp = SelectPredictor(embedder.config.hidden_size, hidden_dim, bidirectional, num_layers, dropout, True)
        sap = AggregatorPredictor(
            embedder.config.hidden_size, hidden_dim, num_agg_op, bidirectional, num_layers, dropout, True
        )
        wnp = WhereNumPredictor(embedder.config.hidden_size, hidden_dim, num_conds, bidirectional, num_layers, dropout)
        wcp = WhereColumnPredictor(embedder.config.hidden_size, hidden_dim, bidirectional, num_layers, dropout, True)
        wop = WhereOperatorPredictor(
            embedder.config.hidden_size, hidden_dim, num_op, bidirectional, num_layers, dropout, True
        )
        wvp = WhereValuePredictor(embedder.config.hidden_size, hidden_dim, bidirectional, num_layers, dropout, True)

        return cls(vocab, embedder, scp, sap, wnp, wcp, wop, wvp)

    def forward(self, question_tokens: Tensor, headers_tokens: Tensor, num_headers: List[int]) -> Tuple[Tensor, ...]:
        """ forward """
        with torch.no_grad():
            bert_inputs, bert_mask = self.pack_bert_input(question_tokens, headers_tokens, num_headers)

        bert_embedding: Tensor
        bert_embedding, *_ = self.embedder(bert_inputs)

        with torch.no_grad():
            question_embedding, headers_embeddings = self.unpack_encoder_output(bert_embedding, bert_mask)

        select_logits = self.scp(question_embedding, headers_embeddings, num_headers)
        agg_logits = self.sap(question_embedding, headers_embeddings, num_headers)
        where_num_logits = self.wnp(question_embedding)  # type: Tensor
        where_col_logits = self.wcp(question_embedding, headers_embeddings, num_headers)  # type: Tensor
        where_op_logits = self.wop(question_embedding, headers_embeddings, num_headers)
        value_start_logits, value_end_logits = self.wvp(
            question_embedding, headers_embeddings, num_headers, where_num_logits, where_col_logits
        )
        return (
            select_logits,
            agg_logits,
            where_num_logits,
            where_col_logits,
            where_op_logits,
            value_start_logits,
            value_end_logits,
        )

    def pack_bert_input(self, question_tokens: Tensor, headers_tokens: Tensor, num_headers: List[int]):
        """
        :param question_tokens: (batch_size, max_sequence_length)
        :param headers_tokens: (batch_size * num_headers, max_header_length)
        :param num_headers: [batch_size]
        :return:
        """
        device = question_tokens.device
        batch_size = len(num_headers)
        unpacked_headers_tokens = headers_tokens.split(num_headers)
        tensors = []
        segments = []
        for b in range(batch_size):

            tensor = [torch.as_tensor((self.vocab.cls_index,), device=device)]
            segment = [self.CLS_SEG]

            for h_tokens in unpacked_headers_tokens[b]:  # type: Tensor
                tensor.append(h_tokens.masked_select(h_tokens != 0))
                segment.extend([self.H_SEG] * len(tensor[-1]))
                tensor.append(torch.as_tensor((self.vocab.sep_index,), device=device))
                segment.extend([self.SEP_SEG])

            tensor.append(question_tokens[b].masked_select(question_tokens[b] != 0))
            segment.extend([self.Q_SEG] * len(tensor[-1]))
            tensors.append(torch.cat(tensor))
            segments.append(torch.as_tensor(segment))

        padded_tensors = rnn.pad_sequence(tensors, batch_first=True, padding_value=self.vocab.pad_index)
        padded_segments = rnn.pad_sequence(segments, batch_first=True, padding_value=self.PAD_SEG)
        return padded_tensors, padded_segments

    def unpack_encoder_output(self, encoder_output: torch.Tensor, segment: torch.Tensor) -> Tuple[Tensor, Tensor]:
        """
        :param encoder_output: (batch_size, length, hidden_dim)
        :param segment: (batch_size, length)
        :return: headers_hiddens, headers_type_hidden, question_encoding
        """
        question_hidden = rnn.pad_sequence(
            [out[s == self.Q_SEG] for out, s in zip(encoder_output, segment)],
            batch_first=True,
            padding_value=self.vocab.pad_index,
        )
        batch_size = encoder_output.size(0)
        headers_hiddens: List[Tensor] = []
        for b in range(batch_size):
            hidden_list = []
            i = 0
            skip = True  # mode flag, denote if it is in skip or collect
            for j, seg in enumerate(segment[b]):
                if skip and seg == self.H_SEG:
                    # see a header segment, start collect mode
                    i = j
                    skip = False
                if not skip and seg != self.H_SEG:
                    # not a header segment, stop collect
                    hidden_list.append(encoder_output[b, i:j])
                    i = j
                    skip = True
            headers_hiddens.extend(hidden_list)
        padded_hiddens: Tensor = rnn.pad_sequence(headers_hiddens, batch_first=True, padding_value=self.vocab.pad_index)
        return question_hidden, padded_hiddens
