"""Roaring-ish bit array for storing sorted integers in numpy array."""
import numpy as np
import sortednp as snp
import logging
import numbers
from typing import Optional

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
DEFAULT_KEY_BITS = 28
DEFAULT_PAYLOAD_MSB_MASK = np.uint64(0x0000000FFFFC0000)
DEFAULT_PAYLOAD_MSB_BITS = 18
DEFAULT_PAYLOAD_LSB_MASK = np.uint64(0x000000000003FFFF)
DEFAULT_PAYLOAD_LSB_BITS = 18


def n_msb_mask(n: np.uint64) -> np.uint64:
    """Return the n most significant bits of num."""
    return ~(np.uint64(1 << (64 - n))) + np.uint64(1)


class RoaringishEncoder:

    def __init__(self, key_bits=DEFAULT_KEY_BITS):
        payload_bits = 64 - key_bits
        self.payload_msb_bits = payload_bits // 2
        self.payload_lsb_bits = payload_bits - self.payload_msb_bits
        self.key_bits = key_bits
        # key bits MSB of 64 bits
        self.key_mask = n_msb_mask(key_bits)
        self.payload_msb_mask = n_msb_mask(self.payload_msb_bits + key_bits) & ~self.key_mask
        self.payload_lsb_mask = (1 << self.payload_lsb_bits) - 1
        if key_bits == DEFAULT_KEY_BITS:
            assert self.key_mask == DEFAULT_KEY_MASK
            assert self.payload_msb_mask == DEFAULT_PAYLOAD_MSB_MASK
            assert self.payload_lsb_mask == DEFAULT_PAYLOAD_LSB_MASK

    def _validate_payload(self, payload: np.ndarray):
        if not np.all(payload < 2**self.payload_lsb_bits):
            raise ValueError(f"Positions must be less than {2**self.payload_lsb_bits}")

    def encode(self, payload: np.ndarray, keys: Optional[np.ndarray] = None):
        """Pack a sorted array of integers into compact bit numpy array.

        each returned array represents a single term, with doc_id as MSBS, ie:

        | 32 MSBs | 16 LSBs | 16 LSBs |
          key     | bits msbs| payload

        for later easy intersection of 32+16 msbs, then checking for adjacent
        positions

        """
        self._validate_payload(payload)
        cols = payload // self.payload_lsb_bits    # Header of bit to use
        cols = cols.astype(np.uint64) << self.payload_msb_bits
        if keys is not None:
            cols |= keys.astype(np.uint64) << (64 - self.key_bits)
        values = payload % self.payload_lsb_bits   # Value to encode

        change_indices = np.nonzero(np.diff(cols))[0] + 1
        change_indices = np.insert(change_indices, 0, 0)

        encoded = cols | (1 << values)
        if len(encoded) == 0:
            return encoded
        return np.bitwise_or.reduceat(encoded, change_indices)

    def decode(self, encoded, get_keys=True):
        keys = (encoded & self.key_mask) >> (64 - self.key_bits)
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
            as_list = list(zip(keys, grouped))
        else:
            as_list = grouped

        return as_list

    def keys(self, encoded):
        """Return keys from encoded."""
        return (encoded & self.key_mask) >> (64 - self.key_bits)

    def intersect(self, lhs: np.ndarray, rhs: np.ndarray):
        """Return the MSBs that are common to both lhs and rhs."""
        # common = np.intersect1d(lhs_msbs, rhs_msbs)
        _, (lhs_idx, rhs_idx) = snp.intersect(lhs >> self.payload_lsb_bits, rhs >> self.payload_lsb_bits, indices=True)
        # With large arrays np.isin becomes a bottleneck
        return lhs[lhs_idx], rhs[rhs_idx]

    def slice(self, encoded: np.ndarray, keys: np.ndarray):
        """Get list of encoded that have values in keys."""
        assert len(keys.shape) == 1
        assert len(encoded.shape) == 1
        encoded_keys = encoded.astype(np.uint64) >> (64 - self.key_bits)
        _, (idx_docs, idx_enc) = snp.intersect(keys, encoded_keys, indices=True,
                                               duplicates=snp.KEEP_MAX_N)

        return encoded[idx_enc]


def convert_keys(keys):
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
