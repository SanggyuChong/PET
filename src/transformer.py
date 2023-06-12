from utilities import get_all_species, get_compositional_features
import os

import torch
import ase.io
import numpy as np
from multiprocessing import cpu_count
from pathos.multiprocessing import ProcessingPool as Pool
from tqdm import tqdm
import torch_geometric
from torch_geometric.data import Data
from torch_geometric.loader import DataLoader
from torch import nn
import torch.nn.functional as F
from sklearn.linear_model import Ridge

import time
from scipy.spatial.transform import Rotation
from torch.optim.lr_scheduler import StepLR
import sys
import copy


class AttentionBlock(nn.Module):
    def __init__(self, total_dim, num_heads, dropout = 0.0, epsilon = 1e-15):
        super(AttentionBlock, self).__init__()
        
        self.input_linear = nn.Linear(total_dim, 3 * total_dim)
        self.dropout = nn.Dropout(dropout)
        self.output_linear = nn.Linear(total_dim, total_dim)        
        
        nn.init.xavier_uniform_(self.input_linear.weight)
        nn.init.constant_(self.input_linear.bias, 0.0)
        nn.init.constant_(self.output_linear.bias, 0.0) 
        
        self.num_heads = num_heads
        self.epsilon = epsilon
        
        if total_dim % num_heads != 0:
            raise ValueError("total dimension is not divisible by the number of heads")
        self.head_dim = total_dim // num_heads        
        self.preconditioning = 1.0 / np.sqrt(self.head_dim)
        
    def forward(self, x, multipliers = None):
        initial_shape = x.shape        
        x = self.input_linear(x)
        x = x.reshape(initial_shape[0], initial_shape[1], 3, self.num_heads, self.head_dim)
        x = x.permute(2, 0, 3, 1, 4)
        
        queries, keys, values = x[0], x[1], x[2]
        alpha = torch.matmul(queries, keys.transpose(-2, -1)) * self.preconditioning       
        alpha = F.softmax(alpha, dim = -1)        
        alpha = self.dropout(alpha)
        
        if multipliers is not None:
            alpha = alpha * multipliers[:, None, :, :]
            alpha = alpha / (alpha.sum(dim = -1)[..., None] + self.epsilon)            

        x = torch.matmul(alpha, values).transpose(1, 2).reshape(initial_shape)
        x = self.output_linear(x)       
        return x
    
    
class TransformerLayer(torch.nn.Module):
    def __init__(self, d_model, n_heads, dim_feedforward = 512, dropout = 0.0,
                 activation = F.silu):
        
        super(TransformerLayer, self).__init__()
        self.attention = AttentionBlock(d_model, n_heads, dropout = dropout) 
        
        self.norm_attention = nn.LayerNorm(d_model)
        self.norm_mlp = nn.LayerNorm(d_model)
        self.dropout = nn.Dropout(dropout)        

        self.activation = activation
        
        self.mlp = nn.Sequential(nn.Linear(d_model, dim_feedforward),
                                 self.activation,
                                 nn.Dropout(dropout),
                                 nn.Linear(dim_feedforward, d_model),
                                 nn.Dropout(dropout)) 


    def forward(self, x, multipliers = None): 
        x = self.norm_attention(x + self.dropout(self.attention(x, multipliers)))
        x = self.norm_mlp(x + self.mlp(x))
        return x

class Transformer(torch.nn.Module):
    def __init__(self, trans_layer, num_layers):
        super(Transformer, self).__init__()
        self.layers = [copy.deepcopy(trans_layer) for _ in range(num_layers)]
        self.layers = nn.ModuleList(self.layers)
        

    def forward(self, x, multipliers = None):  
        for layer in self.layers:           
            x = layer(x, multipliers) 
        return x