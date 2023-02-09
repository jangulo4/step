# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/source/02_utils.ipynb.

# %% auto 0
__all__ = ['color_order', 'color_dict', 'colors', 'colors_light', 'colors_dark', 'cmap_hist1', 'cmap_hist2', 'cmap_points',
           'fig_size', 'linewidth', 'alpha_grid', 'scatter_size', 'D_units', 'lengths_from_cps', 'split_tensor',
           'get_displacements', 'fit_segments', 'find_change_points', 'get_splits', 'change_points_from_splits',
           'get_split_classes', 'majority_vote', 'abundance', 'post_process_prediction', 'evaluate_cp_prediction',
           'assign_changepoints', 'jaccard_index', 'eval_andi_metrics', 'validate_andi_1', 'validate_andi_3_models',
           'validate_andi_3_alpha']

# %% ../nbs/source/02_utils.ipynb 2
import torch
import numpy as np
import ruptures as rpt
from tqdm.auto import tqdm
from fastcore.all import *
import matplotlib.colors as clr
from fastai.metrics import F1ScoreMulti
from fastai.torch_core import tensor, to_detach
from .data import DATA_PATH, get_andi_valid_dls

# %% ../nbs/source/02_utils.ipynb 4
def lengths_from_cps(cps, length=200):
    "Returns segment lengths determined by `cps` and a total length `length`."
    ex_cps = torch.cat((tensor([0]), cps, tensor([length])))
    return ex_cps[1:] - ex_cps[:-1]

def split_tensor(t, indices):
    "Splits input tensor `t` according to indices in the first dimension."
    idx = [0] + list(indices) + [len(t)]
    return [t[i:j] for i, j in zip(idx[:-1], idx[1:])]

def get_displacements(x):
    "Returns the displacements of trajectory `x` [dim, length]."
    return np.sqrt(np.sum(np.diff(x, axis=1)**2, axis=0))

# %% ../nbs/source/02_utils.ipynb 6
import ruptures as rpt
@delegates(rpt.KernelCPD)
def fit_segments(pred, pen=1., return_cps=False, **kwargs):
    "Fit piecewise constant segments to input signal `pred`."
    alg = rpt.KernelCPD(**kwargs).fit(pred.numpy())
    cps = [0] + alg.predict(pen=pen)
    seg_fit = torch.ones_like(pred)
    for i, j in zip(cps[:-1], cps[1:]):
        seg_fit[i:j] *= pred[i:j].mean()
    if return_cps: return seg_fit, np.array(cps)
    return seg_fit

# %% ../nbs/source/02_utils.ipynb 8
def find_change_points(t): 
    "Finds points in tensor `t` where the value changes."
    return ((t[:-1] - t[1:]) != 0).nonzero(as_tuple=True)[0] + 1

def get_splits(t): 
    "Splits tensor `t` into chunks with the same value."
    cps = find_change_points(t)
    sizes = _find_split_sizes(t, cps)
    return list(t.split(sizes.tolist()))

def _find_split_sizes(t, change_points):
    "Finds sizes of chunks in `t` delimited by `change_points`."
    z, max_len = torch.zeros(1, dtype=int, device=t.device), tensor([len(t)], device=t.device)
    cps_ext = torch.cat((z, change_points, max_len))
    return cps_ext[1:] - cps_ext[:-1]

def change_points_from_splits(splits):
    "Returns change point position from split tensor."
    return torch.cumsum(tensor([len(s) for s in splits[:-1]], device=splits[0].device), dim=0)
        
def get_split_classes(splits): 
    "Returns majority class of each split."
    return [majority_vote(s) for s in splits]

def majority_vote(t):
    "Returns majoritary value from `t`."
    values, counts = t.unique(return_counts=True)
    max_idx = (counts == counts.max()).float().multinomial(1) # break ties randomly
    return values[max_idx]

def abundance(val, t):
    "Abundance of value `val` in tensor `t`."
    vals, counts = t.unique(return_counts=True)
    if val in vals: return counts[vals == val]/counts.sum()
    else: return 0.
    
def post_process_prediction(pred, n_change_points=1):
    "Segmentation prediction post-processing to find change points and classes."
    if len(pred.squeeze().shape) == 2: pred = pred.argmax(-1)
    splits = get_splits(pred)
    none_can_merge = False
    while len(splits) > n_change_points + 1:
        sizes = tensor([len(s) for s in splits])
        idx_merge = (sizes[1:-1].argsort() + 1).tolist()
        none_can_merge = True
        for i in idx_merge:
            if _can_merge(splits, i): 
                splits = _merge_splits(splits, i)
                none_can_merge = False
                break

        if none_can_merge:
            len0 = len(splits)
            splits = _merge_contiguous_values(splits)
            len1 = len(splits)
            if len1 < len0: none_can_merge = False

        if none_can_merge: 
            splits = _merge_edge(splits)
            none_can_merge = False
            
    classes = get_split_classes(splits)
    change_points = change_points_from_splits(splits)
    return change_points, classes, splits

def _merge_left(splits, i):
    "Merges split `i` to the left."
    return [torch.cat(splits[k-1:k+1]) if k == i else splits[k] 
            for k in range(len(splits)) if not k == i - 1]

def _merge_right(splits, i):
    "Merges split `i` to the right."
    return [torch.cat(splits[k:k+2]) if k == i else splits[k] 
            for k in range(len(splits)) if not k == i + 1]

def _merge_left_or_right(splits, i):
    "Merges split `i` towards left or right depending on majority classes."
    left_slice, right_slice = torch.cat(splits[:i]), torch.cat(splits[i+1:])
    classes, counts = splits[i].unique(return_counts=True)
    for c in classes[counts.argsort(descending=True)]:
        abundance_left, abundance_right = abundance(c, left_slice), abundance(c, right_slice)
        if   abundance_left > abundance_right: return _merge_left(splits, i)
        elif abundance_right > abundance_left: return _merge_right(splits, i)

    if i == 1 and len(splits[i]) > len(splits[0]): return _merge_left(splits, i)
    if i == len(splits) - 2 and len(splits[i]) > len(splits[-1]): return _merge_right(splits, i)
    else: return _merge_left(splits, i) if torch.randint(2, (1,)) else _merge_right(splits, i)

def _can_merge(splits, i):
    "Checks whether split `i` is suitable for merging."
    classes = get_split_classes(splits)
    return i == 0 or i == len(splits) - 1 or classes[i-1] == classes[i+1]

def _merge_splits(splits, i):
    "Merges split `i` in `splits` with a contiguous one."
    if   i == 0:               return _merge_right(splits, i)
    elif i == len(splits) - 1: return _merge_left(splits, i)
    else:                      return _merge_left_or_right(splits, i)
    
def _merge_contiguous_values(splits):
    "Merges contiguous splits of the same class."
    classes = get_split_classes(splits)
    max_len = len(splits)
    for e, (c0, c1) in enumerate(zip(classes[-2::-1], classes[:0:-1])):
        if c0 == c1: 
            idx = max_len - e - 1
            splits = _merge_left(splits, idx)
    return splits

def _merge_edge(splits):
    "Merges one of the edge splits."
    left, right, adj_left, adj_right = splits[0], splits[-1], splits[1], splits[-2]
    (vl, cl), (vr, cr) = left.unique(return_counts=True), right.unique(return_counts=True)
    idx_r = len(splits) - 1
    sim_left  = [abundance(v, adj_left)*abundance(v, left) for v, c in zip(vl, cl)]
    sim_right = [abundance(v, adj_right)*abundance(v, right) for v, c in zip(vr, cr)]
    sim_left, sim_right = torch.mean(tensor(sim_left)), torch.mean(tensor(sim_right))

    if   sim_left > sim_right: return _merge_right(splits, 0)
    elif sim_right > sim_left: return _merge_left(splits, idx_r)
        
    if    len(left) < len(adj_left) and len(right) > len(adj_right):
        return _merge_right(splits, 0)
    elif  len(left) > len(adj_left) and len(right) < len(adj_right):
        return _merge_left(splits, idx_r)
    elif  len(left) < len(right):
        return _merge_right(splits, 0)
    elif  len(left) > len(right):
        return _merge_left(splits, idx_r)
    else:
        return _merge_left(splits, idx_r) if torch.randint(2, (1,)) else _merge_right(splits, 0)

# %% ../nbs/source/02_utils.ipynb 11
def evaluate_cp_prediction(true, pred, changepoint_threshold=5):
    "Evaluates the change point prediction."
    true_positive = 0
    false_positive = max(len(pred) - len(true), 0)
    false_negative = max(len(true) - len(pred), 0)
    squared_error = []
    
    assignment = assign_changepoints(true, pred)
    for idx in assignment:
        difference = np.abs(true[idx[0]] - pred[idx[1]])
        if difference < changepoint_threshold:
            true_positive += 1
            squared_error.append(difference**2)
        else:
            false_positive += 1
            false_negative += 1
            
    return {'squared_error': squared_error, 
            'tp': true_positive, 
            'fp': false_positive, 
            'fn': false_negative}

def assign_changepoints(true, pred):
    "Matches predicted and true changepoints solving a linear sum assignment problem."
    from scipy.optimize import linear_sum_assignment
    cost = np.zeros((len(true), len(pred)))
    for i, t in enumerate(true):
        cost[i, :] = np.abs(t-pred)
    return np.array(linear_sum_assignment(cost)).T

def jaccard_index(true_positive, false_positive, false_negative):
    "Computes the Jaccard index a.k.a. Tanimoto index."
    return true_positive/(true_positive + false_positive + false_negative)

# %% ../nbs/source/02_utils.ipynb 13
def eval_andi_metrics(dls, model):
    "Evaluates model in validation set in order to obtain AnDi challenge metrics."
    f1_score = F1ScoreMulti(average='micro')
    cps_pred, cls0_pred, cls1_pred = [], [], []
    cps_true, cls0_true, cls1_true = [], [], []
    for x, y in dls.valid:
        pred = model.activation(model(x)).detach()
        for p, true in zip(pred, y):
            cp_p, cls_p, _ = post_process_prediction(p)
            cp_t, cls_t, _ = post_process_prediction(true)
            cps_true.append(cp_t[0].item()) 
            cls0_true.append(cls_t[0].item()); cls1_true.append(cls_t[1].item())
            if len(cls_p) < 2: 
                cls0_pred.append(cls_p[0].item())
                cls1_pred.append(cls_p[0].item())
                cps_pred.append(0)
            else:
                cls0_pred.append(cls_p[0].item())
                cls1_pred.append(cls_p[1].item())
                cps_pred.append(cp_p[0].item())

    cps_pred, cps_true = tensor(cps_pred), tensor(cps_true)
    full_preds = torch.cat((tensor(cls0_pred), tensor(cls1_pred)), axis=0)
    full_true = torch.cat((tensor(cls0_true), tensor(cls1_true)), axis=0)
    
    rmse = (cps_pred - cps_true).pow(2).float().mean().sqrt()
    f1 = f1_score(full_preds, full_true)
    return rmse, f1

@delegates(get_andi_valid_dls)
def validate_andi_1(m, dim=1, bs=1, **kwargs):
    "Validates model on the AnDi test set for task 1 (anomalous exponent)."
    pred_path = DATA_PATH/"task1"
    dls = get_andi_valid_dls(dim=dim, task=1, bs=1, **kwargs)
    dls.device = next(m.parameters()).device
    preds = [to_detach(m.activation(m(x))) for x,_ in tqdm(dls.valid)]
    with open(pred_path.with_suffix('.txt'), 'w') as f:
        for p in preds:
            alpha = p.mean().item()
            #dim; alpha
            f.write(f'{int(dim)}; {alpha}\n')

@delegates(get_andi_valid_dls)
def validate_andi_3_models(m, dim=1, task=3, **kwargs):
    "Validates model on the AnDi test set for task 3 (segmentation) predicting diffusion models."
    pred_path = DATA_PATH/"task3"
    dls = get_andi_valid_dls(dim=dim, task=3, **kwargs)
    dls.device = next(m.parameters()).device
    preds = torch.cat([to_detach(m.activation(m(x))) for x,_ in tqdm(dls.valid)])
    with open(pred_path.with_suffix('.txt'), 'w') as f:
        for p in preds:
            cp, classes, _ = post_process_prediction(p)
            if len(classes) < 2: 
                cp = tensor(100)
                classes.append(classes[0])
            #dim; cp; model_0; alpha_0; model_1; alpha_1
            f.write(f'{int(dim)}; {cp.item()}; {classes[0].item()}; 0.; {classes[1].item()}; 0.\n')
            
@delegates(get_andi_valid_dls)
def validate_andi_3_alpha(m, dim=1, task=3, **kwargs):
    "Validates model on the AnDi test set for task 3 (segmentation) predicting anomalous exponents."
    pred_path = DATA_PATH/"task3"
    dls = get_andi_valid_dls(dim=dim, task=3, **kwargs)
    dls.device = next(m.parameters()).device
    preds = torch.cat([to_detach(m.activation(m(x))) for x,_ in tqdm(dls.valid)])
    with open(pred_path.with_suffix('.txt'), 'w') as f:
        for p in preds:
            cp = rpt.KernelCPD(min_size=5).fit(p.numpy()).predict(n_bkps=1)[0]
            alpha_0, alpha_1 = p[:cp].mean(), p[cp:].mean()
            #dim; cp; model_0; alpha_0; model_1; alpha_1
            f.write(f'{int(dim)}; {cp}; 0; {alpha_0}; 0; {alpha_1}\n')

# %% ../nbs/source/02_utils.ipynb 15
color_order = ['blue', 'orange', 'yellow', 'purple', 'green']
color_dict = {
    'blue':   {'dark': (0.2745098, 0.4, 0.6),
               'medium': (0.39607843, 0.5254902, 0.71764706),
               'light': (0.65098039, 0.79215686, 0.94117647)},
    'orange': {'dark': (0.71764706, 0.36470588, 0.24313725),
               'medium': (0.88627451, 0.4627451, 0.34901961),
               'light': (1.0, 0.63921569, 0.44705882)},
    'yellow': {'dark': (0.85882353, 0.58431373, 0.18039216),
               'medium': (0.89803922, 0.68235294, 0.39607843),
               'light': (0.96470588, 0.84705882, 0.52941176)},
    'purple': {'dark': (0.6627451, 0.16078431, 0.30980392),
               'medium': (0.7372549, 0.39607843, 0.55294118),
               'light': (0.89019608, 0.38823529, 0.52941176)},
    'green':  {'dark': (0.22352941, 0.46666667, 0.4549019607843137),
               'medium': (0.29803922, 0.60784314, 0.58431373),
               'light': (0.50980392, 0.76862745, 0.76470588)}
}

colors = [color_dict[k]['medium'] for k in color_order]
colors_light = [color_dict[k]['light'] for k in color_order]
colors_dark = [color_dict[k]['dark'] for k in color_order]

cmap_hist1 = clr.LinearSegmentedColormap.from_list(
    'custom cm', ['w', 
                  color_dict['blue']['light'],
                  color_dict['blue']['dark']],
                  N=256
)
cmap_hist2 = clr.LinearSegmentedColormap.from_list(
    'custom cm', ['w', 
                  color_dict['orange']['light'],
                  color_dict['orange']['dark']],
                  N=256
)
cmap_points = clr.LinearSegmentedColormap.from_list(
    'custom cm', [color_dict['yellow']['light'], 
                  color_dict['purple']['light'],
                  color_dict['blue']['medium']],
                  N=256
)

fig_size = 4
linewidth = 2
alpha_grid = 0.2
scatter_size = 12

D_units = "($\mu$m$^2$/s)"
