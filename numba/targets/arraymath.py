"""
Implementation of math operations on Array objects.
"""

from __future__ import print_function, absolute_import, division

import math
from collections import namedtuple
from enum import IntEnum
from functools import partial

import numpy as np

from llvmlite import ir
import llvmlite.llvmpy.core as lc
from llvmlite.llvmpy.core import Constant, Type

from numba import types, cgutils, typing, generated_jit
from numba.extending import overload, overload_method, register_jitable
from numba.numpy_support import as_dtype, type_can_asarray
from numba.numpy_support import version as numpy_version
from numba.targets.imputils import (lower_builtin, impl_ret_borrowed,
                                    impl_ret_new_ref, impl_ret_untracked)
from numba.typing import signature
from .arrayobj import make_array, load_item, store_item, _empty_nd_impl
from .linalg import ensure_blas

from numba.extending import intrinsic
from numba.errors import RequireLiteralValue, TypingError

def _check_blas():
    # Checks if a BLAS is available so e.g. dot will work
    try:
        ensure_blas()
    except ImportError:
        return False
    return True

_HAVE_BLAS = _check_blas()

@intrinsic
def _create_tuple_result_shape(tyctx, shape_list, shape_tuple):
    """
    This routine converts shape list where the axis dimension has already
    been popped to a tuple for indexing of the same size.  The original shape
    tuple is also required because it contains a length field at compile time
    whereas the shape list does not.
    """

    # The new tuple's size is one less than the original tuple since axis
    # dimension removed.
    nd = len(shape_tuple) - 1
    # The return type of this intrinsic is an int tuple of length nd.
    tupty = types.UniTuple(types.intp, nd)
    # The function signature for this intrinsic.
    function_sig = tupty(shape_list, shape_tuple)

    def codegen(cgctx, builder, signature, args):
        lltupty = cgctx.get_value_type(tupty)
        # Create an empty int tuple.
        tup = cgutils.get_null_value(lltupty)

        # Get the shape list from the args and we don't need shape tuple.
        [in_shape, _] = args

        def array_indexer(a, i):
            return a[i]

        # loop to fill the tuple
        for i in range(nd):
            dataidx = cgctx.get_constant(types.intp, i)
            # compile and call array_indexer
            data = cgctx.compile_internal(builder, array_indexer,
                                          types.intp(shape_list, types.intp),
                                          [in_shape, dataidx])
            tup = builder.insert_value(tup, data, i)
        return tup

    return function_sig, codegen

@intrinsic
def _gen_index_tuple(tyctx, shape_tuple, value, axis):
    """
    Generates a tuple that can be used to index a specific slice from an
    array for sum with axis.  shape_tuple is the size of the dimensions of
    the input array.  'value' is the value to put in the indexing tuple
    in the axis dimension and 'axis' is that dimension.  For this to work,
    axis has to be a const.
    """
    if not isinstance(axis, types.Literal):
        raise RequireLiteralValue('axis argument must be a constant')
    # Get the value of the axis constant.
    axis_value = axis.literal_value
    # The length of the indexing tuple to be output.
    nd = len(shape_tuple)

    # If the axis value is impossible for the given size array then
    # just fake it like it was for axis 0.  This will stop compile errors
    # when it looks like it could be called from array_sum_axis but really
    # can't because that routine checks the axis mismatch and raise an
    # exception.
    if axis_value >= nd:
        axis_value = 0

    # Calculate the type of the indexing tuple.  All the non-axis
    # dimensions have slice2 type and the axis dimension has int type.
    before = axis_value
    after  = nd - before - 1
    types_list = ([types.slice2_type] * before) +  \
                  [types.intp] +                   \
                 ([types.slice2_type] * after)

    # Creates the output type of the function.
    tupty = types.Tuple(types_list)
    # Defines the signature of the intrinsic.
    function_sig = tupty(shape_tuple, value, axis)

    def codegen(cgctx, builder, signature, args):
        lltupty = cgctx.get_value_type(tupty)
        # Create an empty indexing tuple.
        tup = cgutils.get_null_value(lltupty)

        # We only need value of the axis dimension here.
        # The rest are constants defined above.
        [_, value_arg, _] = args

        def create_full_slice():
            return slice(None, None)

        # loop to fill the tuple with slice(None,None) before
        # the axis dimension.

        # compile and call create_full_slice
        slice_data = cgctx.compile_internal(builder, create_full_slice,
                                            types.slice2_type(),
                                            [])
        for i in range(0, axis_value):
            tup = builder.insert_value(tup, slice_data, i)

        # Add the axis dimension 'value'.
        tup = builder.insert_value(tup, value_arg, axis_value)

        # loop to fill the tuple with slice(None,None) after
        # the axis dimension.
        for i in range(axis_value + 1, nd):
            tup = builder.insert_value(tup, slice_data, i)
        return tup

    return function_sig, codegen


#----------------------------------------------------------------------------
# Basic stats and aggregates

@lower_builtin(np.sum, types.Array)
@lower_builtin("array.sum", types.Array)
def array_sum(context, builder, sig, args):
    zero = sig.return_type(0)

    def array_sum_impl(arr):
        c = zero
        for v in np.nditer(arr):
            c += v.item()
        return c

    res = context.compile_internal(builder, array_sum_impl, sig, args,
                                    locals=dict(c=sig.return_type))
    return impl_ret_borrowed(context, builder, sig.return_type, res)

@register_jitable
def _array_sum_axis_nop(arr, v):
    return arr

@lower_builtin(np.sum, types.Array, types.intp)
@lower_builtin(np.sum, types.Array, types.IntegerLiteral)
@lower_builtin("array.sum", types.Array, types.intp)
@lower_builtin("array.sum", types.Array, types.IntegerLiteral)
def array_sum_axis(context, builder, sig, args):
    """
    The third parameter to gen_index_tuple that generates the indexing
    tuples has to be a const so we can't just pass "axis" through since
    that isn't const.  We can check for specific values and have
    different instances that do take consts.  Supporting axis summation
    only up to the fourth dimension for now.
    """
    # typing/arraydecl.py:sum_expand defines the return type for sum with axis.
    # It is one dimension less than the input array.

    retty = sig.return_type
    zero = getattr(retty, 'dtype', retty)(0)
    # if the return is scalar in type then "take" the 0th element of the
    # 0d array accumulator as the return value
    if getattr(retty, 'ndim', None) is None:
        op = np.take
    else:
        op = _array_sum_axis_nop
    [ty_array, ty_axis] = sig.args
    is_axis_const = False
    const_axis_val = 0
    if isinstance(ty_axis, types.Literal):
        # this special-cases for constant axis
        const_axis_val = ty_axis.literal_value
        # fix negative axis
        if const_axis_val < 0:
            const_axis_val = ty_array.ndim + const_axis_val
        if const_axis_val < 0 or const_axis_val > ty_array.ndim:
            raise ValueError("'axis' entry is out of bounds")

        ty_axis = context.typing_context.resolve_value_type(const_axis_val)
        axis_val = context.get_constant(ty_axis, const_axis_val)
        # rewrite arguments
        args = args[0], axis_val
        # rewrite sig
        sig = sig.replace(args=[ty_array, ty_axis])
        is_axis_const = True

    def array_sum_impl_axis(arr, axis):
        ndim = arr.ndim

        if not is_axis_const:
            # Catch where axis is negative or greater than 3.
            if axis < 0 or axis > 3:
                raise ValueError("Numba does not support sum with axis "
                                 "parameter outside the range 0 to 3.")

        # Catch the case where the user misspecifies the axis to be
        # more than the number of the array's dimensions.
        if axis >= ndim:
            raise ValueError("axis is out of bounds for array")

        # Convert the shape of the input array to a list.
        ashape = list(arr.shape)
        # Get the length of the axis dimension.
        axis_len = ashape[axis]
        # Remove the axis dimension from the list of dimensional lengths.
        ashape.pop(axis)
        # Convert this shape list back to a tuple using above intrinsic.
        ashape_without_axis = _create_tuple_result_shape(ashape, arr.shape)
        # Tuple needed here to create output array with correct size.
        result = np.full(ashape_without_axis, zero, type(zero))

        # Iterate through the axis dimension.
        for axis_index in range(axis_len):
            if is_axis_const:
                # constant specialized version works for any valid axis value
                index_tuple_generic = _gen_index_tuple(arr.shape, axis_index,
                                                       const_axis_val)
                result += arr[index_tuple_generic]
            else:
                # Generate a tuple used to index the input array.
                # The tuple is ":" in all dimensions except the axis
                # dimension where it is "axis_index".
                if axis == 0:
                    index_tuple1 = _gen_index_tuple(arr.shape, axis_index, 0)
                    result += arr[index_tuple1]
                elif axis == 1:
                    index_tuple2 = _gen_index_tuple(arr.shape, axis_index, 1)
                    result += arr[index_tuple2]
                elif axis == 2:
                    index_tuple3 = _gen_index_tuple(arr.shape, axis_index, 2)
                    result += arr[index_tuple3]
                elif axis == 3:
                    index_tuple4 = _gen_index_tuple(arr.shape, axis_index, 3)
                    result += arr[index_tuple4]

        return op(result, 0)

    res = context.compile_internal(builder, array_sum_impl_axis, sig, args)
    return impl_ret_new_ref(context, builder, sig.return_type, res)

@lower_builtin(np.prod, types.Array)
@lower_builtin("array.prod", types.Array)
def array_prod(context, builder, sig, args):

    def array_prod_impl(arr):
        c = 1
        for v in np.nditer(arr):
            c *= v.item()
        return c

    res = context.compile_internal(builder, array_prod_impl, sig, args,
                                    locals=dict(c=sig.return_type))
    return impl_ret_borrowed(context, builder, sig.return_type, res)

@lower_builtin(np.cumsum, types.Array)
@lower_builtin("array.cumsum", types.Array)
def array_cumsum(context, builder, sig, args):
    scalar_dtype = sig.return_type.dtype
    dtype = as_dtype(scalar_dtype)
    zero = scalar_dtype(0)

    def array_cumsum_impl(arr):
        out = np.empty(arr.size, dtype)
        c = zero
        for idx, v in enumerate(arr.flat):
            c += v
            out[idx] = c
        return out

    res = context.compile_internal(builder, array_cumsum_impl, sig, args,
                                   locals=dict(c=scalar_dtype))
    return impl_ret_new_ref(context, builder, sig.return_type, res)

@lower_builtin(np.cumprod, types.Array)
@lower_builtin("array.cumprod", types.Array)
def array_cumprod(context, builder, sig, args):
    scalar_dtype = sig.return_type.dtype
    dtype = as_dtype(scalar_dtype)

    def array_cumprod_impl(arr):
        out = np.empty(arr.size, dtype)
        c = 1
        for idx, v in enumerate(arr.flat):
            c *= v
            out[idx] = c
        return out

    res = context.compile_internal(builder, array_cumprod_impl, sig, args,
                                   locals=dict(c=scalar_dtype))
    return impl_ret_new_ref(context, builder, sig.return_type, res)

@lower_builtin(np.mean, types.Array)
@lower_builtin("array.mean", types.Array)
def array_mean(context, builder, sig, args):
    zero = sig.return_type(0)

    def array_mean_impl(arr):
        # Can't use the naive `arr.sum() / arr.size`, as it would return
        # a wrong result on integer sum overflow.
        c = zero
        for v in np.nditer(arr):
            c += v.item()
        return c / arr.size

    res = context.compile_internal(builder, array_mean_impl, sig, args,
                                   locals=dict(c=sig.return_type))
    return impl_ret_untracked(context, builder, sig.return_type, res)

@lower_builtin(np.var, types.Array)
@lower_builtin("array.var", types.Array)
def array_var(context, builder, sig, args):
    def array_var_impl(arr):
        # Compute the mean
        m = arr.mean()

        # Compute the sum of square diffs
        ssd = 0
        for v in np.nditer(arr):
            val = (v.item() - m)
            ssd +=  np.real(val * np.conj(val))
        return ssd / arr.size

    res = context.compile_internal(builder, array_var_impl, sig, args)
    return impl_ret_untracked(context, builder, sig.return_type, res)


@lower_builtin(np.std, types.Array)
@lower_builtin("array.std", types.Array)
def array_std(context, builder, sig, args):
    def array_std_impl(arry):
        return arry.var() ** 0.5
    res = context.compile_internal(builder, array_std_impl, sig, args)
    return impl_ret_untracked(context, builder, sig.return_type, res)


def zero_dim_msg(fn_name):
    msg = ("zero-size array to reduction operation "
           "{0} which has no identity".format(fn_name))
    return msg


@lower_builtin(np.min, types.Array)
@lower_builtin("array.min", types.Array)
def array_min(context, builder, sig, args):
    ty = sig.args[0].dtype
    MSG = zero_dim_msg('minimum')

    if isinstance(ty, (types.NPDatetime, types.NPTimedelta)):
        # NaT is smaller than every other value, but it is
        # ignored as far as min() is concerned.
        nat = ty('NaT')

        def array_min_impl(arry):
            if arry.size == 0:
                raise ValueError(MSG)

            min_value = nat
            it = np.nditer(arry)
            for view in it:
                v = view.item()
                if v != nat:
                    min_value = v
                    break

            for view in it:
                v = view.item()
                if v != nat and v < min_value:
                    min_value = v
            return min_value

    elif isinstance(ty, types.Complex):
        def array_min_impl(arry):
            if arry.size == 0:
                raise ValueError(MSG)

            it = np.nditer(arry)
            min_value = next(it).take(0)

            for view in it:
                v = view.item()
                if v.real < min_value.real:
                    min_value = v
                elif v.real == min_value.real:
                    if v.imag < min_value.imag:
                        min_value = v
            return min_value

    else:
        def array_min_impl(arry):
            if arry.size == 0:
                raise ValueError(MSG)

            it = np.nditer(arry)
            min_value = next(it).take(0)

            for view in it:
                v = view.item()
                if v < min_value:
                    min_value = v
            return min_value

    res = context.compile_internal(builder, array_min_impl, sig, args)
    return impl_ret_borrowed(context, builder, sig.return_type, res)


@lower_builtin(np.max, types.Array)
@lower_builtin("array.max", types.Array)
def array_max(context, builder, sig, args):
    ty = sig.args[0].dtype
    MSG = zero_dim_msg('maximum')

    if isinstance(ty, types.Complex):
        def array_max_impl(arry):
            if arry.size == 0:
                raise ValueError(MSG)

            it = np.nditer(arry)
            max_value = next(it).take(0)

            for view in it:
                v = view.item()
                if v.real > max_value.real:
                    max_value = v
                elif v.real == max_value.real:
                    if v.imag > max_value.imag:
                        max_value = v
            return max_value
    else:
        def array_max_impl(arry):
            if arry.size == 0:
                raise ValueError(MSG)

            it = np.nditer(arry)
            max_value = next(it).take(0)

            for view in it:
                v = view.item()
                if v > max_value:
                    max_value = v
            return max_value

    res = context.compile_internal(builder, array_max_impl, sig, args)
    return impl_ret_borrowed(context, builder, sig.return_type, res)


@lower_builtin(np.argmin, types.Array)
@lower_builtin("array.argmin", types.Array)
def array_argmin(context, builder, sig, args):
    ty = sig.args[0].dtype
    # NOTE: Under Numpy < 1.10, argmin() is inconsistent with min() on NaT values:
    # https://github.com/numpy/numpy/issues/6030

    if (numpy_version >= (1, 10) and
        isinstance(ty, (types.NPDatetime, types.NPTimedelta))):
        # NaT is smaller than every other value, but it is
        # ignored as far as argmin() is concerned.
        nat = ty('NaT')

        def array_argmin_impl(arry):
            if arry.size == 0:
                raise ValueError("attempt to get argmin of an empty sequence")
            min_value = nat
            min_idx = 0
            it = arry.flat
            idx = 0
            for v in it:
                if v != nat:
                    min_value = v
                    min_idx = idx
                    idx += 1
                    break
                idx += 1

            for v in it:
                if v != nat and v < min_value:
                    min_value = v
                    min_idx = idx
                idx += 1
            return min_idx

    else:
        def array_argmin_impl(arry):
            if arry.size == 0:
                raise ValueError("attempt to get argmin of an empty sequence")
            for v in arry.flat:
                min_value = v
                min_idx = 0
                break

            idx = 0
            for v in arry.flat:
                if v < min_value:
                    min_value = v
                    min_idx = idx
                idx += 1
            return min_idx
    res = context.compile_internal(builder, array_argmin_impl, sig, args)
    return impl_ret_untracked(context, builder, sig.return_type, res)


@lower_builtin(np.argmax, types.Array)
@lower_builtin("array.argmax", types.Array)
def array_argmax(context, builder, sig, args):
    def array_argmax_impl(arry):
        if arry.size == 0:
            raise ValueError("attempt to get argmax of an empty sequence")
        for v in arry.flat:
            max_value = v
            max_idx = 0
            break

        idx = 0
        for v in arry.flat:
            if v > max_value:
                max_value = v
                max_idx = idx
            idx += 1
        return max_idx
    res = context.compile_internal(builder, array_argmax_impl, sig, args)
    return impl_ret_untracked(context, builder, sig.return_type, res)


@overload(np.all)
@overload_method(types.Array, "all")
def np_all(a):
    def flat_all(a):
        for v in np.nditer(a):
            if not v.item():
                return False
        return True

    return flat_all

@overload(np.any)
@overload_method(types.Array, "any")
def np_any(a):
    def flat_any(a):
        for v in np.nditer(a):
            if v.item():
                return True
        return False

    return flat_any


def get_isnan(dtype):
    """
    A generic isnan() function
    """
    if isinstance(dtype, (types.Float, types.Complex)):
        return np.isnan
    else:
        @register_jitable
        def _trivial_isnan(x):
            return False
        return _trivial_isnan

@register_jitable
def less_than(a, b):
    return a < b

@register_jitable
def greater_than(a, b):
    return a > b

@register_jitable
def check_array(a):
    if a.size == 0:
        raise ValueError('zero-size array to reduction operation not possible')

def nan_min_max_factory(comparison_op, is_complex_dtype):

    if is_complex_dtype:
        def impl(a):
            arr = np.asarray(a)
            check_array(arr)
            it = np.nditer(arr)
            return_val = next(it).take(0)
            for view in it:
                v = view.item()
                if np.isnan(return_val.real) and not np.isnan(v.real):
                    return_val = v
                else:
                    if comparison_op(v.real, return_val.real):
                        return_val = v
                    elif v.real == return_val.real:
                        if comparison_op(v.imag, return_val.imag):
                            return_val = v
            return return_val
    else:
        def impl(a):
            arr = np.asarray(a)
            check_array(arr)
            it = np.nditer(arr)
            return_val = next(it).take(0)
            for view in it:
                v = view.item()
                if not np.isnan(v):
                    if not comparison_op(return_val, v):
                        return_val = v
            return return_val

    return impl

real_nanmin = register_jitable(
    nan_min_max_factory(less_than, is_complex_dtype=False)
)
real_nanmax = register_jitable(
    nan_min_max_factory(greater_than, is_complex_dtype=False)
)
complex_nanmin = register_jitable(
    nan_min_max_factory(less_than, is_complex_dtype=True)
)
complex_nanmax = register_jitable(
    nan_min_max_factory(greater_than, is_complex_dtype=True)
)

@overload(np.nanmin)
def np_nanmin(a):
    dt = determine_dtype(a)
    if np.issubdtype(dt, np.complexfloating):
        return complex_nanmin
    else:
        return real_nanmin

@overload(np.nanmax)
def np_nanmax(a):
    dt = determine_dtype(a)
    if np.issubdtype(dt, np.complexfloating):
        return complex_nanmax
    else:
        return real_nanmax

if numpy_version >= (1, 8):
    @overload(np.nanmean)
    def np_nanmean(a):
        if not isinstance(a, types.Array):
            return
        isnan = get_isnan(a.dtype)

        def nanmean_impl(a):
            c = 0.0
            count = 0
            for view in np.nditer(a):
                v = view.item()
                if not isnan(v):
                    c += v.item()
                    count += 1
            # np.divide() doesn't raise ZeroDivisionError
            return np.divide(c, count)

        return nanmean_impl

    @overload(np.nanvar)
    def np_nanvar(a):
        if not isinstance(a, types.Array):
            return
        isnan = get_isnan(a.dtype)

        def nanvar_impl(a):
            # Compute the mean
            m = np.nanmean(a)

            # Compute the sum of square diffs
            ssd = 0.0
            count = 0
            for view in np.nditer(a):
                v = view.item()
                if not isnan(v):
                    val = (v.item() - m)
                    ssd +=  np.real(val * np.conj(val))
                    count += 1
            # np.divide() doesn't raise ZeroDivisionError
            return np.divide(ssd, count)

        return nanvar_impl

    @overload(np.nanstd)
    def np_nanstd(a):
        if not isinstance(a, types.Array):
            return

        def nanstd_impl(a):
            return np.nanvar(a) ** 0.5

        return nanstd_impl

@overload(np.nansum)
def np_nansum(a):
    if not isinstance(a, types.Array):
        return
    if isinstance(a.dtype, types.Integer):
        retty = types.intp
    else:
        retty = a.dtype
    zero = retty(0)
    isnan = get_isnan(a.dtype)

    def nansum_impl(a):
        c = zero
        for view in np.nditer(a):
            v = view.item()
            if not isnan(v):
                c += v
        return c

    return nansum_impl

if numpy_version >= (1, 10):
    @overload(np.nanprod)
    def np_nanprod(a):
        if not isinstance(a, types.Array):
            return
        if isinstance(a.dtype, types.Integer):
            retty = types.intp
        else:
            retty = a.dtype
        one = retty(1)
        isnan = get_isnan(a.dtype)

        def nanprod_impl(a):
            c = one
            for view in np.nditer(a):
                v = view.item()
                if not isnan(v):
                    c *= v
            return c

        return nanprod_impl

if numpy_version >= (1, 12):
    @overload(np.nancumprod)
    def np_nancumprod(a):
        if not isinstance(a, types.Array):
            return

        if isinstance(a.dtype, (types.Boolean, types.Integer)):
            # dtype cannot possibly contain NaN
            return lambda a: np.cumprod(a)
        else:
            retty = a.dtype
            is_nan = get_isnan(retty)
            one = retty(1)

            def nancumprod_impl(a):
                out = np.empty(a.size, retty)
                c = one
                for idx, v in enumerate(a.flat):
                    if ~is_nan(v):
                        c *= v
                    out[idx] = c
                return out

            return nancumprod_impl

    @overload(np.nancumsum)
    def np_nancumsum(a):
        if not isinstance(a, types.Array):
            return

        if isinstance(a.dtype, (types.Boolean, types.Integer)):
            # dtype cannot possibly contain NaN
            return lambda a: np.cumsum(a)
        else:
            retty = a.dtype
            is_nan = get_isnan(retty)
            zero = retty(0)

            def nancumsum_impl(a):
                out = np.empty(a.size, retty)
                c = zero
                for idx, v in enumerate(a.flat):
                    if ~is_nan(v):
                        c += v
                    out[idx] = c
                return out

            return nancumsum_impl

@register_jitable
def prepare_ptp_input(a):
    arr = _asarray(a)
    if len(arr) == 0:
        raise ValueError('zero-size array reduction not possible')
    else:
        return arr

def _compute_current_val_impl_gen(op):
    def _compute_current_val_impl(current_val, val):
        if isinstance(current_val, types.Complex):
            # The sort order for complex numbers is lexicographic. If both the
            # real and imaginary parts are non-nan then the order is determined
            # by the real parts except when they are equal, in which case the
            # order is determined by the imaginary parts.
            # https://github.com/numpy/numpy/blob/577a86e/numpy/core/fromnumeric.py#L874-L877
            def impl(current_val, val):
                if op(val.real, current_val.real):
                    return val
                elif (val.real == current_val.real
                    and op(val.imag, current_val.imag)):
                    return val
                return current_val
        else:
            def impl(current_val, val):
                return val if op(val, current_val) else current_val
        return impl
    return _compute_current_val_impl

_compute_a_max = generated_jit(_compute_current_val_impl_gen(greater_than))
_compute_a_min = generated_jit(_compute_current_val_impl_gen(less_than))

@generated_jit
def _early_return(val):
    UNUSED = 0
    if isinstance(val, types.Complex):
        def impl(val):
            if np.isnan(val.real):
                if np.isnan(val.imag):
                    return True, np.nan + np.nan * 1j
                else:
                    return True, np.nan + 0j
            else:
                return False, UNUSED
    elif isinstance(val, types.Float):
        def impl(val):
            if np.isnan(val):
                return True, np.nan
            else:
                return False, UNUSED
    else:
        def impl(val):
            return False, UNUSED
    return impl

@overload(np.ptp)
def np_ptp(a):

    if hasattr(a, 'dtype'):
        if isinstance(a.dtype, types.Boolean):
            raise TypingError("Boolean dtype is unsupported (as per NumPy)")
            # Numpy raises a TypeError

    def np_ptp_impl(a):
        arr = prepare_ptp_input(a)

        a_flat = arr.flat
        a_min = a_flat[0]
        a_max = a_flat[0]

        for i in range(arr.size):
            val = a_flat[i]
            take_branch, retval = _early_return(val)
            if take_branch:
                return retval
            a_max = _compute_a_max(a_max, val)
            a_min = _compute_a_min(a_min, val)

        return a_max - a_min

    return np_ptp_impl

#----------------------------------------------------------------------------
# Median and partitioning

@register_jitable
def nan_aware_less_than(a, b):
    if np.isnan(a):
        return False
    else:
        if np.isnan(b):
            return True
        else:
            return a < b

def _partition_factory(pivotimpl):
    def _partition(A, low, high):
        mid = (low + high) >> 1
        # NOTE: the pattern of swaps below for the pivot choice and the
        # partitioning gives good results (i.e. regular O(n log n))
        # on sorted, reverse-sorted, and uniform arrays.  Subtle changes
        # risk breaking this property.

        # Use median of three {low, middle, high} as the pivot
        if pivotimpl(A[mid], A[low]):
            A[low], A[mid] = A[mid], A[low]
        if pivotimpl(A[high], A[mid]):
            A[high], A[mid] = A[mid], A[high]
        if pivotimpl(A[mid], A[low]):
            A[low], A[mid] = A[mid], A[low]
        pivot = A[mid]

        A[high], A[mid] = A[mid], A[high]
        i = low
        j = high - 1
        while True:
            while i < high and pivotimpl(A[i], pivot):
                i += 1
            while j >= low and pivotimpl(pivot, A[j]):
                j -= 1
            if i >= j:
                break
            A[i], A[j] = A[j], A[i]
            i += 1
            j -= 1
        # Put the pivot back in its final place (all items before `i`
        # are smaller than the pivot, all items at/after `i` are larger)
        A[i], A[high] = A[high], A[i]
        return i
    return _partition

_partition = register_jitable(_partition_factory(less_than))
_partition_w_nan = register_jitable(_partition_factory(nan_aware_less_than))

def _select_factory(partitionimpl):
    def _select(arry, k, low, high):
        """
        Select the k'th smallest element in array[low:high + 1].
        """
        i = partitionimpl(arry, low, high)
        while i != k:
            if i < k:
                low = i + 1
                i = partitionimpl(arry, low, high)
            else:
                high = i - 1
                i = partitionimpl(arry, low, high)
        return arry[k]
    return _select

_select = register_jitable(_select_factory(_partition))
_select_w_nan = register_jitable(_select_factory(_partition_w_nan))

@register_jitable
def _select_two(arry, k, low, high):
    """
    Select the k'th and k+1'th smallest elements in array[low:high + 1].

    This is significantly faster than doing two independent selections
    for k and k+1.
    """
    while True:
        assert high > low  # by construction
        i = _partition(arry, low, high)
        if i < k:
            low = i + 1
        elif i > k + 1:
            high = i - 1
        elif i == k:
            _select(arry, k + 1, i + 1, high)
            break
        else:  # i == k + 1
            _select(arry, k, low, i - 1)
            break

    return arry[k], arry[k + 1]

@register_jitable
def _median_inner(temp_arry, n):
    """
    The main logic of the median() call.  *temp_arry* must be disposable,
    as this function will mutate it.
    """
    low = 0
    high = n - 1
    half = n >> 1
    if n & 1 == 0:
        a, b = _select_two(temp_arry, half - 1, low, high)
        return (a + b) / 2
    else:
        return _select(temp_arry, half, low, high)

@overload(np.median)
def np_median(a):
    if not isinstance(a, types.Array):
        return

    def median_impl(a):
        # np.median() works on the flattened array, and we need a temporary
        # workspace anyway
        temp_arry = a.flatten()
        n = temp_arry.shape[0]
        return _median_inner(temp_arry, n)

    return median_impl

@register_jitable
def _collect_percentiles_inner(a, q):
    n = len(a)

    if n == 1:
        # single element array; output same for all percentiles
        out = np.full(len(q), a[0], dtype=np.float64)
    else:
        out = np.empty(len(q), dtype=np.float64)
        for i in range(len(q)):
            percentile = q[i]

            # bypass pivoting where requested percentile is 100
            if percentile == 100:
                val = np.max(a)
                # heuristics to handle infinite values a la NumPy
                if ~np.all(np.isfinite(a)):
                    if ~np.isfinite(val):
                        val = np.nan

            # bypass pivoting where requested percentile is 0
            elif percentile == 0:
                val = np.min(a)
                # convoluted heuristics to handle infinite values a la NumPy
                if ~np.all(np.isfinite(a)):
                    num_pos_inf = np.sum(a == np.inf)
                    num_neg_inf = np.sum(a == -np.inf)
                    num_finite = n - (num_neg_inf + num_pos_inf)
                    if num_finite == 0:
                        val = np.nan
                    if num_pos_inf == 1 and n == 2:
                        val = np.nan
                    if num_neg_inf > 1:
                        val = np.nan
                    if num_finite == 1:
                        if num_pos_inf > 1:
                            if num_neg_inf != 1:
                                val = np.nan

            else:
                # linear interp between closest ranks
                rank = 1 + (n - 1) * np.true_divide(percentile, 100.0)
                f = math.floor(rank)
                m = rank - f
                lower, upper = _select_two(a, k=int(f - 1), low=0, high=(n - 1))
                val = lower * (1 - m) + upper * m
            out[i] = val

    return out

@register_jitable
def _can_collect_percentiles(a, nan_mask, skip_nan):
    if skip_nan:
        a = a[~nan_mask]
        if len(a) == 0:
            return False  # told to skip nan, but no elements remain
    else:
        if np.any(nan_mask):
            return False  # told *not* to skip nan, but nan encountered

    if len(a) == 1:  # single element array
        val = a[0]
        return np.isfinite(val)  # can collect percentiles if element is finite
    else:
        return True

@register_jitable
def check_valid(q, q_upper_bound):
    valid = True

    # avoid expensive reductions where possible
    if q.ndim == 1 and q.size < 10:
        for i in range(q.size):
            if q[i] < 0.0 or q[i] > q_upper_bound or np.isnan(q[i]):
                valid = False
                break
    else:
        if np.any(np.isnan(q)) or np.any(q < 0.0) or np.any(q > q_upper_bound):
            valid = False

    return valid

@register_jitable
def percentile_is_valid(q):
    if not check_valid(q, q_upper_bound=100.0):
        raise ValueError('Percentiles must be in the range [0, 100]')

@register_jitable
def quantile_is_valid(q):
    if not check_valid(q, q_upper_bound=1.0):
        raise ValueError('Quantiles must be in the range [0, 1]')

@register_jitable
def _collect_percentiles(a, q, skip_nan):
    temp_arry = a.copy()
    nan_mask = np.isnan(temp_arry)

    if _can_collect_percentiles(temp_arry, nan_mask, skip_nan):
        temp_arry = temp_arry[~nan_mask]
        out = _collect_percentiles_inner(temp_arry, q)
    else:
        out = np.full(len(q), np.nan)

    return out

@register_jitable
def _collect_percentiles(a, q, check_q, factor, skip_nan):
    q = np.asarray(q, dtype=np.float64).flatten()
    check_q(q)
    q = q * factor

    temp_arry = np.asarray(a, dtype=np.float64).flatten()
    nan_mask = np.isnan(temp_arry)

    if _can_collect_percentiles(temp_arry, nan_mask, skip_nan):
        temp_arry = temp_arry[~nan_mask]
        out = _collect_percentiles_inner(temp_arry, q)
    else:
        out = np.full(len(q), np.nan)

    return out

def _percentile_quantile_inner(a, q, skip_nan, factor, check_q):
    """
    The underlying algorithm to find percentiles and quantiles
    is the same, hence we converge onto the same code paths
    in this inner function implementation
    """
    dt = determine_dtype(a)
    if np.issubdtype(dt, np.complexfloating):
        raise TypingError('Not supported for complex dtype')
        # this could be supported, but would require a
        # lexicographic comparison

    def np_percentile_q_scalar_impl(a, q):
        return _collect_percentiles(a, q, check_q, factor, skip_nan)[0]

    def np_percentile_impl(a, q):
        return _collect_percentiles(a, q, check_q, factor, skip_nan)

    if isinstance(q, (types.Number, types.Boolean)):
        return np_percentile_q_scalar_impl
    elif isinstance(q, types.Array) and q.ndim == 0:
        return np_percentile_q_scalar_impl
    else:
        return np_percentile_impl

if numpy_version >= (1, 10):
    @overload(np.percentile)
    def np_percentile(a, q):
        # Note: np.percentile behaviour in the case of an array containing one
        # or more NaNs was changed in numpy 1.10 to return an array of np.NaN of
        # length equal to q, hence version guard.
        return _percentile_quantile_inner(
            a, q, skip_nan=False, factor=1.0, check_q=percentile_is_valid
        )

if numpy_version >= (1, 11):
    @overload(np.nanpercentile)
    def np_nanpercentile(a, q):
        # Note: np.nanpercentile return type in the case of an all-NaN slice
        # was changed in 1.11 to be an array of np.NaN of length equal to q,
        # hence version guard.
        return _percentile_quantile_inner(
            a, q, skip_nan=True, factor=1.0, check_q=percentile_is_valid
        )

if numpy_version >= (1, 15):
    @overload(np.quantile)
    def np_quantile(a, q):
        return _percentile_quantile_inner(
            a, q, skip_nan=False, factor=100.0, check_q=quantile_is_valid
        )

if numpy_version >= (1, 15):
    @overload(np.nanquantile)
    def np_nanquantile(a, q):
        return _percentile_quantile_inner(
            a, q, skip_nan=True, factor=100.0, check_q=quantile_is_valid
        )

if numpy_version >= (1, 9):
    @overload(np.nanmedian)
    def np_nanmedian(a):
        if not isinstance(a, types.Array):
            return
        isnan = get_isnan(a.dtype)

        def nanmedian_impl(a):
            # Create a temporary workspace with only non-NaN values
            temp_arry = np.empty(a.size, a.dtype)
            n = 0
            for view in np.nditer(a):
                v = view.item()
                if not isnan(v):
                    temp_arry[n] = v
                    n += 1

            # all NaNs
            if n == 0:
                return np.nan

            return _median_inner(temp_arry, n)

        return nanmedian_impl

@register_jitable
def np_partition_impl_inner(a, kth_array):

    # allocate and fill empty array rather than copy a and mutate in place
    # as the latter approach fails to preserve strides
    out = np.empty_like(a)

    idx = np.ndindex(a.shape[:-1])  # Numpy default partition axis is -1
    for s in idx:
        arry = a[s].copy()
        low = 0
        high = len(arry) - 1

        for kth in kth_array:
            _select_w_nan(arry, kth, low, high)
            low = kth  # narrow span of subsequent partition

        out[s] = arry
    return out

@register_jitable
def valid_kths(a, kth):
    """
    Returns a sorted, unique array of kth values which serve
    as indexers for partitioning the input array, a.

    If the absolute value of any of the provided values
    is greater than a.shape[-1] an exception is raised since
    we are partitioning along the last axis (per Numpy default
    behaviour).

    Values less than 0 are transformed to equivalent positive
    index values.
    """
    kth_array = _asarray(kth).astype(np.int64)  # cast boolean to int, where relevant

    if kth_array.ndim != 1:
        raise ValueError('kth must be scalar or 1-D')
        # numpy raises ValueError: object too deep for desired array

    if np.any(np.abs(kth_array) >= a.shape[-1]):
        raise ValueError("kth out of bounds")

    out = np.empty_like(kth_array)

    for index, val in np.ndenumerate(kth_array):
        if val < 0:
            out[index] = val + a.shape[-1]  # equivalent positive index
        else:
            out[index] = val

    return np.unique(out)

@overload(np.partition)
def np_partition(a, kth):

    if not isinstance(a, (types.Array, types.Sequence, types.Tuple)):
        raise TypeError('The first argument must be an array-like')

    if isinstance(a, types.Array) and a.ndim == 0:
        raise TypeError('The first argument must be at least 1-D (found 0-D)')

    kthdt = getattr(kth, 'dtype', kth)
    if not isinstance(kthdt, (types.Boolean, types.Integer)):  # bool gets cast to int subsequently
        raise TypeError('Partition index must be integer')

    def np_partition_impl(a, kth):
        a_tmp = _asarray(a)
        if a_tmp.size == 0:
            return a_tmp.copy()
        else:
            kth_array = valid_kths(a_tmp, kth)
            return np_partition_impl_inner(a_tmp, kth_array)

    return np_partition_impl

#----------------------------------------------------------------------------
# Building matrices

@register_jitable
def _tri_impl(N, M, k):
    shape = max(0, N), max(0, M)  # numpy floors each dimension at 0
    out = np.empty(shape, dtype=np.float64)  # numpy default dtype

    for i in range(shape[0]):
        m_max = min(max(0, i + k + 1), shape[1])
        out[i, :m_max] = 1
        out[i, m_max:] = 0

    return out

@overload(np.tri)
def np_tri(N, M=None, k=0):

    # we require k to be integer, unlike numpy
    if not isinstance(k, (int, types.Integer)):
        raise TypeError('k must be an integer')

    def tri_impl(N, M=None, k=0):
        if M is None:
            M = N
        return _tri_impl(N, M, k)

    return tri_impl

@register_jitable
def _make_square(m):
    """
    Takes a 1d array and tiles it to form a square matrix
    - i.e. a facsimile of np.tile(m, (len(m), 1))
    """
    assert m.ndim == 1

    len_m = len(m)
    out = np.empty((len_m, len_m), dtype=m.dtype)

    for i in range(len_m):
        out[i] = m

    return out

@register_jitable
def np_tril_impl_2d(m, k=0):
    mask = np.tri(m.shape[-2], M=m.shape[-1], k=k).astype(np.uint)
    return np.where(mask, m, np.zeros_like(m, dtype=m.dtype))

@overload(np.tril)
def my_tril(m, k=0):

    # we require k to be integer, unlike numpy
    if not isinstance(k, (int, types.Integer)):
        raise TypeError('k must be an integer')

    def np_tril_impl_1d(m, k=0):
        m_2d = _make_square(m)
        return np_tril_impl_2d(m_2d, k)

    def np_tril_impl_multi(m, k=0):
        mask = np.tri(m.shape[-2], M=m.shape[-1], k=k).astype(np.uint)
        idx = np.ndindex(m.shape[:-2])
        z = np.empty_like(m)
        zero_opt = np.zeros_like(mask, dtype=m.dtype)
        for sel in idx:
            z[sel] = np.where(mask, m[sel], zero_opt)
        return z

    if m.ndim == 1:
        return np_tril_impl_1d
    elif m.ndim == 2:
        return np_tril_impl_2d
    else:
        return np_tril_impl_multi

@register_jitable
def np_triu_impl_2d(m, k=0):
    mask = np.tri(m.shape[-2], M=m.shape[-1], k=k-1).astype(np.uint)
    return np.where(mask, np.zeros_like(m, dtype=m.dtype), m)

@overload(np.triu)
def my_triu(m, k=0):

    # we require k to be integer, unlike numpy
    if not isinstance(k, (int, types.Integer)):
        raise TypeError('k must be an integer')

    def np_triu_impl_1d(m, k=0):
        m_2d = _make_square(m)
        return np_triu_impl_2d(m_2d, k)

    def np_triu_impl_multi(m, k=0):
        mask = np.tri(m.shape[-2], M=m.shape[-1], k=k-1).astype(np.uint)
        idx = np.ndindex(m.shape[:-2])
        z = np.empty_like(m)
        zero_opt = np.zeros_like(mask, dtype=m.dtype)
        for sel in idx:
            z[sel] = np.where(mask, zero_opt, m[sel])
        return z

    if m.ndim == 1:
        return np_triu_impl_1d
    elif m.ndim == 2:
        return np_triu_impl_2d
    else:
        return np_triu_impl_multi

def _prepare_array(arr):
    pass

@overload(_prepare_array)
def _prepare_array_impl(arr):
    if arr in (None, types.none):
        return lambda arr: np.array(())
    else:
        return lambda arr: _asarray(arr).ravel()

def _dtype_of_compound(inobj):
    obj = inobj
    while True:
        if isinstance(obj, (types.Number, types.Boolean)):
            return as_dtype(obj)
        l = getattr(obj, '__len__', None)
        if l is not None and l() == 0: # empty tuple or similar
            return np.float64
        dt = getattr(obj, 'dtype', None)
        if dt is None:
            raise TypeError("type has no dtype attr")
        if isinstance(obj, types.Sequence):
            obj = obj.dtype
        else:
            return as_dtype(dt)


if numpy_version >= (1, 12):  # replicate behaviour of NumPy 1.12 bugfix release
    @overload(np.ediff1d)
    def np_ediff1d(ary, to_end=None, to_begin=None):

        if isinstance(ary, types.Array):
            if isinstance(ary.dtype, types.Boolean):
                raise TypeError("Boolean dtype is unsupported (as per NumPy)")
                # Numpy tries to do this: return ary[1:] - ary[:-1] which results in a
                # TypeError exception being raised

        # since np 1.16 there are casting checks for to_end and to_begin to make
        # sure they are compatible with the ary
        if numpy_version >= (1, 16):
            ary_dt = _dtype_of_compound(ary)
            to_begin_dt = None
            if not(_is_nonelike(to_begin)):
                to_begin_dt = _dtype_of_compound(to_begin)
            to_end_dt = None
            if not(_is_nonelike(to_end)):
                to_end_dt = _dtype_of_compound(to_end)

            if to_begin_dt is not None and not np.can_cast(to_begin_dt, ary_dt):
                msg = "dtype of to_begin must be compatible with input ary"
                raise TypeError(msg)

            if to_end_dt is not None and not np.can_cast(to_end_dt, ary_dt):
                msg = "dtype of to_end must be compatible with input ary"
                raise TypeError(msg)


        def np_ediff1d_impl(ary, to_end=None, to_begin=None):
            # transform each input into an equivalent 1d array
            start = _prepare_array(to_begin)
            mid = _prepare_array(ary)
            end = _prepare_array(to_end)

            out_dtype = mid.dtype
            # output array dtype determined by ary dtype, per NumPy (for the most part);
            # an exception to the rule is a zero length array-like, where NumPy falls back
            # to np.float64; this behaviour is *not* replicated

            if len(mid) > 0:
                out = np.empty((len(start) + len(mid) + len(end) - 1), dtype=out_dtype)
                start_idx = len(start)
                mid_idx = len(start) + len(mid) - 1
                out[:start_idx] = start
                out[start_idx:mid_idx] = np.diff(mid)
                out[mid_idx:] = end
            else:
                out = np.empty((len(start) + len(end)), dtype=out_dtype)
                start_idx = len(start)
                out[:start_idx] = start
                out[start_idx:] = end
            return out

        return np_ediff1d_impl

def _select_element(arr):
    pass

@overload(_select_element)
def _select_element_impl(arr):
    zerod = getattr(arr, 'ndim', None) == 0
    if zerod:
        def impl(arr):
            x = np.array((1,), dtype=arr.dtype)
            x[:] = arr
            return x[0]
        return impl
    else:
        def impl(arr):
            return arr
        return impl

def _get_d(dx, x):
    pass

@overload(_get_d)
def get_d_impl(x, dx):
    if _is_nonelike(x):
        def impl(x, dx):
            return np.asarray(dx)
    else:
        def impl(x, dx):
            return np.diff(np.asarray(x))
    return impl

@overload(np.trapz)
def np_trapz(y, x=None, dx=1.0):

    if isinstance(y, (types.Number, types.Boolean)):
        raise TypingError('y cannot be a scalar')
    elif isinstance(y, types.Array) and y.ndim == 0:
        raise TypingError('y cannot be 0D')
        # NumPy raises IndexError: list assignment index out of range

    # inspired by:
    # https://github.com/numpy/numpy/blob/7ee52003/numpy/lib/function_base.py#L4040-L4065
    def impl(y, x=None, dx=1.0):
        yarr = np.asarray(y)
        d = _get_d(x, dx)
        y_ave = (yarr[..., slice(1, None)] + yarr[..., slice(None, -1)]) / 2.0
        ret = np.sum(d * y_ave, -1)
        processed = _select_element(ret)
        return processed

    return impl

@register_jitable
def _np_vander(x, N, increasing, out):
    """
    Generate an N-column Vandermonde matrix from a supplied 1-dimensional
    array, x. Store results in an output matrix, out, which is assumed to
    be of the required dtype.

    Values are accumulated using np.multiply to match the floating point
    precision behaviour of numpy.vander.
    """
    m, n = out.shape
    assert m == len(x)
    assert n == N

    if increasing:
        for i in range(N):
            if i == 0:
                out[:, i] = 1
            else:
                out[:, i] = np.multiply(x, out[:, (i - 1)])
    else:
        for i in range(N - 1, -1, -1):
            if i == N - 1:
                out[:, i] = 1
            else:
                out[:, i] = np.multiply(x, out[:, (i + 1)])

@register_jitable
def _check_vander_params(x, N):
    if x.ndim > 1:
        raise ValueError('x must be a one-dimensional array or sequence.')
    if N < 0:
        raise ValueError('Negative dimensions are not allowed')

@overload(np.vander)
def np_vander(x, N=None, increasing=False):

    if N not in (None, types.none):
        if not isinstance(N, types.Integer):
            raise TypingError('Second argument N must be None or an integer')

    def np_vander_impl(x, N=None, increasing=False):
        if N is None:
            N = len(x)

        _check_vander_params(x, N)

        # allocate output matrix using dtype determined in closure
        out = np.empty((len(x), int(N)), dtype=dtype)

        _np_vander(x, N, increasing, out)
        return out

    def np_vander_seq_impl(x, N=None, increasing=False):
        if N is None:
            N = len(x)

        x_arr = np.array(x)
        _check_vander_params(x_arr, N)

        # allocate output matrix using dtype inferred when x_arr was created
        out = np.empty((len(x), int(N)), dtype=x_arr.dtype)

        _np_vander(x_arr, N, increasing, out)
        return out

    if isinstance(x, types.Array):
        x_dt = as_dtype(x.dtype)
        dtype = np.promote_types(x_dt, int)  # replicate numpy behaviour w.r.t. type promotion
        return np_vander_impl
    elif isinstance(x, (types.Tuple, types.Sequence)):
        return np_vander_seq_impl

@overload(np.roll)
def np_roll(a, shift):

    if not isinstance(shift, (types.Integer, types.Boolean)):
        raise TypingError('shift must be an integer')

    def np_roll_impl(a, shift):
        arr = np.asarray(a)
        out = np.empty(arr.shape, dtype=arr.dtype)
        # empty_like might result in different contiguity vs NumPy

        arr_flat = arr.flat
        for i in range(arr.size):
            idx = (i + shift) % arr.size
            out.flat[idx] = arr_flat[i]

        return out

    if isinstance(a, (types.Number, types.Boolean)):
        return lambda a, shift: np.asarray(a)
    else:
        return np_roll_impl

#----------------------------------------------------------------------------
# Mathematical functions

LIKELY_IN_CACHE_SIZE = 8

@register_jitable
def binary_search_with_guess(key, arr, length, guess):
    # NOTE: Do not refactor... see note in np_interp function impl below
    # this is a facsimile of binary_search_with_guess prior to 1.15:
    # https://github.com/numpy/numpy/blob/maintenance/1.15.x/numpy/core/src/multiarray/compiled_base.c
    # Permanent reference:
    # https://github.com/numpy/numpy/blob/3430d78c01a3b9a19adad75f1acb5ae18286da73/numpy/core/src/multiarray/compiled_base.c#L447
    imin = 0
    imax = length

    # Handle keys outside of the arr range first
    if key > arr[length - 1]:
        return length
    elif key < arr[0]:
        return -1

    # If len <= 4 use linear search.
    # From above we know key >= arr[0] when we start.
    if length <= 4:
        i = 1
        while i < length and key >= arr[i]:
            i += 1
        return i - 1

    if guess > length - 3:
        guess = length - 3

    if guess < 1:
        guess = 1

    # check most likely values: guess - 1, guess, guess + 1
    if key < arr[guess]:
        if key < arr[guess - 1]:
            imax = guess - 1

            # last attempt to restrict search to items in cache
            if guess > LIKELY_IN_CACHE_SIZE and \
                    key >= arr[guess - LIKELY_IN_CACHE_SIZE]:
                imin = guess - LIKELY_IN_CACHE_SIZE
        else:
            # key >= arr[guess - 1]
            return guess - 1
    else:
        # key >= arr[guess]
        if key < arr[guess + 1]:
            return guess
        else:
            # key >= arr[guess + 1]
            if key < arr[guess + 2]:
                return guess + 1
            else:
                # key >= arr[guess + 2]
                imin = guess + 2
                # last attempt to restrict search to items in cache
                if (guess < (length - LIKELY_IN_CACHE_SIZE - 1)) and \
                        (key < arr[guess + LIKELY_IN_CACHE_SIZE]):
                    imax = guess + LIKELY_IN_CACHE_SIZE

    # finally, find index by bisection
    while imin < imax:
        imid = imin + ((imax - imin) >> 1)
        if key >= arr[imid]:
            imin = imid + 1
        else:
            imax = imid

    return imin - 1

@register_jitable
def np_interp_impl_complex_fp_inner(x, xp, fp, dtype):
    # NOTE: Do not refactor... see note in np_interp function impl below
    # this is a facsimile of arr_interp_complex prior to 1.16:
    # https://github.com/numpy/numpy/blob/maintenance/1.15.x/numpy/core/src/multiarray/compiled_base.c
    # Permanent reference:
    # https://github.com/numpy/numpy/blob/3430d78c01a3b9a19adad75f1acb5ae18286da73/numpy/core/src/multiarray/compiled_base.c#L683
    dz = np.asarray(x)
    dx = np.asarray(xp)
    dy = np.asarray(fp)

    if len(dx) == 0:
        raise ValueError('array of sample points is empty')

    if len(dx) != len(dy):
        raise ValueError('fp and xp are not of the same size.')

    if dx.size == 1:
        return np.full(dz.shape, fill_value=dy[0], dtype=dtype)

    dres = np.empty(dz.shape, dtype=dtype)

    lenx = dz.size
    lenxp = len(dx)
    lval = dy[0]
    rval = dy[lenxp - 1]

    if lenxp == 1:
        xp_val = dx[0]
        fp_val = dy[0]

        for i in range(lenx):
            x_val = dz.flat[i]
            if x_val < xp_val:
                dres.flat[i] = lval
            elif x_val > xp_val:
                dres.flat[i] = rval
            else:
                dres.flat[i] = fp_val

    else:
        j = 0

        # only pre-calculate slopes if there are relatively few of them.
        if lenxp <= lenx:
            slopes = np.empty((lenxp - 1), dtype=dtype)
        else:
            slopes = np.empty(0, dtype=dtype)

        if slopes.size:
            for i in range(lenxp - 1):
                inv_dx = 1 / (dx[i + 1] - dx[i])
                real = (dy[i + 1].real - dy[i].real) * inv_dx
                imag = (dy[i + 1].imag - dy[i].imag) * inv_dx
                slopes[i] = real + 1j * imag

        for i in range(lenx):
            x_val = dz.flat[i]

            if np.isnan(x_val):
                real = x_val
                imag = 0.0
                dres.flat[i] = real + 1j * imag
                continue

            j = binary_search_with_guess(x_val, dx, lenxp, j)

            if j == -1:
                dres.flat[i] = lval
            elif j == lenxp:
                dres.flat[i] = rval
            elif j == lenxp - 1:
                dres.flat[i] = dy[j]
            else:
                if slopes.size:
                    slope = slopes[j]
                else:
                    inv_dx = 1 / (dx[j + 1] - dx[j])
                    real = (dy[j + 1].real - dy[j].real) * inv_dx
                    imag = (dy[j + 1].imag - dy[j].imag) * inv_dx
                    slope = real + 1j * imag

                real = slope.real * (x_val - dx[j]) + dy[j].real
                imag = slope.imag * (x_val - dx[j]) + dy[j].imag
                dres.flat[i] = real + 1j * imag

                # NOTE: there's a change in master which is not
                # in any released version of 1.16.x yet... as
                # per the real value implementation, but
                # interpolate real and imaginary parts
                # independently; this will need to be added in
                # due course

    return dres

@register_jitable
def np_interp_impl_complex_fp_inner_116(x, xp, fp, dtype):
    # NOTE: Do not refactor... see note in np_interp function impl below
    # this is a facsimile of arr_interp_complex post 1.16:
    # https://github.com/numpy/numpy/blob/maintenance/1.16.x/numpy/core/src/multiarray/compiled_base.c
    # Permanent reference:
    # https://github.com/numpy/numpy/blob/971e2e89d08deeae0139d3011d15646fdac13c92/numpy/core/src/multiarray/compiled_base.c#L628
    dz = np.asarray(x)
    dx = np.asarray(xp)
    dy = np.asarray(fp)

    if len(dx) == 0:
        raise ValueError('array of sample points is empty')

    if len(dx) != len(dy):
        raise ValueError('fp and xp are not of the same size.')

    if dx.size == 1:
        return np.full(dz.shape, fill_value=dy[0], dtype=dtype)

    dres = np.empty(dz.shape, dtype=dtype)

    lenx = dz.size
    lenxp = len(dx)
    lval = dy[0]
    rval = dy[lenxp - 1]

    if lenxp == 1:
        xp_val = dx[0]
        fp_val = dy[0]

        for i in range(lenx):
            x_val = dz.flat[i]
            if x_val < xp_val:
                dres.flat[i] = lval
            elif x_val > xp_val:
                dres.flat[i] = rval
            else:
                dres.flat[i] = fp_val

    else:
        j = 0

        # only pre-calculate slopes if there are relatively few of them.
        if lenxp <= lenx:
            slopes = np.empty((lenxp - 1), dtype=dtype)
        else:
            slopes = np.empty(0, dtype=dtype)

        if slopes.size:
            for i in range(lenxp - 1):
                inv_dx = 1 / (dx[i + 1] - dx[i])
                real = (dy[i + 1].real - dy[i].real) * inv_dx
                imag = (dy[i + 1].imag - dy[i].imag) * inv_dx
                slopes[i] = real + 1j * imag

        for i in range(lenx):
            x_val = dz.flat[i]

            if np.isnan(x_val):
                real = x_val
                imag = 0.0
                dres.flat[i] = real + 1j * imag
                continue

            j = binary_search_with_guess(x_val, dx, lenxp, j)

            if j == -1:
                dres.flat[i] = lval
            elif j == lenxp:
                dres.flat[i] = rval
            elif j == lenxp - 1:
                dres.flat[i] = dy[j]
            elif dx[j] == x_val:
                # Avoid potential non-finite interpolation
                dres.flat[i] = dy[j]
            else:
                if slopes.size:
                    slope = slopes[j]
                else:
                    inv_dx = 1 / (dx[j + 1] - dx[j])
                    real = (dy[j + 1].real - dy[j].real) * inv_dx
                    imag = (dy[j + 1].imag - dy[j].imag) * inv_dx
                    slope = real + 1j * imag

                real = slope.real * (x_val - dx[j]) + dy[j].real
                imag = slope.imag * (x_val - dx[j]) + dy[j].imag
                dres.flat[i] = real + 1j * imag

                # NOTE: there's a change in master which is not
                # in any released version of 1.16.x yet... as
                # per the real value implementation, but
                # interpolate real and imaginary parts
                # independently; this will need to be added in
                # due course

    return dres

@register_jitable
def np_interp_impl_inner(x, xp, fp, dtype):
    # NOTE: Do not refactor... see note in np_interp function impl below
    # this is a facsimile of arr_interp prior to 1.16:
    # https://github.com/numpy/numpy/blob/maintenance/1.15.x/numpy/core/src/multiarray/compiled_base.c
    # Permanent reference:
    # https://github.com/numpy/numpy/blob/3430d78c01a3b9a19adad75f1acb5ae18286da73/numpy/core/src/multiarray/compiled_base.c#L532
    dz = np.asarray(x)
    dx = np.asarray(xp)
    dy = np.asarray(fp)

    if len(dx) == 0:
        raise ValueError('array of sample points is empty')

    if len(dx) != len(dy):
        raise ValueError('fp and xp are not of the same size.')

    if dx.size == 1:
        return np.full(dz.shape, fill_value=dy[0], dtype=dtype)

    dres = np.empty(dz.shape, dtype=dtype)

    lenx = dz.size
    lenxp = len(dx)
    lval = dy[0]
    rval = dy[lenxp - 1]

    if lenxp == 1:
        xp_val = dx[0]
        fp_val = dy[0]

        for i in range(lenx):
            x_val = dz.flat[i]
            if x_val < xp_val:
                dres.flat[i] = lval
            elif x_val > xp_val:
                dres.flat[i] = rval
            else:
                dres.flat[i] = fp_val

    else:
        j = 0

        # only pre-calculate slopes if there are relatively few of them.
        if lenxp <= lenx:
            slopes = (dy[1:] - dy[:-1]) / (dx[1:] - dx[:-1])
        else:
            slopes = np.empty(0, dtype=dtype)

        for i in range(lenx):
            x_val = dz.flat[i]

            if np.isnan(x_val):
                dres.flat[i] = x_val
                continue

            j = binary_search_with_guess(x_val, dx, lenxp, j)

            if j == -1:
                dres.flat[i] = lval
            elif j == lenxp:
                dres.flat[i] = rval
            elif j == lenxp - 1:
                dres.flat[i] = dy[j]
            else:
                if slopes.size:
                    slope = slopes[j]
                else:
                    slope = (dy[j + 1] - dy[j]) / (dx[j + 1] - dx[j])

                dres.flat[i] = slope * (x_val - dx[j]) + dy[j]

                # NOTE: this is in master but not in any released
                # version of 1.16.x yet...
                #
                # If we get nan in one direction, try the other
                # if np.isnan(dres.flat[i]):
                #     dres.flat[i] = slope * (x_val - dx[j + 1]) + dy[j + 1]
                #
                #     if np.isnan(dres.flat[i]) and dy[j] == dy[j + 1]:
                #         dres.flat[i] = dy[j]

    return dres

@register_jitable
def np_interp_impl_inner_116(x, xp, fp, dtype):
    # NOTE: Do not refactor... see note in np_interp function impl below
    # this is a facsimile of arr_interp post 1.16:
    # https://github.com/numpy/numpy/blob/maintenance/1.16.x/numpy/core/src/multiarray/compiled_base.c
    # Permanent reference:
    # https://github.com/numpy/numpy/blob/971e2e89d08deeae0139d3011d15646fdac13c92/numpy/core/src/multiarray/compiled_base.c#L473
    dz = np.asarray(x)
    dx = np.asarray(xp)
    dy = np.asarray(fp)

    if len(dx) == 0:
        raise ValueError('array of sample points is empty')

    if len(dx) != len(dy):
        raise ValueError('fp and xp are not of the same size.')

    if dx.size == 1:
        return np.full(dz.shape, fill_value=dy[0], dtype=dtype)

    dres = np.empty(dz.shape, dtype=dtype)

    lenx = dz.size
    lenxp = len(dx)
    lval = dy[0]
    rval = dy[lenxp - 1]

    if lenxp == 1:
        xp_val = dx[0]
        fp_val = dy[0]

        for i in range(lenx):
            x_val = dz.flat[i]
            if x_val < xp_val:
                dres.flat[i] = lval
            elif x_val > xp_val:
                dres.flat[i] = rval
            else:
                dres.flat[i] = fp_val

    else:
        j = 0

        # only pre-calculate slopes if there are relatively few of them.
        if lenxp <= lenx:
            slopes = (dy[1:] - dy[:-1]) / (dx[1:] - dx[:-1])
        else:
            slopes = np.empty(0, dtype=dtype)

        for i in range(lenx):
            x_val = dz.flat[i]

            if np.isnan(x_val):
                dres.flat[i] = x_val
                continue

            j = binary_search_with_guess(x_val, dx, lenxp, j)

            if j == -1:
                dres.flat[i] = lval
            elif j == lenxp:
                dres.flat[i] = rval
            elif j == lenxp - 1:
                dres.flat[i] = dy[j]
            elif dx[j] == x_val:
                # Avoid potential non-finite interpolation
                dres.flat[i] = dy[j]
            else:
                if slopes.size:
                    slope = slopes[j]
                else:
                    slope = (dy[j + 1] - dy[j]) / (dx[j + 1] - dx[j])

                dres.flat[i] = slope * (x_val - dx[j]) + dy[j]

                # NOTE: this is in master but not in any released
                # version of 1.16.x yet...
                #
                # If we get nan in one direction, try the other
                # if np.isnan(dres.flat[i]):
                #     dres.flat[i] = slope * (x_val - dx[j + 1]) + dy[j + 1]
                #
                #     if np.isnan(dres.flat[i]) and dy[j] == dy[j + 1]:
                #         dres.flat[i] = dy[j]

    return dres

if numpy_version >= (1, 10):
    # replicate behaviour change of 1.10+
    @overload(np.interp)
    def np_interp(x, xp, fp):
        # NOTE: there is considerable duplication present in the functions:
        # np_interp_impl_complex_fp_inner_116
        # np_interp_impl_complex_fp_inner
        # np_interp_impl_inner_116
        # np_interp_impl_inner
        #
        # This is because:
        # 1. Replicating basic interp is relatively simple, however matching the
        #    behaviour of NumPy for edge cases is really quite hard, after a
        #    couple of attempts trying to avoid translation of the C source it
        #    was deemed unavoidable.
        # 2. Due to 1. it is much easier to keep track of changes if the Numba
        #    source reflects the NumPy C source, so the duplication is kept.
        # 3. There are significant changes that happened in the NumPy 1.16
        #    release series, hence functions with `np116` appended, they behave
        #    slightly differently!

        if hasattr(xp, 'ndim') and xp.ndim > 1:
            raise TypingError('xp must be 1D')
        if hasattr(fp, 'ndim') and fp.ndim > 1:
            raise TypingError('fp must be 1D')

        complex_dtype_msg = (
            "Cannot cast array data from complex dtype to float64 dtype"
        )

        xp_dt = determine_dtype(xp)
        if np.issubdtype(xp_dt, np.complexfloating):
            raise TypingError(complex_dtype_msg)

        if numpy_version < (1, 12):
            fp_dt = determine_dtype(fp)
            if np.issubdtype(fp_dt, np.complexfloating):
                raise TypingError(complex_dtype_msg)

        if numpy_version >= (1, 16):
            impl = np_interp_impl_inner_116
            impl_complex = np_interp_impl_complex_fp_inner_116
        else:
            impl = np_interp_impl_inner
            impl_complex = np_interp_impl_complex_fp_inner

        fp_dt = determine_dtype(fp)
        dtype = np.result_type(fp_dt, np.float64)

        if np.issubdtype(dtype, np.complexfloating):
            inner = impl_complex
        else:
            inner = impl

        def np_interp_impl(x, xp, fp):
            return inner(x, xp, fp, dtype)

        def np_interp_scalar_impl(x, xp, fp):
            return inner(x, xp, fp, dtype).flat[0]

        if isinstance(x, types.Number):
            if isinstance(x, types.Complex):
                raise TypingError(complex_dtype_msg)
            return np_interp_scalar_impl

        return np_interp_impl

#----------------------------------------------------------------------------
# Statistics

@register_jitable
def row_wise_average(a):
    assert a.ndim == 2

    m, n = a.shape
    out = np.empty((m, 1), dtype=a.dtype)

    for i in range(m):
        out[i, 0] = np.sum(a[i, :]) / n

    return out

@register_jitable
def np_cov_impl_inner(X, bias, ddof):

    # determine degrees of freedom
    if ddof is None:
        if bias:
            ddof = 0
        else:
            ddof = 1

    # determine the normalization factor
    fact = X.shape[1] - ddof

    # numpy warns if less than 0 and floors at 0
    fact = max(fact, 0.0)

    # de-mean
    X -= row_wise_average(X)

    # calculate result - requires blas
    c = np.dot(X, np.conj(X.T))
    c *= np.true_divide(1, fact)
    return c

def _prepare_cov_input_inner():
    pass

@overload(_prepare_cov_input_inner)
def _prepare_cov_input_impl(m, y, rowvar, dtype):
    if y in (None, types.none):
        def _prepare_cov_input_inner(m, y, rowvar, dtype):
            m_arr = np.atleast_2d(_asarray(m))

            if not rowvar:
                m_arr = m_arr.T

            return m_arr
    else:
        def _prepare_cov_input_inner(m, y, rowvar, dtype):
            m_arr = np.atleast_2d(_asarray(m))
            y_arr = np.atleast_2d(_asarray(y))

            # transpose if asked to and not a (1, n) vector - this looks
            # wrong as you might end up transposing one and not the other,
            # but it's what numpy does
            if not rowvar:
                if m_arr.shape[0] != 1:
                    m_arr = m_arr.T
                if y_arr.shape[0] != 1:
                    y_arr = y_arr.T

            m_rows, m_cols = m_arr.shape
            y_rows, y_cols = y_arr.shape

            if m_cols != y_cols:
                raise ValueError("m and y have incompatible dimensions")

            # allocate and fill output array
            out = np.empty((m_rows + y_rows, m_cols), dtype=dtype)
            out[:m_rows, :] = m_arr
            out[-y_rows:, :] = y_arr

            return out

    return _prepare_cov_input_inner

@register_jitable
def _handle_m_dim_change(m):
    if m.ndim == 2 and m.shape[0] == 1:
        msg = ("2D array containing a single row is unsupported due to "
               "ambiguity in type inference. To use numpy.cov in this case "
               "simply pass the row as a 1D array, i.e. m[0].")
        raise RuntimeError(msg)

_handle_m_dim_nop = register_jitable(lambda x: x)

def determine_dtype(array_like):
    array_like_dt = np.float64
    if isinstance(array_like, types.Array):
        array_like_dt = as_dtype(array_like.dtype)
    elif isinstance(array_like, (types.Number, types.Boolean)):
        array_like_dt = as_dtype(array_like)
    elif isinstance(array_like, (types.UniTuple, types.Tuple)):
        coltypes = set()
        for val in array_like:
            if hasattr(val, 'count'):
                [coltypes.add(v) for v in val]
            else:
                coltypes.add(val)
        if len(coltypes) > 1:
            array_like_dt = np.promote_types(*[as_dtype(ty) for ty in coltypes])
        elif len(coltypes) == 1:
            array_like_dt = as_dtype(coltypes.pop())

    return array_like_dt

def check_dimensions(array_like, name):
    if isinstance(array_like, types.Array):
        if array_like.ndim > 2:
            raise TypeError("{0} has more than 2 dimensions".format(name))
    elif isinstance(array_like, types.Sequence):
        if isinstance(array_like.key[0], types.Sequence):
            if isinstance(array_like.key[0].key[0], types.Sequence):
                raise TypeError("{0} has more than 2 dimensions".format(name))

@register_jitable
def _handle_ddof(ddof):
    if not np.isfinite(ddof):
        raise ValueError('Cannot convert non-finite ddof to integer')
    if ddof - int(ddof) != 0:
        raise ValueError('ddof must be integral value')

_handle_ddof_nop = register_jitable(lambda x: x)

@register_jitable
def _prepare_cov_input(m, y, rowvar, dtype, ddof, _DDOF_HANDLER, _M_DIM_HANDLER):
    _M_DIM_HANDLER(m)
    _DDOF_HANDLER(ddof)
    return _prepare_cov_input_inner(m, y, rowvar, dtype)

def scalar_result_expected(mandatory_input, optional_input):
    opt_is_none = optional_input in (None, types.none)

    if isinstance(mandatory_input, types.Array) and mandatory_input.ndim == 1:
        return opt_is_none

    if isinstance(mandatory_input, types.BaseTuple):
        if all(isinstance(x, (types.Number, types.Boolean)) for x in mandatory_input.types):
            return opt_is_none
        else:
            if len(mandatory_input.types) == 1 and isinstance(mandatory_input.types[0], types.BaseTuple):
                return opt_is_none

    if isinstance(mandatory_input, (types.Number, types.Boolean)):
        return opt_is_none

    if isinstance(mandatory_input, types.Sequence):
        if not isinstance(mandatory_input.key[0], types.Sequence) and opt_is_none:
            return True

    return False

@register_jitable
def _clip_corr(x):
    return np.where(np.fabs(x) > 1, np.sign(x), x)

@register_jitable
def _clip_complex(x):
    real = _clip_corr(x.real)
    imag = _clip_corr(x.imag)
    return real + 1j * imag

if numpy_version >= (1, 10):  # replicate behaviour post numpy 1.10 bugfix release
    @overload(np.cov)
    def np_cov(m, y=None, rowvar=True, bias=False, ddof=None):

        # reject problem if m and / or y are more than 2D
        check_dimensions(m, 'm')
        check_dimensions(y, 'y')

        # reject problem if ddof invalid (either upfront if type is
        # obviously invalid, or later if value found to be non-integral)
        if ddof in (None, types.none):
            _DDOF_HANDLER = _handle_ddof_nop
        else:
            if isinstance(ddof, (types.Integer, types.Boolean)):
                _DDOF_HANDLER = _handle_ddof_nop
            elif isinstance(ddof, types.Float):
                _DDOF_HANDLER = _handle_ddof
            else:
                raise TypingError('ddof must be a real numerical scalar type')

        # special case for 2D array input with 1 row of data - select
        # handler function which we'll call later when we have access
        # to the shape of the input array
        _M_DIM_HANDLER = _handle_m_dim_nop
        if isinstance(m, types.Array):
            _M_DIM_HANDLER = _handle_m_dim_change

        # infer result dtype
        m_dt = determine_dtype(m)
        y_dt = determine_dtype(y)
        dtype = np.result_type(m_dt, y_dt, np.float64)

        def np_cov_impl(m, y=None, rowvar=True, bias=False, ddof=None):
            X = _prepare_cov_input(m, y, rowvar, dtype, ddof, _DDOF_HANDLER, _M_DIM_HANDLER).astype(dtype)

            if np.any(np.array(X.shape) == 0):
                return np.full((X.shape[0], X.shape[0]), fill_value=np.nan, dtype=dtype)
            else:
                return np_cov_impl_inner(X, bias, ddof)

        def np_cov_impl_single_variable(m, y=None, rowvar=True, bias=False, ddof=None):
            X = _prepare_cov_input(m, y, rowvar, ddof, dtype, _DDOF_HANDLER, _M_DIM_HANDLER).astype(dtype)

            if np.any(np.array(X.shape) == 0):
                variance = np.nan
            else:
                variance = np_cov_impl_inner(X, bias, ddof).flat[0]

            return np.array(variance)

        if scalar_result_expected(m, y):
            return np_cov_impl_single_variable
        else:
            return np_cov_impl

    @overload(np.corrcoef)
    def np_corrcoef(x, y=None, rowvar=True):

        x_dt = determine_dtype(x)
        y_dt = determine_dtype(y)
        dtype = np.result_type(x_dt, y_dt, np.float64)

        if dtype == np.complex:
            clip_fn = _clip_complex
        else:
            clip_fn = _clip_corr

        def np_corrcoef_impl(x, y=None, rowvar=True):
            c = np.cov(x, y, rowvar)
            d = np.diag(c)
            stddev = np.sqrt(d.real)

            for i in range(c.shape[0]):
                c[i, :] /= stddev
                c[:, i] /= stddev

            return clip_fn(c)

        def np_corrcoef_impl_single_variable(x, y=None, rowvar=True):
            c = np.cov(x, y, rowvar)
            return c / c

        if scalar_result_expected(x, y):
            return np_corrcoef_impl_single_variable
        else:
            return np_corrcoef_impl

#----------------------------------------------------------------------------
# Element-wise computations

@register_jitable
def _fill_diagonal_params(a, wrap):
    if a.ndim == 2:
        m = a.shape[0]
        n = a.shape[1]
        step = 1 + n
        if wrap:
            end = n * m
        else:
            end = n * min(m, n)
    else:
        shape = np.array(a.shape)

        if not np.all(np.diff(shape) == 0):
            raise ValueError("All dimensions of input must be of equal length")

        step = 1 + (np.cumprod(shape[:-1])).sum()
        end = shape.prod()

    return end, step

@register_jitable
def _fill_diagonal_scalar(a, val, wrap):
    end, step = _fill_diagonal_params(a, wrap)

    for i in range(0, end, step):
        a.flat[i] = val

@register_jitable
def _fill_diagonal(a, val, wrap):
    end, step = _fill_diagonal_params(a, wrap)
    ctr = 0
    v_len = len(val)

    for i in range(0, end, step):
        a.flat[i] = val[ctr]
        ctr += 1
        ctr = ctr % v_len

@register_jitable
def _check_val_int(a, val):
    iinfo = np.iinfo(a.dtype)
    v_min = iinfo.min
    v_max = iinfo.max

    # check finite values are within bounds
    if np.any(~np.isfinite(val)) or np.any(val < v_min) or np.any(val > v_max):
        raise ValueError('Unable to safely conform val to a.dtype')

@register_jitable
def _check_val_float(a, val):
    finfo = np.finfo(a.dtype)
    v_min = finfo.min
    v_max = finfo.max

    # check finite values are within bounds
    finite_vals = val[np.isfinite(val)]
    if np.any(finite_vals < v_min) or np.any(finite_vals > v_max):
        raise ValueError('Unable to safely conform val to a.dtype')

# no check performed, needed for pathway where no check is required
_check_nop = register_jitable(lambda x, y: x)

def _asarray(x):
    pass

@overload(_asarray)
def _asarray_impl(x):
    if isinstance(x, types.Array):
        return lambda x: x
    elif isinstance(x, (types.Sequence, types.Tuple)):
        return lambda x: np.array(x)
    elif isinstance(x, (types.Number, types.Boolean)):
        ty = as_dtype(x)
        return lambda x: np.array([x], dtype=ty)

@overload(np.fill_diagonal)
def np_fill_diagonal(a, val, wrap=False):

    if a.ndim > 1:
        # the following can be simplified after #3088; until then, employ
        # a basic mechanism for catching cases where val is of a type/value
        # which cannot safely be cast to a.dtype
        if isinstance(a.dtype, types.Integer):
            checker = _check_val_int
        elif isinstance(a.dtype, types.Float):
            checker = _check_val_float
        else:
            checker = _check_nop

        def scalar_impl(a, val, wrap=False):
            tmpval = _asarray(val).flatten()
            checker(a, tmpval)
            _fill_diagonal_scalar(a, val, wrap)

        def non_scalar_impl(a, val, wrap=False):
            tmpval = _asarray(val).flatten()
            checker(a, tmpval)
            _fill_diagonal(a, tmpval, wrap)

        if isinstance(val, (types.Float, types.Integer, types.Boolean)):
            return scalar_impl
        elif isinstance(val, (types.Tuple, types.Sequence, types.Array)):
            return non_scalar_impl
    else:
        msg = "The first argument must be at least 2-D (found %s-D)" % a.ndim
        raise TypingError(msg)

def _np_round_intrinsic(tp):
    # np.round() always rounds half to even
    return "llvm.rint.f%d" % (tp.bitwidth,)

def _np_round_float(context, builder, tp, val):
    llty = context.get_value_type(tp)
    module = builder.module
    fnty = lc.Type.function(llty, [llty])
    fn = module.get_or_insert_function(fnty, name=_np_round_intrinsic(tp))
    return builder.call(fn, (val,))

@lower_builtin(np.round, types.Float)
def scalar_round_unary(context, builder, sig, args):
    res =  _np_round_float(context, builder, sig.args[0], args[0])
    return impl_ret_untracked(context, builder, sig.return_type, res)

@lower_builtin(np.round, types.Integer)
def scalar_round_unary(context, builder, sig, args):
    res = args[0]
    return impl_ret_untracked(context, builder, sig.return_type, res)

@lower_builtin(np.round, types.Complex)
def scalar_round_unary_complex(context, builder, sig, args):
    fltty = sig.args[0].underlying_float
    z = context.make_complex(builder, sig.args[0], args[0])
    z.real = _np_round_float(context, builder, fltty, z.real)
    z.imag = _np_round_float(context, builder, fltty, z.imag)
    res = z._getvalue()
    return impl_ret_untracked(context, builder, sig.return_type, res)

@lower_builtin(np.round, types.Float, types.Integer)
@lower_builtin(np.round, types.Integer, types.Integer)
def scalar_round_binary_float(context, builder, sig, args):
    def round_ndigits(x, ndigits):
        if math.isinf(x) or math.isnan(x):
            return x

        # NOTE: this is CPython's algorithm, but perhaps this is overkill
        # when emulating Numpy's behaviour.
        if ndigits >= 0:
            if ndigits > 22:
                # pow1 and pow2 are each safe from overflow, but
                # pow1*pow2 ~= pow(10.0, ndigits) might overflow.
                pow1 = 10.0 ** (ndigits - 22)
                pow2 = 1e22
            else:
                pow1 = 10.0 ** ndigits
                pow2 = 1.0
            y = (x * pow1) * pow2
            if math.isinf(y):
                return x
            return (np.round(y) / pow2) / pow1

        else:
            pow1 = 10.0 ** (-ndigits)
            y = x / pow1
            return np.round(y) * pow1

    res = context.compile_internal(builder, round_ndigits, sig, args)
    return impl_ret_untracked(context, builder, sig.return_type, res)

@lower_builtin(np.round, types.Complex, types.Integer)
def scalar_round_binary_complex(context, builder, sig, args):
    def round_ndigits(z, ndigits):
        return complex(np.round(z.real, ndigits),
                       np.round(z.imag, ndigits))

    res = context.compile_internal(builder, round_ndigits, sig, args)
    return impl_ret_untracked(context, builder, sig.return_type, res)


@lower_builtin(np.round, types.Array, types.Integer,
           types.Array)
def array_round(context, builder, sig, args):
    def array_round_impl(arr, decimals, out):
        if arr.shape != out.shape:
            raise ValueError("invalid output shape")
        for index, val in np.ndenumerate(arr):
            out[index] = np.round(val, decimals)
        return out

    res = context.compile_internal(builder, array_round_impl, sig, args)
    return impl_ret_new_ref(context, builder, sig.return_type, res)


@lower_builtin(np.sinc, types.Array)
def array_sinc(context, builder, sig, args):
    def array_sinc_impl(arr):
        out = np.zeros_like(arr)
        for index, val in np.ndenumerate(arr):
            out[index] = np.sinc(val)
        return out
    res = context.compile_internal(builder, array_sinc_impl, sig, args)
    return impl_ret_new_ref(context, builder, sig.return_type, res)

@lower_builtin(np.sinc, types.Number)
def scalar_sinc(context, builder, sig, args):
    scalar_dtype = sig.return_type
    def scalar_sinc_impl(val):
        if val == 0.e0: # to match np impl
            val = 1e-20
        val *= np.pi # np sinc is the normalised variant
        return np.sin(val)/val
    res = context.compile_internal(builder, scalar_sinc_impl, sig, args,
                                   locals=dict(c=scalar_dtype))
    return impl_ret_untracked(context, builder, sig.return_type, res)


@lower_builtin(np.angle, types.Number)
@lower_builtin(np.angle, types.Number, types.Boolean)
def scalar_angle_kwarg(context, builder, sig, args):
    deg_mult = sig.return_type(180 / np.pi)
    def scalar_angle_impl(val, deg):
        if deg:
            return np.arctan2(val.imag, val.real) * deg_mult
        else:
            return np.arctan2(val.imag, val.real)

    if len(args) == 1:
        args = args + (cgutils.false_bit,)
        sig = signature(sig.return_type, *(sig.args + (types.boolean,)))
    res = context.compile_internal(builder, scalar_angle_impl,
                                   sig, args)
    return impl_ret_untracked(context, builder, sig.return_type, res)

@lower_builtin(np.angle, types.Array)
@lower_builtin(np.angle, types.Array, types.Boolean)
def array_angle_kwarg(context, builder, sig, args):
    arg = sig.args[0]
    ret_dtype = sig.return_type.dtype

    def array_angle_impl(arr, deg):
        out = np.zeros_like(arr, dtype=ret_dtype)
        for index, val in np.ndenumerate(arr):
            out[index] = np.angle(val, deg)
        return out

    if len(args) == 1:
        args = args + (cgutils.false_bit,)
        sig = signature(sig.return_type, *(sig.args + (types.boolean,)))

    res = context.compile_internal(builder, array_angle_impl, sig, args)
    return impl_ret_new_ref(context, builder, sig.return_type, res)


@lower_builtin(np.nonzero, types.Array)
@lower_builtin("array.nonzero", types.Array)
@lower_builtin(np.where, types.Array)
def array_nonzero(context, builder, sig, args):
    aryty = sig.args[0]
    # Return type is a N-tuple of 1D C-contiguous arrays
    retty = sig.return_type
    outaryty = retty.dtype
    ndim = aryty.ndim
    nouts = retty.count

    ary = make_array(aryty)(context, builder, args[0])
    shape = cgutils.unpack_tuple(builder, ary.shape)
    strides = cgutils.unpack_tuple(builder, ary.strides)
    data = ary.data
    layout = aryty.layout

    # First count the number of non-zero elements
    zero = context.get_constant(types.intp, 0)
    one = context.get_constant(types.intp, 1)
    count = cgutils.alloca_once_value(builder, zero)
    with cgutils.loop_nest(builder, shape, zero.type) as indices:
        ptr = cgutils.get_item_pointer2(builder, data, shape, strides,
                                        layout, indices)
        val = load_item(context, builder, aryty, ptr)
        nz = context.is_true(builder, aryty.dtype, val)
        with builder.if_then(nz):
            builder.store(builder.add(builder.load(count), one), count)

    # Then allocate output arrays of the right size
    out_shape = (builder.load(count),)
    outs = [_empty_nd_impl(context, builder, outaryty, out_shape)._getvalue()
            for i in range(nouts)]
    outarys = [make_array(outaryty)(context, builder, out) for out in outs]
    out_datas = [out.data for out in outarys]

    # And fill them up
    index = cgutils.alloca_once_value(builder, zero)
    with cgutils.loop_nest(builder, shape, zero.type) as indices:
        ptr = cgutils.get_item_pointer2(builder, data, shape, strides,
                                        layout, indices)
        val = load_item(context, builder, aryty, ptr)
        nz = context.is_true(builder, aryty.dtype, val)
        with builder.if_then(nz):
            # Store element indices in output arrays
            if not indices:
                # For a 0-d array, store 0 in the unique output array
                indices = (zero,)
            cur = builder.load(index)
            for i in range(nouts):
                ptr = cgutils.get_item_pointer2(builder, out_datas[i],
                                                out_shape, (),
                                                'C', [cur])
                store_item(context, builder, outaryty, indices[i], ptr)
            builder.store(builder.add(cur, one), index)

    tup = context.make_tuple(builder, sig.return_type, outs)
    return impl_ret_new_ref(context, builder, sig.return_type, tup)


def array_where(context, builder, sig, args):
    """
    np.where(array, array, array)
    """
    layouts = set(a.layout for a in sig.args)

    npty = np.promote_types(as_dtype(sig.args[1].dtype),
                            as_dtype(sig.args[2].dtype))

    if layouts == set('C') or layouts == set('F'):
        # Faster implementation for C-contiguous arrays
        def where_impl(cond, x, y):
            shape = cond.shape
            if x.shape != shape or y.shape != shape:
                raise ValueError("all inputs should have the same shape")
            res = np.empty_like(x, dtype=npty)
            cf = cond.flat
            xf = x.flat
            yf = y.flat
            rf = res.flat
            for i in range(cond.size):
                rf[i] = xf[i] if cf[i] else yf[i]
            return res
    else:
        def where_impl(cond, x, y):
            shape = cond.shape
            if x.shape != shape or y.shape != shape:
                raise ValueError("all inputs should have the same shape")
            res = np.empty(cond.shape, dtype=npty)
            for idx, c in np.ndenumerate(cond):
                res[idx] = x[idx] if c else y[idx]
            return res

    res = context.compile_internal(builder, where_impl, sig, args)
    return impl_ret_untracked(context, builder, sig.return_type, res)


@register_jitable
def _where_x_y_scalar(cond, x, y, res):
    for idx, c in np.ndenumerate(cond):
        res[idx] = x if c else y
    return res


@register_jitable
def _where_x_scalar(cond, x, y, res):
    for idx, c in np.ndenumerate(cond):
        res[idx] = x if c else y[idx]
    return res


@register_jitable
def _where_y_scalar(cond, x, y, res):
    for idx, c in np.ndenumerate(cond):
        res[idx] = x[idx] if c else y
    return res


def _where_inner(context, builder, sig, args, impl):
    cond, x, y = sig.args

    x_dt = determine_dtype(x)
    y_dt = determine_dtype(y)
    npty = np.promote_types(x_dt, y_dt)

    if cond.layout == 'F':
        def where_impl(cond, x, y):
            res = np.asfortranarray(np.empty(cond.shape, dtype=npty))
            return impl(cond, x, y, res)
    else:
        def where_impl(cond, x, y):
            res = np.empty(cond.shape, dtype=npty)
            return impl(cond, x, y, res)

    res = context.compile_internal(builder, where_impl, sig, args)
    return impl_ret_untracked(context, builder, sig.return_type, res)


array_scalar_scalar_where = partial(_where_inner, impl=_where_x_y_scalar)
array_array_scalar_where = partial(_where_inner, impl=_where_y_scalar)
array_scalar_array_where = partial(_where_inner, impl=_where_x_scalar)


@lower_builtin(np.where, types.Any, types.Any, types.Any)
def any_where(context, builder, sig, args):
    cond, x, y = sig.args

    if isinstance(cond, types.Array):
        if isinstance(x, types.Array):
            if isinstance(y, types.Array):
                impl = array_where
            elif isinstance(y, (types.Number, types.Boolean)):
                impl = array_array_scalar_where
        elif isinstance(x, (types.Number, types.Boolean)):
            if isinstance(y, types.Array):
                impl = array_scalar_array_where
            elif isinstance(y, (types.Number, types.Boolean)):
                impl = array_scalar_scalar_where

        return impl(context, builder, sig, args)

    def scalar_where_impl(cond, x, y):
        """
        np.where(scalar, scalar, scalar): return a 0-dim array
        """
        scal = x if cond else y
        # This is the equivalent of np.full_like(scal, scal),
        # for compatibility with Numpy < 1.8
        arr = np.empty_like(scal)
        arr[()] = scal
        return arr

    res = context.compile_internal(builder, scalar_where_impl, sig, args)
    return impl_ret_new_ref(context, builder, sig.return_type, res)


@overload(np.real)
def np_real(a):
    def np_real_impl(a):
        return a.real

    return np_real_impl


@overload(np.imag)
def np_imag(a):
    def np_imag_impl(a):
        return a.imag

    return np_imag_impl


#----------------------------------------------------------------------------
# Misc functions

np_delete_handler_isslice = register_jitable(lambda x : x)
np_delete_handler_isarray = register_jitable(lambda x : np.asarray(x))

@overload(np.delete)
def np_delete(arr, obj):
    # Implementation based on numpy
    # https://github.com/numpy/numpy/blob/af66e487a57bfd4850f4306e3b85d1dac3c70412/numpy/lib/function_base.py#L4065-L4267

    if not isinstance(arr, (types.Array, types.Sequence)):
        raise TypingError("arr must be either an Array or a Sequence")

    if isinstance(obj, (types.Array, types.Sequence, types.SliceType)):
        if isinstance(obj, (types.SliceType)):
            handler = np_delete_handler_isslice
        else:
            if not isinstance(obj.dtype, types.Integer):
                raise TypingError('obj should be of Integer dtype')
            handler = np_delete_handler_isarray

        def np_delete_impl(arr, obj):
            arr = np.ravel(np.asarray(arr))
            N = arr.size

            keep = np.ones(N, dtype=np.bool_)
            obj = handler(obj)
            keep[obj] = False
            return arr[keep]
        return np_delete_impl

    else: # scalar value
        if not isinstance(obj, types.Integer):
            raise TypingError('obj should be of Integer dtype')

        def np_delete_scalar_impl(arr, obj):
            arr = np.ravel(np.asarray(arr))
            N = arr.size
            pos = obj

            if (pos < -N or pos >= N):
                raise IndexError('obj must be less than the len(arr)')
                # NumPy raises IndexError: index 'i' is out of
                # bounds for axis 'x' with size 'n'

            if (pos < 0):
                pos += N

            return np.concatenate((arr[:pos], arr[pos+1:]))
        return np_delete_scalar_impl


@overload(np.diff)
def np_diff_impl(a, n=1):
    if not isinstance(a, types.Array) or a.ndim == 0:
        return

    def diff_impl(a, n=1):
        if n == 0:
            return a.copy()
        if n < 0:
            raise ValueError("diff(): order must be non-negative")
        size = a.shape[-1]
        out_shape = a.shape[:-1] + (max(size - n, 0),)
        out = np.empty(out_shape, a.dtype)
        if out.size == 0:
            return out

        # np.diff() works on each last dimension subarray independently.
        # To make things easier, normalize input and output into 2d arrays
        a2 = a.reshape((-1, size))
        out2 = out.reshape((-1, out.shape[-1]))
        # A scratchpad for subarrays
        work = np.empty(size, a.dtype)

        for major in range(a2.shape[0]):
            # First iteration: diff a2 into work
            for i in range(size - 1):
                work[i] = a2[major, i + 1] - a2[major, i]
            # Other iterations: diff work into itself
            for niter in range(1, n):
                for i in range(size - niter - 1):
                    work[i] = work[i + 1] - work[i]
            # Copy final diff into out2
            out2[major] = work[:size - n]

        return out

    return diff_impl


def validate_1d_array_like(func_name, seq):
    if isinstance(seq, types.Array):
        if seq.ndim != 1:
            raise TypeError("{0}(): input should have dimension 1"
                            .format(func_name))
    elif not isinstance(seq, types.Sequence):
        raise TypeError("{0}(): input should be an array or sequence"
                        .format(func_name))


@overload(np.bincount)
def np_bincount(a, weights=None):
    validate_1d_array_like("bincount", a)
    if not isinstance(a.dtype, types.Integer):
        return

    if weights not in (None, types.none):
        validate_1d_array_like("bincount", weights)
        # weights is promoted to double in C impl
        # https://github.com/numpy/numpy/blob/maintenance/1.16.x/numpy/core/src/multiarray/compiled_base.c#L93-L95
        out_dtype = np.float64

        @register_jitable
        def validate_inputs(a, weights):
            if len(a) != len(weights):
                raise ValueError("bincount(): weights and list don't have the same length")

        @register_jitable
        def count_item(out, idx, val, weights):
            out[val] += weights[idx]

    else:
        out_dtype = types.intp

        @register_jitable
        def validate_inputs(a, weights):
            pass

        @register_jitable
        def count_item(out, idx, val, weights):
            out[val] += 1

    def bincount_impl(a, weights=None):
        validate_inputs(a, weights)
        n = len(a)

        a_max = a[0] if n > 0 else -1
        for i in range(1, n):
            if a[i] < 0:
                raise ValueError("bincount(): first argument must be non-negative")
            a_max = max(a_max, a[i])

        out = np.zeros(a_max + 1, out_dtype)
        for i in range(n):
            count_item(out, i, a[i], weights)
        return out

    return bincount_impl

def _searchsorted(func):
    def searchsorted_inner(a, v):
        n = len(a)
        if np.isnan(v):
            # Find the first nan (i.e. the last from the end of a,
            # since there shouldn't be many of them in practice)
            for i in range(n, 0, -1):
                if not np.isnan(a[i - 1]):
                    return i
            return 0
        lo = 0
        hi = n
        while hi > lo:
            mid = (lo + hi) >> 1
            if func(a[mid], (v)):
                # mid is too low => go up
                lo = mid + 1
            else:
                # mid is too high, or is a NaN => go down
                hi = mid
        return lo
    return searchsorted_inner

_lt = less_than
_le = register_jitable(lambda x, y: x <= y)
_searchsorted_left = register_jitable(_searchsorted(_lt))
_searchsorted_right = register_jitable(_searchsorted(_le))

@overload(np.searchsorted)
def searchsorted(a, v, side='left'):
    side_val = getattr(side, 'literal_value', side)
    if side_val == 'left':
        loop_impl = _searchsorted_left
    elif side_val == 'right':
        loop_impl = _searchsorted_right
    else:
        raise ValueError("Invalid value given for 'side': %s" % side_val)

    if isinstance(v, types.Array):
        # N-d array and output
        def searchsorted_impl(a, v, side='left'):
            out = np.empty(v.shape, np.intp)
            for view, outview in np.nditer((v, out)):
                index = loop_impl(a, view.item())
                outview.itemset(index)
            return out

    elif isinstance(v, types.Sequence):
        # 1-d sequence and output
        def searchsorted_impl(a, v, side='left'):
            out = np.empty(len(v), np.intp)
            for i in range(len(v)):
                out[i] = loop_impl(a, v[i])
            return out
    else:
        # Scalar value and output
        # Note: NaNs come last in Numpy-sorted arrays
        def searchsorted_impl(a, v, side='left'):
            return loop_impl(a, v)

    return searchsorted_impl

@overload(np.digitize)
def np_digitize(x, bins, right=False):
    @register_jitable
    def are_bins_increasing(bins):
        n = len(bins)
        is_increasing = True
        is_decreasing = True
        if n > 1:
            prev = bins[0]
            for i in range(1, n):
                cur = bins[i]
                is_increasing = is_increasing and not prev > cur
                is_decreasing = is_decreasing and not prev < cur
                if not is_increasing and not is_decreasing:
                    raise ValueError("bins must be monotonically increasing or decreasing")
                prev = cur
        return is_increasing

    # NOTE: the algorithm is slightly different from searchsorted's,
    # as the edge cases (bin boundaries, NaN) give different results.

    @register_jitable
    def digitize_scalar(x, bins, right):
        # bins are monotonically-increasing
        n = len(bins)
        lo = 0
        hi = n

        if right:
            if np.isnan(x):
                # Find the first nan (i.e. the last from the end of bins,
                # since there shouldn't be many of them in practice)
                for i in range(n, 0, -1):
                    if not np.isnan(bins[i - 1]):
                        return i
                return 0
            while hi > lo:
                mid = (lo + hi) >> 1
                if bins[mid] < x:
                    # mid is too low => narrow to upper bins
                    lo = mid + 1
                else:
                    # mid is too high, or is a NaN => narrow to lower bins
                    hi = mid
        else:
            if np.isnan(x):
                # NaNs end up in the last bin
                return n
            while hi > lo:
                mid = (lo + hi) >> 1
                if bins[mid] <= x:
                    # mid is too low => narrow to upper bins
                    lo = mid + 1
                else:
                    # mid is too high, or is a NaN => narrow to lower bins
                    hi = mid

        return lo

    @register_jitable
    def digitize_scalar_decreasing(x, bins, right):
        # bins are monotonically-decreasing
        n = len(bins)
        lo = 0
        hi = n

        if right:
            if np.isnan(x):
                # Find the last nan
                for i in range(0, n):
                    if not np.isnan(bins[i]):
                        return i
                return n
            while hi > lo:
                mid = (lo + hi) >> 1
                if bins[mid] < x:
                    # mid is too high => narrow to lower bins
                    hi = mid
                else:
                    # mid is too low, or is a NaN => narrow to upper bins
                    lo = mid + 1
        else:
            if np.isnan(x):
                # NaNs end up in the first bin
                return 0
            while hi > lo:
                mid = (lo + hi) >> 1
                if bins[mid] <= x:
                    # mid is too high => narrow to lower bins
                    hi = mid
                else:
                    # mid is too low, or is a NaN => narrow to upper bins
                    lo = mid + 1

        return lo

    if isinstance(x, types.Array):
        # N-d array and output

        def digitize_impl(x, bins, right=False):
            is_increasing = are_bins_increasing(bins)
            out = np.empty(x.shape, np.intp)
            for view, outview in np.nditer((x, out)):
                if is_increasing:
                    index = digitize_scalar(view.item(), bins, right)
                else:
                    index = digitize_scalar_decreasing(view.item(), bins, right)
                outview.itemset(index)
            return out

        return digitize_impl

    elif isinstance(x, types.Sequence):
        # 1-d sequence and output

        def digitize_impl(x, bins, right=False):
            is_increasing = are_bins_increasing(bins)
            out = np.empty(len(x), np.intp)
            for i in range(len(x)):
                if is_increasing:
                    out[i] = digitize_scalar(x[i], bins, right)
                else:
                    out[i] = digitize_scalar_decreasing(x[i], bins, right)
            return out

        return digitize_impl


_range = range

@overload(np.histogram)
def np_histogram(a, bins=10, range=None):
    if isinstance(bins, (int, types.Integer)):
        # With a uniform distribution of bins, use a fast algorithm
        # independent of the number of bins

        if range in (None, types.none):
            inf = float('inf')
            def histogram_impl(a, bins=10, range=None):
                bin_min = inf
                bin_max = -inf
                for view in np.nditer(a):
                    v = view.item()
                    if bin_min > v:
                        bin_min = v
                    if bin_max < v:
                        bin_max = v
                return np.histogram(a, bins, (bin_min, bin_max))

        else:
            def histogram_impl(a, bins=10, range=None):
                if bins <= 0:
                    raise ValueError("histogram(): `bins` should be a positive integer")
                bin_min, bin_max = range
                if not bin_min <= bin_max:
                    raise ValueError("histogram(): max must be larger than min in range parameter")

                hist = np.zeros(bins, np.intp)
                if bin_max > bin_min:
                    bin_ratio = bins / (bin_max - bin_min)
                    for view in np.nditer(a):
                        v = view.item()
                        b = math.floor((v - bin_min) * bin_ratio)
                        if 0 <= b < bins:
                            hist[int(b)] += 1
                        elif v == bin_max:
                            hist[bins - 1] += 1

                bins_array = np.linspace(bin_min, bin_max, bins + 1)
                return hist, bins_array

    else:
        # With a custom bins array, use a bisection search

        def histogram_impl(a, bins=10, range=None):
            nbins = len(bins) - 1
            for i in _range(nbins):
                # Note this also catches NaNs
                if not bins[i] <= bins[i + 1]:
                    raise ValueError("histogram(): bins must increase monotonically")

            bin_min = bins[0]
            bin_max = bins[nbins]
            hist = np.zeros(nbins, np.intp)

            if nbins > 0:
                for view in np.nditer(a):
                    v = view.item()
                    if not bin_min <= v <= bin_max:
                        # Value is out of bounds, ignore (this also catches NaNs)
                        continue
                    # Bisect in bins[:-1]
                    lo = 0
                    hi = nbins - 1
                    while lo < hi:
                        # Note the `+ 1` is necessary to avoid an infinite
                        # loop where mid = lo => lo = mid
                        mid = (lo + hi + 1) >> 1
                        if v < bins[mid]:
                            hi = mid - 1
                        else:
                            lo = mid
                    hist[lo] += 1

            return hist, bins

    return histogram_impl


# Create np.finfo, np.iinfo and np.MachAr
# machar
_mach_ar_supported = ('ibeta', 'it', 'machep', 'eps', 'negep', 'epsneg',
                      'iexp', 'minexp', 'xmin', 'maxexp', 'xmax', 'irnd',
                      'ngrd', 'epsilon', 'tiny', 'huge', 'precision',
                      'resolution',)
MachAr = namedtuple('MachAr', _mach_ar_supported)

# Do not support MachAr field
# finfo
_finfo_supported = ('eps', 'epsneg', 'iexp', 'machep', 'max', 'maxexp', 'min',
                    'minexp', 'negep', 'nexp', 'nmant', 'precision',
                    'resolution', 'tiny',)
if numpy_version >= (1, 12):
    _finfo_supported = ('bits',) + _finfo_supported

finfo = namedtuple('finfo', _finfo_supported)

# iinfo
_iinfo_supported = ('min', 'max')
if numpy_version >= (1, 12):
    _iinfo_supported = _iinfo_supported + ('bits',)

iinfo = namedtuple('iinfo', _iinfo_supported)

@overload(np.MachAr)
def MachAr_impl():
    f = np.MachAr()
    _mach_ar_data = tuple([getattr(f, x) for x in _mach_ar_supported])
    def impl():
        return MachAr(*_mach_ar_data)
    return impl

def generate_xinfo(np_func, container, attr):
    @overload(np_func)
    def xinfo_impl(arg):
        nbty = getattr(arg, 'dtype', arg)
        f = np_func(as_dtype(nbty))
        data = tuple([getattr(f, x) for x in attr])
        def impl(arg):
            return container(*data)
        return impl

generate_xinfo(np.finfo, finfo, _finfo_supported)
generate_xinfo(np.iinfo, iinfo, _iinfo_supported)

def _get_inner_prod(dta, dtb):
    # gets an inner product implementation, if both types are float then
    # BLAS is used else a local function

    @register_jitable
    def _innerprod(a, b):
        acc = 0
        for i in range(len(a)):
            acc = acc + a[i] * b[i]
        return acc

    # no BLAS... use local function regardless
    if not _HAVE_BLAS:
        return _innerprod

    flty = types.real_domain | types.complex_domain
    floats = dta in flty and dtb in flty
    if not floats:
        return _innerprod
    else:
        a_dt = as_dtype(dta)
        b_dt = as_dtype(dtb)
        dt = np.promote_types(a_dt, b_dt)

        @register_jitable
        def _dot_wrap(a, b):
            return np.dot(a.astype(dt), b.astype(dt))
        return _dot_wrap

def _assert_1d(a, func_name):
    if isinstance(a, types.Array):
        if not a.ndim <= 1:
            raise TypingError("%s() only supported on 1D arrays " % func_name)

def _np_correlate_core(ap1, ap2, mode, direction):
    pass


class _corr_conv_Mode(IntEnum):
    """
    Enumerated modes for correlate/convolve as per:
    https://github.com/numpy/numpy/blob/ac6b1a902b99e340cf7eeeeb7392c91e38db9dd8/numpy/core/numeric.py#L862-L870
    """
    VALID = 0
    SAME = 1
    FULL = 2


@overload(_np_correlate_core)
def _np_correlate_core_impl(ap1, ap2, mode, direction):
    a_dt = as_dtype(ap1.dtype)
    b_dt = as_dtype(ap2.dtype)
    dt = np.promote_types(a_dt, b_dt)
    innerprod = _get_inner_prod(ap1.dtype, ap2.dtype)

    Mode = _corr_conv_Mode

    def impl(ap1, ap2, mode, direction):
        # Implementation loosely based on `_pyarray_correlate` from
        # https://github.com/numpy/numpy/blob/3bce2be74f228684ca2895ad02b63953f37e2a9d/numpy/core/src/multiarray/multiarraymodule.c#L1191
        # For "Mode":
        # Convolve uses 'full' by default, this is denoted by the number 2
        # Correlate uses 'valid' by default, this is denoted by the number 0
        # For "direction", +1 to write the return values out in order 0->N
        # -1 to write them out N->0.

        if not (mode == Mode.VALID or mode == Mode.FULL):
            raise ValueError("Invalid mode")

        n1 = len(ap1)
        n2 = len(ap2)
        length = n1
        n = n2
        if mode == Mode.VALID: # mode == valid == 0, correlate default
            length = length - n + 1
            n_left = 0
            n_right = 0
        elif mode == Mode.FULL: # mode == full == 2, convolve default
            n_right = n - 1
            n_left = n - 1
            length = length + n - 1
        else:
            raise ValueError("Invalid mode")

        ret = np.zeros(length, dt)
        n = n - n_left

        if direction == 1:
            idx = 0
            inc = 1
        elif direction == -1:
            idx = length - 1
            inc = -1
        else:
            raise ValueError("Invalid direction")

        for i in range(n_left):
            ret[idx] = innerprod(ap1[:idx + 1], ap2[-(idx + 1):])
            idx = idx + inc

        for i in range(n1 - n2 + 1):
            ret[idx] = innerprod(ap1[i : i + n2], ap2)
            idx = idx + inc

        for i in range(n_right, 0, -1):
            ret[idx] = innerprod(ap1[-i:], ap2[:i])
            idx = idx + inc
        return ret

    return impl

@overload(np.correlate)
def _np_correlate(a, v):
    _assert_1d(a, 'np.correlate')
    _assert_1d(v, 'np.correlate')

    @register_jitable
    def op_conj(x):
        return np.conj(x)

    @register_jitable
    def op_nop(x):
        return x

    Mode = _corr_conv_Mode

    if a.dtype in types.complex_domain:
        if v.dtype in types.complex_domain:
            a_op = op_nop
            b_op = op_conj
        else:
            a_op = op_nop
            b_op = op_nop
    else:
        if v.dtype in types.complex_domain:
            a_op = op_nop
            b_op = op_conj
        else:
            a_op = op_conj
            b_op = op_nop

    def impl(a, v):
        if len(a) < len(v):
            return _np_correlate_core(b_op(v), a_op(a), Mode.VALID, -1)
        else:
            return _np_correlate_core(a_op(a), b_op(v), Mode.VALID, 1)

    return impl

@overload(np.convolve)
def np_convolve(a, v):
    _assert_1d(a, 'np.convolve')
    _assert_1d(v, 'np.convolve')

    Mode = _corr_conv_Mode

    def impl(a, v):
        la = len(a)
        lv = len(v)

        if la == 0:
            raise ValueError("'a' cannot be empty")
        if lv == 0:
            raise ValueError("'v' cannot be empty")

        if la < lv:
            return _np_correlate_core(v, a[::-1], Mode.FULL, 1)
        else:
            return _np_correlate_core(a, v[::-1], Mode.FULL, 1)

    return impl

def _is_nonelike(ty):
    return (ty is None) or isinstance(ty, types.NoneType)

@overload(np.asarray)
def np_asarray(a, dtype=None):

    # developer note... keep this function (type_can_asarray) in sync with the
    # accepted types implementations below!
    if not type_can_asarray(a):
        return None

    impl = None
    if isinstance(a, types.Array):
        if _is_nonelike(dtype) or a.dtype == dtype.dtype:
            def impl(a, dtype=None):
                return a
        else:
            def impl(a, dtype=None):
                return a.astype(dtype)
    elif isinstance(a, (types.Sequence, types.Tuple)):
        # Nested lists cannot be unpacked, therefore only single lists are
        # permitted and these conform to Sequence and can be unpacked along on
        # the same path as Tuple.
        if _is_nonelike(dtype):
            def impl(a, dtype=None):
                return np.array(a)
        else:
            def impl(a, dtype=None):
                return np.array(a, dtype)
    elif isinstance(a, (types.Number, types.Boolean)):
        dt_conv = a if _is_nonelike(dtype) else dtype
        ty = as_dtype(dt_conv)
        def impl(a, dtype=None):
                return np.array(a, ty)

    return impl

@overload(np.extract)
def np_extract(condition, arr):

    def np_extract_impl(condition, arr):
        cond = np.asarray(condition).flatten()
        a = np.asarray(arr)

        if a.size == 0:
            raise ValueError('Cannot extract from an empty array')

        # the following looks odd but replicates NumPy...
        # https://github.com/numpy/numpy/issues/12859
        if np.any(cond[a.size:]) and cond.size > a.size:
            msg = 'condition shape inconsistent with arr shape'
            raise ValueError(msg)
            # NumPy raises IndexError: index 'm' is out of
            # bounds for size 'n'

        max_len = min(a.size, cond.size)
        out = [a.flat[idx] for idx in range(max_len) if cond[idx]]

        return np.array(out)

    return np_extract_impl

#----------------------------------------------------------------------------
# Windowing functions
#   - translated from the numpy implementations found in:
#   https://github.com/numpy/numpy/blob/v1.16.1/numpy/lib/function_base.py#L2543-L3233
#   at commit: f1c4c758e1c24881560dd8ab1e64ae750

@register_jitable
def np_bartlett_impl(M):
    n = np.arange(M)
    return np.where(np.less_equal(n, (M - 1) / 2.0), 2.0 * n / (M - 1),
            2.0 - 2.0 * n / (M - 1))


@register_jitable
def np_blackman_impl(M):
    n = np.arange(M)
    return (0.42 - 0.5 * np.cos(2.0 * np.pi * n / (M - 1)) +
            0.08 * np.cos(4.0* np.pi * n / (M - 1)))


@register_jitable
def np_hamming_impl(M):
    n = np.arange(M)
    return 0.54 - 0.46 * np.cos(2.0 * np.pi * n / (M - 1))


@register_jitable
def np_hanning_impl(M):
    n = np.arange(M)
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * n / (M - 1))


def window_generator(func):
    def window_overload(M):
        if not isinstance(M, types.Integer):
            raise TypingError('M must be an integer')

        def window_impl(M):

            if M < 1:
                return np.array((), dtype=np.float_)
            if M == 1:
                return np.ones(1, dtype=np.float_)
            return func(M)

        return window_impl
    return window_overload

overload(np.bartlett)(window_generator(np_bartlett_impl))
overload(np.blackman)(window_generator(np_blackman_impl))
overload(np.hamming)(window_generator(np_hamming_impl))
overload(np.hanning)(window_generator(np_hanning_impl))


_i0A = np.array([
    -4.41534164647933937950E-18,
    3.33079451882223809783E-17,
    -2.43127984654795469359E-16,
    1.71539128555513303061E-15,
    -1.16853328779934516808E-14,
    7.67618549860493561688E-14,
    -4.85644678311192946090E-13,
    2.95505266312963983461E-12,
    -1.72682629144155570723E-11,
    9.67580903537323691224E-11,
    -5.18979560163526290666E-10,
    2.65982372468238665035E-9,
    -1.30002500998624804212E-8,
    6.04699502254191894932E-8,
    -2.67079385394061173391E-7,
    1.11738753912010371815E-6,
    -4.41673835845875056359E-6,
    1.64484480707288970893E-5,
    -5.75419501008210370398E-5,
    1.88502885095841655729E-4,
    -5.76375574538582365885E-4,
    1.63947561694133579842E-3,
    -4.32430999505057594430E-3,
    1.05464603945949983183E-2,
    -2.37374148058994688156E-2,
    4.93052842396707084878E-2,
    -9.49010970480476444210E-2,
    1.71620901522208775349E-1,
    -3.04682672343198398683E-1,
    6.76795274409476084995E-1
    ])

_i0B = np.array([
    -7.23318048787475395456E-18,
    -4.83050448594418207126E-18,
    4.46562142029675999901E-17,
    3.46122286769746109310E-17,
    -2.82762398051658348494E-16,
    -3.42548561967721913462E-16,
    1.77256013305652638360E-15,
    3.81168066935262242075E-15,
    -9.55484669882830764870E-15,
    -4.15056934728722208663E-14,
    1.54008621752140982691E-14,
    3.85277838274214270114E-13,
    7.18012445138366623367E-13,
    -1.79417853150680611778E-12,
    -1.32158118404477131188E-11,
    -3.14991652796324136454E-11,
    1.18891471078464383424E-11,
    4.94060238822496958910E-10,
    3.39623202570838634515E-9,
    2.26666899049817806459E-8,
    2.04891858946906374183E-7,
    2.89137052083475648297E-6,
    6.88975834691682398426E-5,
    3.36911647825569408990E-3,
    8.04490411014108831608E-1
    ])


@register_jitable
def _chbevl(x, vals):
    b0 = vals[0]
    b1 = 0.0

    for i in range(1, len(vals)):
        b2 = b1
        b1 = b0
        b0 = x * b1 - b2 + vals[i]

    return 0.5 * (b0 - b2)


@register_jitable
def _i0(x):
    if x < 0:
        x = -x
    if x <= 8.0:
        y = (0.5 * x) - 2.0
        return np.exp(x) * _chbevl(y, _i0A)

    return np.exp(x) * _chbevl(32.0 / x - 2.0, _i0B) / np.sqrt(x)


@register_jitable
def _i0n(n, alpha, beta):
    y = np.empty_like(n, dtype=np.float_)
    t = _i0(np.float_(beta))
    for i in range(len(y)):
        y[i] = _i0(beta * np.sqrt(1 - ((n[i] - alpha) / alpha)**2.0)) / t

    return y


@overload(np.kaiser)
def np_kaiser(M, beta):
    if not isinstance(M, types.Integer):
        raise TypingError('M must be an integer')

    if not isinstance(beta, (types.Integer, types.Float)):
        raise TypingError('beta must be an integer or float')

    def np_kaiser_impl(M, beta):
        if M < 1:
            return np.array((), dtype=np.float_)
        if M == 1:
            return np.ones(1, dtype=np.float_)

        n = np.arange(0, M)
        alpha = (M - 1) / 2.0

        return _i0n(n, alpha, beta)

    return np_kaiser_impl
