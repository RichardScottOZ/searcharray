"""Roaring-ish bit array for storing sorted integers in numpy array.

See - https://softwaredoug.com/blog/2024/01/21/search-array-phrase-algorithm
"""
import numpy as np
import sortednp as snp
import logging
import numbers
from typing import Optional, Tuple, List, Union

from searcharray.utils.snp_ops import intersect, unique

logger = logging.getLogger(__name__)

# When running in pytest
import sys  # noqa
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.ERROR)
formatter = logging.Formatter("[%(filename)s:%(lineno)s - %(funcName)20s() ] %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.ERROR)


DEFAULT_KEY_MASK = np.uint64(0xFFFFFFF000000000)
DEFAULT_KEY_BITS = np.uint64(28)
DEFAULT_PAYLOAD_MSB_MASK = np.uint64(0x0000000FFFFC0000)
DEFAULT_PAYLOAD_MSB_BITS = np.uint64(18)
DEFAULT_PAYLOAD_LSB_MASK = np.uint64(0x000000000003FFFF)
DEFAULT_PAYLOAD_LSB_BITS = np.uint64(18)

# To not constantly type coerce
_64 = np.uint64(64)
_2 = np.uint64(2)
_1 = np.uint64(1)
_0 = np.uint64(0)
_neg1 = np.int64(-1)


_algorithm = snp.GALLOPING_SEARCH


def n_msb_mask(n: np.uint64) -> np.uint64:
    """Return the n most significant bits of num."""
    return np.uint64(~(np.uint64(_1 << (_64 - n))) + _1)


def sorted_unique(arr: np.ndarray) -> np.ndarray:
    return unique(arr)


class RoaringishEncoder:
    """An encoder for key->integer sets as a numpy array.

    Each returned array represents a single term, with key as MSBS, ie:

        | 32 MSBs | 16 LSBs   | 16 LSBs |
          key     | bits msbs | payload

    (different number of MSBs / payload bits can be specified)

    """

    def __init__(self, key_bits: np.uint64 = DEFAULT_KEY_BITS):
        payload_bits = _64 - key_bits
        self.payload_msb_bits = payload_bits // _2
        self.payload_lsb_bits = np.uint64(payload_bits - self.payload_msb_bits)
        self.key_bits = key_bits
        assert self.key_bits.dtype == np.uint64
        # key bits MSB of 64 bits
        self.key_mask = n_msb_mask(key_bits)
        self.payload_msb_mask = n_msb_mask(np.uint64(self.payload_msb_bits + key_bits)) & ~self.key_mask
        assert self.payload_msb_bits.dtype == np.uint64, f"MSB bits dtype was {self.payload_msb_bits.dtype}"
        assert self.payload_msb_mask.dtype == np.uint64, f"MSB mask dtype was {self.payload_msb_mask.dtype}"
        self.payload_lsb_mask = (_1 << self.payload_lsb_bits) - np.uint64(1)
        assert self.payload_lsb_bits.dtype == np.uint64, f"LSB bits dtype was {self.payload_lsb_bits.dtype}"
        assert self.payload_lsb_mask.dtype == np.uint64, f"LSB mask dtype was {self.payload_lsb_mask.dtype}"
        self.header_mask = self.key_mask | self.payload_msb_mask
        if key_bits == DEFAULT_KEY_BITS:
            assert self.key_mask == DEFAULT_KEY_MASK
            assert self.payload_msb_mask == DEFAULT_PAYLOAD_MSB_MASK
            assert self.payload_lsb_mask == DEFAULT_PAYLOAD_LSB_MASK
        self.max_payload = np.uint64(2**self.payload_lsb_bits - 1)

    def validate_payload(self, payload: np.ndarray):
        """Optional validation of payload."""
        if np.any(payload > self.max_payload):
            raise ValueError(f"Positions must be less than {2**self.payload_lsb_bits}")

    def encode(self, payload: np.ndarray,
               keys: Optional[np.ndarray] = None,
               boundaries: Optional[np.ndarray] = None) -> Tuple[np.ndarray, Optional[np.ndarray]]:
        """Pack a sorted array of integers into compact bit numpy array.

        each returned array represents a single term, with key as MSBS, ie:

        | 32 MSBs | 16 LSBs | 16 LSBs |
          key     | bits msbs| payload

        for later easy intersection of 32+16 msbs, then checking for adjacent
        positions

        If boundaries are provided, then we consider multiple distinct payloads
        being encoded simultaneously, and we return the boundaries of each

        """
        cols = np.floor_divide(payload, self.payload_lsb_bits, dtype=np.uint64)    # Header of bit to use
        cols <<= self.payload_msb_bits
        if keys is not None:
            cols |= keys.astype(np.uint64) << (_64 - self.key_bits)
        values = payload % self.payload_lsb_bits   # Value to encode

        change_indices_one_doc = np.nonzero(np.diff(cols))[0] + 1
        change_indices_one_doc = np.concatenate([[0], change_indices_one_doc])
        if boundaries is not None:
            change_indices = snp.merge(change_indices_one_doc, boundaries,
                                       duplicates=snp.DROP)
            new_boundaries = snp.intersect(boundaries, change_indices, indices=True)[1][1]
            new_boundaries = np.concatenate([new_boundaries, [len(change_indices)]])
        else:
            change_indices = change_indices_one_doc
            new_boundaries = None

        # 0 as a position, goes in bit 1,
        # 1 as a position, goes in bit 2, etc
        values = _1 << values
        cols |= values
        encoded = cols
        if len(encoded) == 0:
            return encoded, new_boundaries
        reduced = np.bitwise_or.reduceat(encoded, change_indices)
        return reduced, new_boundaries

    def decode(self, encoded: np.ndarray, get_keys: bool = True) -> Union[List[Tuple[np.uint64, np.ndarray]], List[np.ndarray]]:
        """Decode an encoded bit array into keys / payloads."""
        keys = (encoded & self.key_mask) >> (_64 - self.key_bits)
        msbs = (encoded & self.payload_msb_mask) >> self.payload_msb_bits
        to_concat = []
        for bit in range(self.payload_lsb_bits):
            mask = 1 << bit
            lsbs = encoded & mask
            set_lsbs = (lsbs != 0)
            this_keys = keys[set_lsbs]
            payload = bit + (msbs[set_lsbs] * self.payload_lsb_bits)
            doc_with_posn = np.dstack([this_keys, payload])[0]
            to_concat.append(doc_with_posn)

        stacked = np.vstack(to_concat)
        # Sort by doc_id, then posn
        sorted_payload = stacked[np.lexsort((stacked[:, 1], stacked[:, 0]))]
        keys, idx = np.unique(sorted_payload[:, 0], return_index=True)
        grouped = np.split(sorted_payload[:, 1], idx[1:])
        if get_keys:
            return list(zip(keys, grouped))
        else:
            return grouped

    def keys(self, encoded: np.ndarray) -> np.ndarray:
        """Return keys from encoded."""
        return encoded >> (_64 - self.key_bits)

    def keys_unique(self, encoded: np.ndarray) -> np.ndarray:
        """Return keys from encoded."""
        keys = self.keys(encoded)
        intersected = sorted_unique(keys)
        return intersected

    def payload_msb(self, encoded: np.ndarray) -> np.ndarray:
        """Return payload MSBs from encoded."""
        return (encoded & self.payload_msb_mask) >> self.payload_msb_bits

    def payload_lsb(self, encoded: np.ndarray) -> np.ndarray:
        """Return payload LSBs from encoded."""
        return encoded & self.payload_lsb_mask

    def header(self, encoded: np.ndarray) -> np.ndarray:
        """Return header from encoded -- all but lsb bits."""
        return encoded & ~self.payload_lsb_mask

    def _no_underflow_mask(self, rhs: np.ndarray, rshift: np.int64 = _neg1):
        mask = None
        if rshift == _neg1:
            mask = (rhs & self.payload_msb_mask) != 0
        else:
            mask = self.payload_msb(rhs) >= np.abs(rshift)
        return mask

    def _actual_shift(self, rhs: np.ndarray, rshift: np.int64 = _neg1):
        return rhs + (rshift.view(np.uint64) << self.payload_lsb_bits)

    def _rhs_shifted(self, rhs: np.ndarray, rshift: np.int64 = _neg1):
        assert rshift < 0, "rshift must be negative"
        # Prevent overflow by removing the MSBs that will be shifted off
        mask = self._no_underflow_mask(rhs, rshift)
        rhs_int = rhs[mask]
        rhs_shifted = self._actual_shift(rhs_int, rshift)
        return rhs_int, rhs_shifted

    def intersect_rshift(self, lhs: np.ndarray, rhs: np.ndarray,
                         rshift: np.int64 = _neg1) -> Tuple[np.ndarray, np.ndarray]:
        """Return the MSBs that are common to both lhs and rhs (same keys, same MSBs)

        Parameters
        ----------
        lhs : np.ndarray of uint64 (encoded) values
        rhs : np.ndarray of uint64 (encoded) values
        rshift : int how much to shift rhs by to the right
        """
        rhs_int, rhs_shifted = self._rhs_shifted(rhs, rshift)
        # assert np.all(np.diff(rhs_shifted) >= 0), "not sorted"
        # _, (lhs_idx, rhs_idx) = snp.intersect(lhs >> self.payload_lsb_bits,
        #                                       rhs_shifted,
        #                                      indices=True,
        #                                       algorithm=_algorithm)
        _, lhs_idx, rhs_idx = intersect(lhs, rhs_shifted, mask=self.header_mask)
        return lhs[lhs_idx], rhs_int[rhs_idx]

    def intersect(self, lhs: np.ndarray, rhs: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Return the MSBs that are common to both lhs and rhs (same keys, same MSBs)

        Parameters
        ----------
        lhs : np.ndarray of uint64 (encoded) values
        rhs : np.ndarray of uint64 (encoded) values
        """
        # assert np.all(np.diff(rhs_shifted) >= 0), "not sorted"
        _, lhs_idx, rhs_idx = intersect(lhs, rhs, mask=self.header_mask)
        # _, (lhs_idx, rhs_idx) = snp.intersect(lhs >> self.payload_lsb_bits,
        #                                       rhs >> self.payload_lsb_bits,
        #                                       indices=True,
        #                                      algorithm=_algorithm)
        return lhs[lhs_idx], rhs[rhs_idx]

    def slice(self, encoded: np.ndarray,
              keys: np.ndarray,
              max_payload: Optional[int] = None,
              min_payload: Optional[int] = None) -> np.ndarray:
        """Get list of encoded that have values in keys."""
        # encoded_keys = encoded.view(np.uint64) >> (_64 - self.key_bits)
        encoded_keys = self.keys(encoded)
        _, (idx_docs, idx_enc) = snp.intersect(keys, encoded_keys, indices=True,
                                               duplicates=snp.KEEP_MAX_N,
                                               algorithm=_algorithm)

        if max_payload is None and min_payload is None:
            return encoded[idx_enc]
        else:
            gt_min_payload = np.ones(len(encoded), dtype=bool)
            lt_max_payload = np.ones(len(encoded), dtype=bool)
            if min_payload is not None:
                if min_payload % self.payload_lsb_bits != 0:
                    raise ValueError(f"min_payload must be a multiple of {self.payload_lsb_bits}")
                min_payload_bit = min_payload // self.payload_lsb_bits
                gt_min_payload = self.payload_msb(encoded[idx_enc]) >= min_payload_bit
            if max_payload is not None:
                if max_payload % self.payload_lsb_bits != self.payload_lsb_bits - 1:
                    raise ValueError(f"max_payload must be a multiple of {self.payload_lsb_bits} - 1")
                max_payload_bit = max_payload // self.payload_lsb_bits
                lt_max_payload = self.payload_msb(encoded[idx_enc]) <= max_payload_bit
            return encoded[lt_max_payload & gt_min_payload]


def convert_keys(keys) -> np.ndarray:
    """Convert keys to range or np.ndarray of uint64."""
    if isinstance(keys, numbers.Number):
        return np.asarray([keys], dtype=np.uint64)
    elif isinstance(keys, list):
        return np.asarray(keys, dtype=np.uint64)
    elif isinstance(keys, np.ndarray):
        return keys.astype(np.uint64)
    elif isinstance(keys, range) and len(keys) > 0:
        # UNFORTUNATE COPY
        return np.arange(keys[0], keys[-1] + 1, dtype=np.uint64) + keys[0]
    elif isinstance(keys, range):
        return np.asarray([], dtype=np.uint64)
    raise ValueError(f"Unknown type for keys: {type(keys)}")
