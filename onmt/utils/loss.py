"""
This includes: LossComputeBase and the standard NMTLossCompute, and
               sharded loss compute stuff.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
import onmt
from onmt.modules.sparse_losses import SparsemaxLoss
from onmt.modules.sparse_activations import LogSparsemax
from onmt.constants import ModelTask, DefaultTokens
from onmt.modules.copy_generator import collapse_copy_scores
from onmt.model_builder import load_test_model
try:
    import ctranslate2
except ImportError:
    pass   # this is tested when importing for loading a LM


class LossCompute(nn.Module):
    """
    Class for managing efficient loss computation. Handles
    accumulating multiple loss computations.

    Args:
        criterion (:obj:`nn. loss function`) : NLLoss or customed loss
        generator (:obj:`nn.Module`) :
        normalization (str): "tokens" or "sents"
        copy_attn (bool): whether copy attention mechanism is on/off
        lambda_coverage: Hyper-param to apply coverage attention if any
        lambda_align: Hyper-param for alignment loss
        tgt_shift_index (int): 1 for NMT, 0 for LM
        vocab: target vocab (for copy attention score calculation)
             module that maps the output of the decoder to a
             distribution over the target vocabulary.
        lm_generator (:obj:`ctranslate2.Generator`): LM Generator
        lm_prior_lambda (float): weight of LM model in loss
        lm_prior_tau (float): scaler for LM loss
    """
    def __init__(self, criterion, generator, normalization="tokens",
                 copy_attn=False, lambda_coverage=0.0, lambda_align=0.0,
                 tgt_shift_index=1, vocab=None, lm_generator=None,
                 lm_prior_lambda=None, lm_prior_tau=None,
                 lm_prior_model=None):
        super(LossCompute, self).__init__()
        self.criterion = criterion
        self.generator = generator
        self.normalization = normalization
        self.lambda_coverage = lambda_coverage
        self.lambda_align = lambda_align
        self.tgt_shift_index = tgt_shift_index
        self.copy_attn = copy_attn
        self.vocab = vocab  # target vocab for copy_attn need
        self.lm_generator = lm_generator
        self.lm_prior_lambda = lm_prior_lambda
        self.lm_prior_tau = lm_prior_tau
        self.lm_prior_model = lm_prior_model

    @classmethod
    def from_opts(cls, opt, model, vocab, train=True):
        """
        Returns a subclass which wraps around an nn.Module subclass
        (such as nn.NLLLoss) which defines the loss criterion. The LossCompute
        object passes relevant data to a Statistics object which handles
        training/validation logging.
        The Criterion and LossCompute options are triggered by opt settings.
        """
        device = torch.device("cuda" if onmt.utils.misc.use_gpu(opt)
                              else "cpu")

        padding_idx = vocab[DefaultTokens.PAD]
        unk_idx = vocab[DefaultTokens.UNK]

        if opt.lambda_coverage != 0:
            assert opt.coverage_attn, "--coverage_attn needs to be set in " \
                "order to use --lambda_coverage != 0"

        tgt_shift_idx = 1 if opt.model_task == ModelTask.SEQ2SEQ else 0

        if opt.copy_attn:
            criterion = onmt.modules.CopyGeneratorLoss(
                len(vocab), opt.copy_attn_force,
                unk_index=unk_idx, ignore_index=padding_idx
            )
        else:
            if opt.label_smoothing > 0 and train:
                criterion = LabelSmoothingLoss(
                    opt.label_smoothing, len(vocab),
                    ignore_index=padding_idx
                )
            elif isinstance(model.generator[-1], LogSparsemax):
                criterion = SparsemaxLoss(ignore_index=padding_idx,
                                          reduction='sum')
            else:
                criterion = nn.NLLLoss(ignore_index=padding_idx,
                                       reduction='sum')

        lm_prior_lambda = opt.lm_prior_lambda
        lm_prior_tau = opt.lm_prior_tau
        if opt.lm_prior_model:
            if opt.lm_prior_model[-3:] == ".pt":
                opt.gpu = 0
                opt.fp32 = False
                opt.int8 = False
                _, lm_prior_model, lm_model_opt \
                    = load_test_model(opt, model_path=opt.lm_prior_model)
                lm_prior_model.to(torch.device("cuda", opt.gpu))
                lm_prior_model.eval()
                lm_generator = None
            else:
                lm_prior_model = None
                try:
                    import ctranslate2
                    lm_generator = ctranslate2.Generator(
                        opt.lm_prior_model, device="cuda",
                        compute_type="float16")
                except ImportError:
                    raise ImportError("Could not import ctranslate2")
        else:
            lm_generator = None
            lm_prior_model = None

        # if the loss function operates on vectors of raw logits instead
        # of probabilities, only the first part of the generator needs to
        # be passed to the NMTLossCompute. At the moment, the only
        # supported loss function of this kind is the sparsemax loss.
        use_raw_logits = isinstance(criterion, SparsemaxLoss)
        loss_gen = model.generator[0] if use_raw_logits \
            else model.generator

        compute = cls(criterion, loss_gen,
                      normalization=opt.normalization,
                      copy_attn=opt.copy_attn,
                      lambda_coverage=opt.lambda_coverage,
                      lambda_align=opt.lambda_align,
                      tgt_shift_index=tgt_shift_idx,
                      vocab=vocab, lm_generator=lm_generator,
                      lm_prior_lambda=lm_prior_lambda,
                      lm_prior_tau=lm_prior_tau,
                      lm_prior_model=lm_prior_model)
        compute.to(device)

        return compute

    @property
    def padding_idx(self):
        return self.criterion.ignore_index

    def _compute_coverage_loss(self, std_attn, coverage_attn):
        """compute coverage loss"""
        covloss = torch.min(std_attn, coverage_attn).sum()
        covloss *= self.lambda_coverage
        return covloss

    def _compute_alignement_loss(self, align_head, ref_align):
        """Compute loss between 2 partial alignment matrix."""
        # align_head contains value in [0, 1) presenting attn prob,
        # 0 was resulted by the context attention src_pad_mask
        # So, the correspand position in ref_align should also be 0
        # Therefore, clip align_head to > 1e-18 should be bias free.
        align_loss = -align_head.clamp(min=1e-18).log().mul(ref_align).sum()
        align_loss *= self.lambda_align
        return align_loss

    def _compute_copy_loss(self, batch, output, target, align, attns):
        """Compute the copy attention loss.
        Args:
            batch: the current batch.
            output: the predict output from the model.
            target: the validate target to compare output with.
            align:
            attns: dictionary of attention distributions
              `(tgt_len, batch, src_len)`
        Returns:
            A tuple with the loss and raw scores.
        """
        scores = self.generator(self._bottle(output),
                                self._bottle(attns['copy']),
                                batch['src_map'])
        loss = self.criterion(scores, align, target).sum()

        return loss, scores

    def _compute_lm_loss_ct2(self, output, target):
        """
        Compute the loss between MT output and LM output
        https://github.com/cbaziotis/lm-prior-for-nmt/blob/master
        /fairseq_extension/user/lm_prior/lm_prior.py#L131-L133
        """

        # we use the raw logits, rescale with tau (temperature) and
        # apply the log_softmax. reminder generator[0] is just the nn.Linear
        scores = self.generator[0](self._bottle(output)) / self.lm_prior_tau
        scores = F.log_softmax(scores.to(torch.float32), dim=-1)

        src = target.detach().clone()
        src[src == self.vocab[DefaultTokens.EOS]] = self.padding_idx
        src_len = src[:, :, 0].ne(self.padding_idx).sum(1)
        # ct2 expects src with lengths without padding
        lm_scores = self.lm_generator.forward_batch(
            ctranslate2.StorageView.from_array(src[:, :, 0].to(torch.int32)),
            ctranslate2.StorageView.from_array(src_len.to(torch.int32)),
            return_log_probs=False)
        lm_scores = torch.as_tensor(lm_scores, device=scores.device)
        # again we use raw probs to rescale with tau and apply log_softmax
        lm_scores = self._bottle(lm_scores) / self.lm_prior_tau
        lm_scores = F.log_softmax(lm_scores.to(torch.float32), dim=-1)
        lm_scores[:, self.vocab[DefaultTokens.UNK]] = -50
        lm_scores[:, self.vocab[DefaultTokens.EOS]] -= 20
        # lm_scores are in log space so log_target=True
        lm_loss = F.kl_div(scores, lm_scores.detach().clone(),
                           reduction='none',
                           log_target=True).sum(-1)
        non_padding = self._bottle(output).ne(self.padding_idx)[:, 0]
        lm_loss = lm_loss.masked_select(non_padding).sum()
        lm_loss = lm_loss * (self.lm_prior_tau ** 2)
        return lm_loss

    def _compute_lm_loss(self, output, target):
        """
        Compute the loss between MT output and LM output
        https://github.com/cbaziotis/lm-prior-for-nmt/blob/master
        /fairseq_extension/user/lm_prior/lm_prior.py#L131-L133
        """

        # we use the raw logits, rescale with tau (temperature) and
        # apply the log_softmax. reminder generator[0] is just the nn.Linear
        scores = self.generator[0](self._bottle(output)) / self.lm_prior_tau
        scores = F.log_softmax(scores.to(torch.float32), dim=-1)

        src = target.detach().clone()
        src[src == self.vocab[DefaultTokens.EOS]] = self.padding_idx
        src_len = src[:, :, 0].ne(self.padding_idx).sum(1)
        # ct2 expects src with lengths without padding
        lm_outs, _ = self.lm_prior_model(src, None, src_len,
                                         with_align=False)
        lm_scores = self.lm_prior_model.generator[0](
            self._bottle(lm_outs)) / self.lm_prior_tau
        # again we use raw probs to rescale with tau and apply log_softmax
        lm_scores = F.log_softmax(lm_scores.to(torch.float32), dim=-1)
        lm_scores[:, self.vocab[DefaultTokens.UNK]] = -50
        lm_scores[:, self.vocab[DefaultTokens.EOS]] -= 20
        # lm_scores are in log space so log_target=True
        lm_loss = F.kl_div(scores, lm_scores.detach().clone(),
                           reduction='none', log_target=True).sum(-1)
        non_padding = self._bottle(output).ne(self.padding_idx)[:, 0]
        lm_loss = lm_loss.masked_select(non_padding).sum()
        lm_loss = lm_loss * (self.lm_prior_tau ** 2)
        return lm_loss

    def _bottle(self, _v):
        return _v.view(-1, _v.size(2))

    def _unbottle(self, _v, batch_size):
        return _v.view(-1, batch_size, _v.size(1))

    def forward(self, batch, output, attns,
                trunc_start=0, trunc_size=None):
        """Compute the forward loss, supports truncated BPTT for long
        sequences by taking a range in the decoder output sequence to
        back propagate in.
        Range is from `(trunc_start, trunc_start + trunc_size)`.
        Truncation is an approximate efficiency trick to relieve the
        memory required in the RNN buffers.

        Args:
          batch (batch) : batch of labeled examples
          output (:obj:`FloatTensor`) :
              output of decoder model ``(batch, tgt_len, hidden)``
          attns (dict) : dictionary of attention weights
              ``(batch, tgt_len, src_len)``
          trunc_start (int) : starting position of truncation window
          trunc_size (int) : length of truncation window

        Returns:
            A tuple with the loss and a :obj:`onmt.utils.Statistics` instance.
        """
        if trunc_size is None:
            trunc_size = batch['tgt'].size(1) - trunc_start
        # take into account here the tgt_shift_index (0 / 1 = LM/NMT)
        trunc_range = (trunc_start + self.tgt_shift_index,
                       trunc_start + trunc_size)
        target = batch['tgt'][:, trunc_range[0]:trunc_range[1],
                              :]
        flat_tgt = target[:, :, 0].contiguous().view(-1)

        if self.copy_attn:
            align = batch['alignment'][
                :, trunc_range[0]:trunc_range[1]
                ].contiguous().view(-1)
            loss, scores = self._compute_copy_loss(batch, output, flat_tgt,
                                                   align, attns)
            scores_data = collapse_copy_scores(
                self._unbottle(scores.clone(), len(batch['srclen'])),
                batch, self.vocab, None)
            scores_data = self._bottle(scores_data)
            # Correct target copy token instead of <unk>
            # tgt[i] = align[i] + len(tgt_vocab)
            # for i such that tgt[i] == 0 and align[i] != 0
            target_data = flat_tgt.clone()
            unk = self.criterion.unk_index
            correct_mask = (target_data == unk) & (align != unk)
            offset_align = align[correct_mask] + len(self.vocab)
            target_data[correct_mask] += offset_align
            scores = scores_data
            flat_tgt = target_data

        else:

            scores = self.generator(self._bottle(output))
            loss = self.criterion(scores, flat_tgt)

            if self.lambda_align != 0.0:
                align_head = attns['align']
                if align_head.dtype != loss.dtype:  # Fix FP16
                    align_head = align_head.to(loss.dtype)
                align_idx = batch['align']
                batch_size, pad_tgt_size, _ = batch['tgt'].size()
                _, pad_src_size, _ = batch['src'].size()
                align_matrix_size = [batch_size, pad_tgt_size, pad_src_size]
                ref_align = onmt.utils.make_batch_align_matrix(
                    align_idx, align_matrix_size, normalize=True
                )
                ref_align = ref_align[:, trunc_range[0]:trunc_range[1], :]
                if ref_align.dtype != loss.dtype:
                    ref_align = ref_align.to(loss.dtype)
                align_loss = self._compute_alignement_loss(
                    align_head=align_head, ref_align=ref_align)
                loss += align_loss

        if self.lambda_coverage != 0.0:
            coverage_loss = self._compute_coverage_loss(
                std_attn=attns['std'], coverage_attn=attns['coverage'])
            loss += coverage_loss

        if self.normalization == "tokens":
            normfactor = batch['tgt'][:,
                                      :,
                                      0].ne(self.padding_idx).sum()
        elif self.normalization == "sents":
            normfactor = batch['tgt'].size(0)

        if self.lm_generator is not None:
            lm_loss = self._compute_lm_loss_ct2(output, target)
            loss = loss + lm_loss * self.lm_prior_lambda

        if self.lm_prior_model is not None:
            lm_loss = self._compute_lm_loss(output, target)
            loss = loss + lm_loss * self.lm_prior_lambda

        stats = self._stats(len(batch['srclen']), loss.sum().item(),
                            scores, flat_tgt)

        return loss / float(normfactor), stats

    def _stats(self, bsz, loss, scores, target):
        """
        Args:
            loss (int): the loss computed by the loss criterion.
            scores (:obj:`FloatTensor`): a score for each possible output
            target (:obj:`FloatTensor`): true targets

        Returns:
            :obj:`onmt.utils.Statistics` : statistics for this batch.
        """
        pred = scores.max(1)[1]
        non_padding = target.ne(self.padding_idx)
        num_correct = pred.eq(target).masked_select(non_padding).sum().item()
        num_non_padding = non_padding.sum().item()
        # in the case criterion reduction is None then we need
        # to sum the loss of each sentence in the batch
        return onmt.utils.Statistics(loss=loss,
                                     n_batchs=1,
                                     n_sents=bsz,
                                     n_words=num_non_padding,
                                     n_correct=num_correct)


class LabelSmoothingLoss(nn.Module):
    """
    With label smoothing,
    KL-divergence between q_{smoothed ground truth prob.}(w)
    and p_{prob. computed by model}(w) is minimized.
    """
    def __init__(self, label_smoothing, tgt_vocab_size, ignore_index=-100):
        assert 0.0 < label_smoothing <= 1.0
        self.ignore_index = ignore_index
        super(LabelSmoothingLoss, self).__init__()

        smoothing_value = label_smoothing / (tgt_vocab_size - 2)
        one_hot = torch.full((tgt_vocab_size,), smoothing_value)
        one_hot[self.ignore_index] = 0
        self.register_buffer('one_hot', one_hot.unsqueeze(0))

        self.confidence = 1.0 - label_smoothing

    def forward(self, output, target):
        """
        output (FloatTensor): ``(batch_size, n_classes)``
        target (LongTensor): ``(batch_size)``
        """
        model_prob = self.one_hot.repeat(target.size(0), 1)
        model_prob.scatter_(1, target.unsqueeze(1), self.confidence)
        model_prob.masked_fill_((target == self.ignore_index).unsqueeze(1), 0)

        return F.kl_div(output, model_prob, reduction='sum')
