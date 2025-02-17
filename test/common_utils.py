# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import itertools
import torch
import functorch
from functorch import vmap
import torch.utils._pytree as pytree
from functorch_lagging_op_db import functorch_lagging_op_db
from functorch_additional_op_db import additional_op_db
from torch.testing._internal.common_methods_invocations import DecorateInfo
import unittest
import warnings
import re

"""
Usage:

class MyTestCase(TestCase):
    @parameterized('param', {'abs': torch.abs, 'cos': torch.cos})
    def test_single_param(self, param):
        pass

    @parameterized('param1', {'sin': torch.sin, 'tan': torch.tan})
    @parameterized('param2', {'abs': torch.abs, 'cos': torch.cos})
    def test_multiple_param(self, param1, param2):
        pass

# The following creates:
# - MyTestCase.test_single_param_abs
# - MyTestCase.test_single_param_cos
# - MyTestCase.test_multiple_param_abs_sin
# - MyTestCase.test_multiple_param_cos_sin
# - MyTestCase.test_multiple_param_abs_tan
# - MyTestCase.test_multiple_param_cos_tan
instantiate_parameterized_methods(MyTestCase)

# This is also composable with PyTorch testing's instantiate_device_type_tests
# Make sure the param is after the device arg
class MyDeviceSpecificTest(TestCase):
    @parameterized('param', {'abs': torch.abs, 'cos': torch.cos})
    def test_single_param(self, device, param):
        pass

# The following creates:
# - MyDeviceSpecificTestCPU.test_single_param_abs_cpu
# - MyDeviceSpecificTestCPU.test_single_param_cos_cpu
# - MyDeviceSpecificTestCUDA.test_single_param_abs_cuda
# - MyDeviceSpecificTestCUDA.test_single_param_cos_cpu
instantiate_parameterized_methods(MyDeviceSpecificTest)
instantiate_device_type_tests(MyDeviceSpecificTest, globals())

# !!!!! warning !!!!!
# 1. The method being parameterized over MUST NOT HAVE A DOCSTRING. We'll
# error out nicely if this happens.
# 2. All other decorators MUST USE functools.wraps (they must propagate the docstring)
# `@parameterized` works by storing some metadata in place of the docstring.
# This takes advantage of how other decorators work (other decorators usually
# propagate the docstring via functools.wrap).
# 3. We might not compose with PyTorch testing's @dtypes and @precision
# decorators. But that is easily fixable. TODO.
# I think this composes with PyTorch testing's instantiate_device_type_tests.
"""

PARAM_META = '_torch_parameterized_meta'

class ParamMeta():
    def __init__(self):
        self.stack = []

    def push(self, elt):
        self.stack.append(elt)

    def pop(self, elt):
        return self.stack.pop()

def has_param_meta(method):
    param_meta = getattr(method, '__doc__', None)
    return param_meta is not None and isinstance(param_meta, ParamMeta)

def get_param_meta(method):
    param_meta = getattr(method, '__doc__', None)
    if param_meta is None:
        method.__doc__ = ParamMeta()
    if not isinstance(method.__doc__, ParamMeta):
        raise RuntimeError('Tried to use @parameterized on a method that has '
                           'a docstring. This is not supported. Please remove '
                           'the docstring.')
    return method.__doc__

def parameterized(arg_name, case_dict):
    def decorator(fn):
        param_meta = get_param_meta(fn)
        param_meta.push((arg_name, case_dict))
        return fn
    return decorator

def parameterized_with_device(arg_name, case_dict):
    def decorator(fn):
        param_meta = get_param_meta(fn)
        param_meta.push((arg_name, case_dict))
        fn._has_device = True
        return fn
    return decorator


def _set_parameterized_method(test_base, fn, instantiated_cases, extension_name):
    new_name = f'{fn.__name__}_{extension_name}'

    def wrapped_no_device(self, *args, **kwargs):
        for arg_name, case in instantiated_cases:
            kwargs[arg_name] = case
        return fn(self, *args, **kwargs)

    def wrapped_with_device(self, device, *args, **kwargs):
        for arg_name, case in instantiated_cases:
            kwargs[arg_name] = case
        return fn(self, device, *args, **kwargs)

    if getattr(fn, '_has_device', False):
        wrapped = wrapped_with_device
    else:
        wrapped = wrapped_no_device

    wrapped.__name__ = new_name
    setattr(test_base, new_name, wrapped)

def to_tuples(dct):
    return [(k, v) for k, v in dct.items()]

def instantiate_parameterized_methods(test_base):
    allattrs = tuple(dir(test_base))
    for attr_name in allattrs:
        attr = getattr(test_base, attr_name)
        if not has_param_meta(attr):
            continue

        param_meta = get_param_meta(attr)
        arg_names, case_dicts = zip(*param_meta.stack)
        case_dicts = [to_tuples(cd) for cd in case_dicts]
        for list_of_name_and_case in itertools.product(*case_dicts):
            case_names, cases = zip(*list_of_name_and_case)
            extension_name = '_'.join(case_names)
            instantiated_cases = list(zip(arg_names, cases))
            _set_parameterized_method(test_base, attr, instantiated_cases, extension_name)
        # Remove the base fn from the testcase
        delattr(test_base, attr_name)


def loop(op, in_dims, out_dim, batch_size, *batched_args, **kwarg_values):
    outs = []
    for idx in range(batch_size):
        idx_args = []
        idx_kwargs = {}
        for a, in_dim in zip(batched_args, in_dims):
            idx_args.append(a.select(in_dim, idx) if in_dim is not None else a)
        out = op(*idx_args, **kwarg_values)
        outs.append(out)
    loop_out = []
    if isinstance(outs[0], torch.Tensor):
        loop_out = torch.stack(outs)
    else:
        for idx in range(len(outs[0])):
            loop_out.append(torch.stack([i[idx] for i in outs], out_dim))
    return loop_out


def get_exhaustive_batched_inputs(arg_values, kwarg_values, batch_size=3):
    def add_batch_dim(arg, bdim, batch_size=3):
        if isinstance(arg, torch.Tensor):
            shape = [1] * len(arg.shape)
            shape.insert(bdim, batch_size)
            return (arg.repeat(shape), bdim)
        else:
            return (arg, None)

    batch_choices = []
    for a in arg_values:
        if isinstance(a, torch.Tensor):
            batched_val = add_batch_dim(a, 0, batch_size)
            batch_choices.append((batched_val, (a, None)))
        else:
            batch_choices.append(((a, None),))

    for batched_values in itertools.product(*batch_choices):
        batched_args, in_dims = zip(*batched_values)

        if all([i is None for i in in_dims]):
            continue

        yield batched_args, in_dims, kwarg_values


def get_fallback_and_vmap_exhaustive(op, arg_values, kwarg_values, compute_loop_out=True):
    out_dim = 0
    batch_size = 3
    generator = get_exhaustive_batched_inputs(arg_values, kwarg_values, batch_size)
    for batched_args, in_dims, kwarg_values in generator:
        if compute_loop_out:
            loop_out = loop(op, in_dims, out_dim, batch_size, *batched_args, **kwarg_values)
        else:
            loop_out = None
        # Used for debugging the resulting operations
        # from functorch import make_fx
        # def f(a):
        #     return op(a)
        # t = make_fx(vmap(f, in_dims=in_dims, out_dims=out_dim))(*batched_args, **kwarg_values)
        # import pdb; pdb.set_trace()
        batched_out = vmap(op, in_dims=in_dims, out_dims=out_dim)(*batched_args, **kwarg_values)
        yield (loop_out, batched_out)

        # Tests case where we dispatch to a batching rule with no bdims
        # Should now be covered by https://github.com/facebookresearch/functorch/pull/63
        def f(x, *args, **kwargs):
            out = op(*args, **kwargs)
            if isinstance(out, torch.Tensor):
                return out + x.to(out.device)
            out = list(out)
            for idx in range(len(out)):
                out[idx] = out[idx] + x.to(out[idx].device)
            return out

        vmap1_dims = tuple([0] + [None] * len(in_dims))
        vmap2_dims = tuple([None] + list(in_dims))
        if compute_loop_out:
            loop_out = pytree.tree_map(lambda v: torch.ones(3, *v.shape, device=v.device) + v, loop_out)
        else:
            loop_out = None
        batched_out = vmap(vmap(f, in_dims=vmap1_dims), in_dims=vmap2_dims)(torch.ones(3), *batched_args, **kwarg_values)
        yield (loop_out, batched_out)

def opinfo_in_dict(opinfo, d):
    return (opinfo.name in d) or (f'{opinfo.name}.{opinfo.variant_test_name}' in d)

def xfail(op_name, variant_name=None, *, device_type=None, dtypes=None, expected_failure=True):
    return (op_name, variant_name, device_type, dtypes, expected_failure)

def skipOps(test_case_name, base_test_name, to_skip):
    all_opinfos = functorch_lagging_op_db + additional_op_db
    for xfail in to_skip:
        op_name, variant_name, device_type, dtypes, expected_failure = xfail
        if variant_name is None:
            # match all variants
            matching_opinfos = [o for o in all_opinfos if o.name == op_name]
            assert len(matching_opinfos) >= 1, f"Couldn't find OpInfo for {xfail}"
        else:
            matching_opinfos = [o for o in all_opinfos
                                if o.name == op_name and o.variant_test_name == variant_name]
            assert len(matching_opinfos) >= 1, f"Couldn't find OpInfo for {xfail}"
        for opinfo in matching_opinfos:
            decorators = list(opinfo.decorators)
            decorators.append(DecorateInfo(unittest.expectedFailure,
                                           test_case_name, base_test_name,
                                           device_type=device_type, dtypes=dtypes))
            opinfo.decorators = tuple(decorators)

    # This decorator doesn't modify fn in any way
    def wrapped(fn):
        return fn
    return wrapped

class DisableVmapFallback:
    def __enter__(self):
        self.prev_state = functorch._C._is_vmap_fallback_enabled()
        functorch._C._set_vmap_fallback_enabled(False)

    def __exit__(self, *ignored):
        functorch._C._set_vmap_fallback_enabled(self.prev_state)

def check_vmap_fallback(test_case, thunk, opinfo, dry_run=False):
    try:
        with DisableVmapFallback():
            thunk()
    except:
        if not dry_run:
            raise
        if opinfo.variant_test_name:
            print(f"xfail('{opinfo.name}', '{opinfo.variant_test_name}'),")
        else:
            print(f"xfail('{opinfo.name}'),")
