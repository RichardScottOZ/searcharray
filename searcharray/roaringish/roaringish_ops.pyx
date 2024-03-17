# These are modified versions of sortednp:
#   https://gitlab.sauerburger.com/frank/sortednp
# cython: boundscheck=False
# cython: wraparound=False
# cython: initializedcheck=False
# cython: cdivision=True
# cython: nonecheck=False
# cython: language_level=3
cimport numpy as np
import numpy as np
from enum import Enum

cimport searcharray.roaringish.snp_ops
from searcharray.roaringish.snp_ops cimport _galloping_search, DTYPE_t


cdef DTYPE_t ALL_BITS = 0xFFFFFFFFFFFFFFFF


cdef extern from "stddef.h":
    # Assuming size_t is available via stddef.h for the example's simplicity
    # and portability, though it's not directly used here.
    int __builtin_popcountll(unsigned long long x)

# Popcount reduce key-value pair
# for words 0xKKKKKKKK...KKKKVVVV...VVVV
# Returning two parallel arrays:
#   - keys   - the keys
#   - values - the popcount of the values with those keys
cdef _popcount64_reduce(DTYPE_t[:] arr,
                        DTYPE_t key_shift,
                        DTYPE_t value_mask):
    cdef float[:] popcounts = np.zeros(arr.shape[0], dtype=np.float32)
    cdef DTYPE_t[:] keys = np.empty(arr.shape[0], dtype=np.uint64)
    # cdef int i = 0
    cdef float* popcounts_ptr = &popcounts[0]
    cdef DTYPE_t* keys_ptr = &keys[0]
    cdef DTYPE_t* arr_ptr = &arr[0]
    cdef DTYPE_t last_key = 0xFFFFFFFFFFFFFFFF

    for _ in range(arr.shape[0]):
        popcounts_ptr[0] += __builtin_popcountll(arr_ptr[0] & value_mask)
        if arr_ptr[0] >> key_shift != last_key:
            last_key = arr_ptr[0] >> key_shift
            keys_ptr[0] = last_key
            popcounts_ptr += 1
            keys_ptr += 1
        arr_ptr += 1
    return keys, popcounts, keys_ptr - &keys[0]


def popcount64_reduce(arr, key_shift, value_mask):
    cdef DTYPE_t[:] arr_view = arr
    keys, popcounts, results_idx = _popcount64_reduce(arr_view, key_shift, value_mask)
    return np.array(keys[:results_idx]), np.array(popcounts[:results_idx])


cdef _payload_slice(DTYPE_t[:] arr,
                    DTYPE_t payload_msb_mask,
                    DTYPE_t min_payload,
                    DTYPE_t max_payload):
    cdef DTYPE_t[:] sliced = np.zeros(arr.shape[0], dtype=np.uint64)
    cdef DTYPE_t* sliced_ptr = &sliced[0]
    cdef DTYPE_t* arr_ptr = &arr[0]

    while arr_ptr < &arr[arr.shape[0]]:
        if (arr_ptr[0] & payload_msb_mask) >= min_payload and (arr_ptr[0] & payload_msb_mask) <= max_payload:
            sliced_ptr[0] = arr_ptr[0]
            sliced_ptr += 1
        arr_ptr += 1

    return sliced, sliced_ptr - &sliced[0]


def payload_slice(arr, payload_msb_mask,
                  min_payload=0, max_payload=0xFFFFFFFFFFFFFFFF):
    cdef DTYPE_t[:] arr_view = arr
    sliced, sliced_len = _payload_slice(arr_view, payload_msb_mask,
                                        min_payload, max_payload)
    return np.array(sliced[:sliced_len])


cdef _as_dense_array(DTYPE_t[:] indices,  # Its likely these indices are sorted, if that helps
                     float[:] values,
                     DTYPE_t size):

    cdef DTYPE_t* indices_ptr = &indices[0]
    cdef float* values_ptr = &values[0]
    cdef float[:] arr = np.zeros(size, dtype=np.float32)

    while indices_ptr < &indices[indices.shape[0]]:
        arr[indices_ptr[0]] = values_ptr[0]
        indices_ptr += 1
        values_ptr += 1

    return arr


def as_dense(indices, values, size):
    cdef DTYPE_t[:] indices_view = indices
    cdef float[:] values_view = values
    if len(indices) != len(values):
        raise ValueError("indices and values must have the same length")
    return np.array(_as_dense_array(indices_view, values_view, size))
