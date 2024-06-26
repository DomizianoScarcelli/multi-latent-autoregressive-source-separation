import torch
from tqdm import tqdm
from lass_mnist.lass.diba_interaces import UnconditionedTransformerPrior
from typing import List, Optional
from .utils import normalize_logits
import torch.nn.functional as F
from transformers import GPT2LMHeadModel

class DirectedGraphicalModel:
    """
    Represents the Bayesian Network with the latent codes z_1,...,z_n and the
    mixtures m_1,...,m_n at all the stages i.
    """

    def __init__(self, 
                 transformer: GPT2LMHeadModel,
                 num_sources:int):
        self.K = 256 #possible discrete values in the latent codes
        self.num_sources = num_sources
        self.transformer = transformer

        # p_mmzs[i] = p(m_i | m{i-1}, z_i)
        p_mmzs_path = "./lass_mnist/models/sums-MNIST-gm/best.pt"
        with open(p_mmzs_path, "rb") as f:
            self.p_mmzs = torch.log(torch.load(f) + 1e-16)
            # Normalization
            self.p_mmzs -= torch.logsumexp(self.p_mmzs, dim=0)
            print(f"p_mmzs has shape: {self.p_mmzs.shape}")
    
    def compute_priors(self, past) -> List[torch.Tensor]:
        priors = []
        for _ in range(self.num_sources):
            priors.append(UnconditionedTransformerPrior(transformer=self.transformer, sos=0))

        # p_zs[i] = p(z_i)
        self.p_zs = []
        for i in range(self.num_sources):
            log_prior, _ = priors[i].get_logits(
                token_ids=past[i],
                past_key_values=None,
            )
            log_prior = normalize_logits(log_prior).squeeze()
            self.p_zs.append(log_prior)

        return priors

    def forward_pass(self, i: int, token_idx: torch.Tensor):
        """
        It computes the message μα in the graphical model forward pass.
        """
        # shape is [256, 256, 256] = [K, K, K]
        curr_p_mmz = self.p_mmzs[i, token_idx]
        # shape is [2, 256] = [num_sources, K]
        curr_p_z = self.p_zs[i]
        if i == 0:
            message = torch.logsumexp(curr_p_mmz + curr_p_z, dim=0)
            return message
        new_message = torch.logsumexp(curr_p_mmz + curr_p_z, dim=1)
        old_message = torch.logsumexp(self.p_zs[i] + self.forward_results[i-1], dim=1)
        final_message = new_message + old_message
        return final_message

    def backward_pass(self, i: int, token_idx: torch.Tensor):
        """
        It computes the message μβ in the graphical model backward pass.
        """
        if i == self.num_sources-1:
            #TODO: see better the axis where to perform logsumexp
            message = torch.logsumexp(self.p_zs[i+1], dim=0) + torch.logsumexp(self.p_mmzs[i], dim=0)
            old_message = torch.logsumexp(self.p_mmzs[i+1] + self.backward_results[i+2], dim=0)
            # print(f"backward old_message shape: {old_message.shape}")
            final_message = message + old_message
            # print(f"backward final_message shape: {final_message.shape}")
            return final_message
        
        message = torch.logsumexp(self.p_zs[i+1] + self.p_mmzs[i, token_idx], dim=-1)
        return message
    
    def compute_marginals(self, i: int) -> torch.Tensor:
        if i == 0:
            return self.p_zs[i] + self.backward_results[i]
        elif i == self.num_sources-1:
            return self.p_zs[i] + self.forward_results[i-1]
        else:
            return self.p_zs[i] + torch.logsumexp(self.forward_results[i-1] + self.backward_results[i], dim=0)

    def single_sample(self, marginals: torch.Tensor, mixture: torch.Tensor, i: int) -> torch.Tensor:
        #TODO: I don't know if I have to use the mixture in some ways here
        results = torch.distributions.Categorical(logits=marginals).sample()
        return results.long()
    
    def single_separate(self, mixture: torch.Tensor, i: int) -> torch.Tensor:
        self.forward_results: List[Optional[torch.Tensor]] = []
        self.backward_results: List[Optional[torch.Tensor]] = [None for _ in range(self.num_sources-1)]
        self.marginal_results = []
        for i in range(self.num_sources-1):
            forward = self.forward_pass(i, mixture[i])
            self.forward_results.append(forward)
        
        backward_range = list(reversed([i for i in range(self.num_sources-1)]))
        # print(f"Initializing backward pass on the sequence: {backward_range}")
        for i in backward_range: 
            backward = self.backward_pass(i, mixture[i])
            self.backward_results[i] = backward
            # print([f"Full: {elem.shape}" if elem is not None else "Empty" for elem in self.backward_results])
        
        for i in range(self.num_sources):
            marginal = self.compute_marginals(i)
            self.marginal_results.append(marginal)

        marginals = torch.stack(self.marginal_results)
        result = self.single_sample(marginals, mixture, i)
        return result

    def separate(self, mixture: torch.Tensor) -> torch.Tensor:
        self.prior_past = torch.full((2, self.K, len(mixture)+1), fill_value=-1, dtype=torch.long)
        self.prior_past[:,:, 0] = 0

        for i in tqdm(range(len(mixture)), desc="Separating mixture..."):
            self.compute_priors(past=self.prior_past[:, :, :i+1])
            sample = self.single_separate(mixture, i)
            self.prior_past[:, :, i+1] = sample
            print(self.prior_past)
        return self.prior_past[:,:,1:]




