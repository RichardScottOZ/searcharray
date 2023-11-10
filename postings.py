"""Tokenized, searchable text as a pandas dtype."""
import pandas as pd
import numbers
from collections import Counter, defaultdict
from pandas.api.extensions import ExtensionDtype, ExtensionArray, register_extension_dtype
from pandas.api.types import is_list_like
from pandas.api.extensions import take
import json
import warnings

import numpy as np
from term_dict import TermDict, TermMissingError

# Doc,Term -> freq
# Note scipy sparse switching to *_array, which is more numpy like
# However, as of now, these don't seem fully baked
from scipy.sparse import lil_matrix, csr_matrix


class PostingsRow:
    """Wrapper around a row of a postings matrix.

    We can't easily directly use a dictionary as a cell type in pandas.
    See:

    https://github.com/pandas-dev/pandas/issues/17777
    """

    def __init__(self, postings, posns=None):
        self.postings = postings
        self.posns = None
        if self.posns is not None and len(self.postings) != len(self.posns):
            raise ValueError("Postings and positions must be the same length.")

    def termfreq(self, term):
        return self.postings[term]

    def terms(self):
        return self.postings.items()

    def positions(self, term=None):
        if self.posns is None:
            return {}
        if term is None:
            return self.posns.items()
        else:
            return self.posns[term]

    def tf_to_dense(self, term_dict):
        """Convert to a dense vector of term frequencies."""
        dense = np.zeros(len(term_dict))
        for term, freq in self.terms():
            dense[term_dict.get_term_id(term)] = freq
        return dense

    def __len__(self):
        return len(self.postings)

    def __repr__(self):
        return f"PostingsRow({repr(self.postings)})"

    def __str__(self):
        return f"PostingsRow({str(self.postings)})"

    def __eq__(self, other):
        # Flip to the other implementation if we're comparing to a PostingsArray
        # to get a boolean array back
        if isinstance(other, PostingsArray):
            return other == self
        return isinstance(other, PostingsRow) and self.postings == other.postings

    def __lt__(self, other):
        # return isinstance(other, PostingsRow) and hash(self) < hash(other)
        keys_both = set(self.postings.keys()).union(set(other.postings.keys()))
        # Sort lexically
        keys_both = sorted(keys_both)

        # Iterate as if these are two vectors of the same large dimensional vector sparse
        for key in keys_both:
            lhs_val = 0
            rhs_val = 0
            try:
                lhs_val = self.postings[key]
            except KeyError:
                pass

            try:
                rhs_val = other.postings[key]
            except KeyError:
                pass

            if lhs_val < rhs_val:
                return True
            elif lhs_val > rhs_val:
                return False
            else:
                continue
        return False

    def __le__(self, other):
        return self < other or self == other

    def __gt__(self, other):
        return not (self < other) and self != other

    def __hash__(self):
        return hash(json.dumps(self.postings, sort_keys=True))


class PostingsDtype(ExtensionDtype):
    name = 'tokenized_text'
    type = PostingsRow
    kind = 'O'  # Object kind

    @classmethod
    def construct_from_string(cls, string):
        if not isinstance(string, str):
            raise TypeError(
                "'construct_from_string' expects a string, got {}".format(type(string))
            )
        elif string == cls.name:
            return cls()
        else:
            raise TypeError(
                "Cannot construct a '{}' from '{}'".format(cls.__name__, string)
            )

    @classmethod
    def construct_array_type(cls):
        return PostingsArray

    def __repr__(self):
        return 'PostingsDtype()'

    @property
    def na_value(self):
        return PostingsRow({})

    def valid_value(self, value):
        return isinstance(value, dict) or pd.isna(value) or isinstance(value, PostingsRow)


register_extension_dtype(PostingsDtype)


def ws_tokenizer(string):
    if pd.isna(string):
        return []
    if not isinstance(string, str):
        raise ValueError("Expected a string")
    return string.split()


class RowViewableMatrix:
    """A slicable matrix that can return views without copying."""

    def __init__(self, csr_mat: csr_matrix, rows: np.ndarray = None):
        self.mat = csr_mat
        if rows is None:
            self.rows = np.arange(self.mat.shape[0])
        else:
            self.rows = rows

    def slice(self, keys):
        return RowViewableMatrix(self.mat, self.rows[keys])

    def __setitem__(self, keys, values):
        # Replace nan with 0
        actual_keys = self.rows[keys]
        if isinstance(actual_keys, numbers.Number):
            self.mat[actual_keys] = values
        elif len(actual_keys) > 0:
            self.mat[actual_keys] = values

    def copy_row_at(self, row):
        return self.mat[self.rows[row]]

    def copy(self):
        return RowViewableMatrix(self.mat.copy(), self.rows.copy())

    def sum(self, axis=0):
        return self.mat[self.rows].sum(axis=axis)

    def copy_col_at(self, col):
        return self.mat[self.rows, col]

    def __getitem__(self, key):
        if isinstance(key, numbers.Number):
            return self.copy_row_at(key)
        else:
            return self.slice(key)

    @property
    def nbytes(self):
        return self.mat.data.nbytes + \
            self.mat.indptr.nbytes + \
            self.mat.indices.nbytes + \
            self.rows.nbytes

    @property
    def shape(self):
        return (len(self.rows), self.mat.shape[1])

    def resize(self, shape):
        self.mat.resize(shape)

    def __len__(self):
        return len(self.rows)

    def __repr__(self):
        return f"RowViewableMatrix({repr(self.mat)}, {repr(self.rows)})"

    def __str__(self):
        return f"RowViewableMatrix({str(self.mat)}, {str(self.rows)})"

# To add positions
# Row/Col -> index to roaring bitmap
# Must be slicable


def _build_index_from_dict(tokenized_postings):
    """Bulid an index from postings that are already tokenized and point at their term frequencies."""
    freqs_table = lil_matrix((len(tokenized_postings), 0), dtype=np.uint8)
    posns_table = lil_matrix((len(tokenized_postings), 0), dtype=np.uint32)
    term_dict = TermDict()
    avg_doc_length = 0
    positions_lookup = []
    for doc_id, tokenized in enumerate(tokenized_postings):
        avg_doc_length += len(tokenized)
        for token, term_freq in tokenized.terms():
            term_id = term_dict.add_term(token)
            if term_id >= freqs_table.shape[1]:
                freqs_table.resize((freqs_table.shape[0], term_id + 1))
                posns_table.resize((posns_table.shape[0], term_id + 1))
            freqs_table[doc_id, term_id] = term_freq

            positions = tokenized.positions(token)
            if positions is not None:
                idx = len(positions_lookup)
                positions_lookup.append(positions)
                posns_table[doc_id, term_id] = idx

    if len(tokenized_postings) > 0:
        avg_doc_length /= len(tokenized_postings)

    assert freqs_table.shape == posns_table.shape
    return RowViewableMatrix(csr_matrix(freqs_table)), RowViewableMatrix(csr_matrix(posns_table)), positions_lookup, term_dict, avg_doc_length


def _row_to_postings_row(row, term_dict):
    result = PostingsRow({term_dict.get_term(term_id): int(row[0, term_id])
                          for term_id in range(row.shape[1]) if row[0, term_id] > 0})
    return result


# Logically a PostingsArray is a document represented as follows:
#
#   docs = [
#       {"foo": 1, "bar": 2, "baz": 1}, # doc 0 term->freq
#       {"foo": 2, "bar": 4, "baz": 8, "the"}, # doc 0 term->freq
#       ...
#   ]
#
# This postings will build its own term_dict and term_freqs
class PostingsArray(ExtensionArray):
    dtype = PostingsDtype()

    def __init__(self, postings, tokenizer=ws_tokenizer):
        # Check dtype, raise TypeError
        if not is_list_like(postings):
            raise TypeError("Expected list-like object, got {}".format(type(postings)))
        if not all(isinstance(x, PostingsRow) or isinstance(x, dict) or pd.isna(x) for x in postings):
            raise TypeError("Expected a list of PostingsRow or dicts")

        # Convert all to postings rows
        as_postings = [PostingsRow(x) if isinstance(x, dict) else x for x in postings]

        self.tokenizer = tokenizer
        self.term_freqs, self.posns, self.posns_lookup, \
            self.term_dict, self.avg_doc_length = _build_index_from_dict(as_postings)
        if self.posns.shape != self.term_freqs.shape:
            import pdb; pdb.set_trace()

    @classmethod
    def index(cls, array, tokenizer=ws_tokenizer):
        """Index an array of strings using tokenizer."""
        # Convert strings to expected scalars (dict -> term freqs)
        if not is_list_like(array):
            raise TypeError("Expected list-like object, got {}".format(type(array)))
        if not all(isinstance(x, str) or pd.isna(x) for x in array):
            raise TypeError("Expected a list of strings to tokenize")

        def tokenized_docs(docs):
            for doc in docs:
                if pd.isna(doc):
                    yield PostingsRow({})
                else:
                    token_stream = tokenizer(doc)
                    term_freqs = Counter(token_stream)
                    positions = defaultdict(list)
                    for posn in range(len(token_stream)):
                        positions[token_stream[posn]].append(posn)
                    yield PostingsRow(term_freqs, positions)

        return cls([a for a in tokenized_docs(array)], tokenizer)

    @classmethod
    def _from_sequence(cls, scalars, dtype=None, copy=False):
        """Construct a new PostingsArray from a sequence of scalars (PostingRow or convertible into)."""
        if dtype is not None:
            if not isinstance(dtype, PostingsDtype):
                return scalars
        if type(scalars) == np.ndarray and scalars.dtype == PostingsDtype():
            return cls(scalars)
        # String types
        elif type(scalars) == np.ndarray and scalars.dtype.kind in 'US':
            return cls(scalars)
        # Other objects
        elif type(scalars) == np.ndarray and scalars.dtype != object:
            return scalars
        return cls(scalars)

    def memory_usage(self, deep=False):
        return self.nbytes

    @property
    def nbytes(self):
        return self.term_freqs.nbytes + self.posns.nbytes

    def __getitem__(self, key):
        key = pd.api.indexers.check_array_indexer(self, key)
        # Want to take rows of term freqs
        if isinstance(key, int):
            try:
                rows = self.term_freqs[key]
                return _row_to_postings_row(rows[0], self.term_dict)
            except IndexError:
                raise IndexError("index out of bounds")
        else:
            # Construct a sliced view of this array
            sliced_tfs = self.term_freqs.slice(key)
            sliced_posns = self.posns.slice(key)
            arr = PostingsArray([], tokenizer=self.tokenizer)
            arr.term_freqs = sliced_tfs
            arr.posns = sliced_posns
            arr.posns_lookup = self.posns_lookup
            arr.term_dict = self.term_dict
            arr.avg_doc_length = self.avg_doc_length
            return arr

    def __setitem__(self, key, value):
        """Set an item in the array."""
        key = pd.api.indexers.check_array_indexer(self, key)
        if isinstance(value, pd.Series):
            value = value.values
        if isinstance(value, pd.DataFrame):
            value = value.values.flatten()
        if isinstance(value, PostingsArray):
            value = value.to_numpy()
        if isinstance(value, list):
            value = np.asarray(value, dtype=object)

        if not isinstance(value, np.ndarray) and not self.dtype.valid_value(value):
            raise ValueError(f"Cannot set non-object array to PostingsArray -- you passed type:{type(value)} -- {value}")

        # Cant set a single value to an array
        if isinstance(key, numbers.Integral) and isinstance(value, np.ndarray):
            raise ValueError("Cannot set a single value to an array")

        try:
            posns = None
            if isinstance(value, float):
                term_freqs = np.asarray([value])
            elif isinstance(value, PostingsRow):
                term_freqs = np.asarray([value.tf_to_dense(self.term_dict)])
                posns = np.asarray([value.positions()])
            elif isinstance(value, np.ndarray):
                term_freqs = np.asarray([x.tf_to_dense(self.term_dict) for x in value])
                posns = np.asarray([x.positions() for x in value])
            np.nan_to_num(term_freqs, copy=False, nan=0)
            self.term_freqs[key] = term_freqs

            if posns is not None:
                update_rows = self.posns[key]
                for update_row_idx, new_posns_row in enumerate(posns):
                    for term, positions in new_posns_row.items():
                        term_id = self.term_dict[term]
                        lookup_location = update_rows[update_row_idx, term_id]
                        self.posns_lookup[lookup_location] = positions

            # Assume we have a positions for each term, doc pair. We can just update it.
            # Otherwise we would have added new terms
        except TermMissingError:
            self._add_new_terms(key, value)

    def _add_new_terms(self, key, value):
        msg = """Adding new terms! This might not be good if you tokenized this new text
                 with a different tokenizer.

                 Also. This is slow."""
        warnings.warn(msg)

        scan_value = value
        if isinstance(value, PostingsRow):
            scan_value = np.asarray([value])
        for row in scan_value:
            for term in row.terms():
                self.term_dict.add_term(term[0])

        self.term_freqs.resize((self.term_freqs.shape[0], len(self.term_dict)))
        self.posns.resize((self.term_freqs.shape[0], len(self.term_dict)))
        if self.posns.shape != self.term_freqs.shape:
            import pdb; pdb.set_trace()
        self[key] = value

    def value_counts(
        self,
        dropna: bool = True,
    ):
        if dropna:
            counts = Counter(self[:])
            counts.pop(PostingsRow({}), None)
        else:
            counts = Counter(self[:])
        return pd.Series(counts)

    def __len__(self):
        return len(self.term_freqs.rows)

    def __ne__(self, other):
        if isinstance(other, pd.DataFrame) or isinstance(other, pd.Series) or isinstance(other, pd.Index):
            return NotImplemented

        return ~(self == other)

    def __eq__(self, other):
        """Return a boolean numpy array indicating elementwise equality."""
        # When other is a dataframe or series, not implemented
        if isinstance(other, pd.DataFrame) or isinstance(other, pd.Series) or isinstance(other, pd.Index):
            return NotImplemented

        # When other is an ExtensionArray
        if isinstance(other, PostingsArray):
            if len(self) != len(other):
                return False
            elif len(other) == 0:
                return np.array([], dtype=bool)
            return np.array(self[:]) == np.array(other[:])

        # When other is a scalar value
        elif isinstance(other, PostingsRow):
            other = PostingsArray([other], tokenizer=self.tokenizer)
            return np.array(self[:]) == np.array(other[:])

        # When other is a sequence but not an ExtensionArray
        # its an array of dicts
        elif is_list_like(other):
            if len(self) != len(other):
                return False
            elif len(other) == 0:
                return np.array([], dtype=bool)
            # We actually don't know how it was tokenized
            other = PostingsArray(other, tokenizer=self.tokenizer)
            return np.array(self[:]) == np.array(other[:])

        # Return False where 'other' is neither the same length nor a scalar
        else:
            return np.full(len(self), False)

    def isna(self):
        # Every row with all 0s
        key_slice_all = slice(None)
        sliced = self.term_freqs.slice(key_slice_all)
        empties = np.asarray((sliced.sum(axis=1) == 0).flatten())[0]
        return empties

    def take(self, indices, allow_fill=False, fill_value=None):
        if allow_fill:
            if fill_value is None or pd.isna(fill_value):
                fill_value = PostingsRow({})
        # Want to take rows of term freqs
        row_indices = np.arange(len(self.term_freqs.rows))
        # Take within the row indices themselves
        result_indices = take(row_indices, indices, allow_fill=allow_fill, fill_value=-1)
        # Construct postings from each result_indices
        taken_postings = []
        for result_index in result_indices:
            if result_index == -1:
                taken_postings.append(fill_value)
            else:
                taken_postings.append(_row_to_postings_row(self.term_freqs.copy_row_at(result_index), self.term_dict))
        if self.posns.shape != self.term_freqs.shape:
            import pdb; pdb.set_trace()
        return PostingsArray(taken_postings, tokenizer=self.tokenizer)

    def copy(self):
        # taken_postings = []
        # for result_index in range(len(self.term_freqs.rows)):
        #     taken_postings.append(_row_to_postings_row(self.term_freqs.copy_row_at(result_index), self.term_dict))
        # arr1 = PostingsArray(taken_postings, tokenizer=self.tokenizer)

        postings_arr = PostingsArray([], tokenizer=self.tokenizer)
        postings_arr.posns = self.posns.copy()
        postings_arr.posns_lookup = self.posns_lookup.copy()
        postings_arr.term_freqs = self.term_freqs.copy()
        postings_arr.term_dict = self.term_dict.copy()
        postings_arr.avg_doc_length = self.avg_doc_length
        if self.posns.shape != self.term_freqs.shape:
            import pdb; pdb.set_trace()
        return postings_arr

    @classmethod
    def _concat_same_type(cls, to_concat):
        concatenated_data = np.concatenate([ea[:] for ea in to_concat])
        return PostingsArray(concatenated_data, tokenizer=to_concat[0].tokenizer)

    @classmethod
    def _from_factorized(cls, values, original):
        return cls(values)

    def _values_for_factorize(self):
        """Return an array and missing value suitable for factorization (ie grouping)."""
        arr = np.asarray(self[:], dtype=object)
        return arr, PostingsRow({})

    # ***********************************************************
    # Naive implementations of search functions to clean up later
    # ***********************************************************
    def term_freq(self, tokenized_term):
        if not isinstance(tokenized_term, str):
            raise TypeError("Expected a string")

        term_id = self.term_dict.get_term_id(tokenized_term)
        matches = self.term_freqs.copy_col_at(term_id).todense().flatten()
        matches = np.asarray(matches).flatten()
        return matches

    def doc_freq(self, tokenized_term):
        if not isinstance(tokenized_term, str):
            raise TypeError("Expected a string")
        # Count number of rows where the term appears
        term_freq = self.term_freq(tokenized_term)
        return np.sum(term_freq > 0)

    def doc_lengths(self):
        return np.array(self.term_freqs.sum(axis=1).flatten())[0]

    def match(self, tokenized_term):
        """Return a boolean numpy array indicating which elements contain the given term."""
        term_freq = self.term_freq(tokenized_term)
        return term_freq > 0

    def bm25_idf(self, tokenized_term):
        df = self.doc_freq(tokenized_term)
        num_docs = len(self)
        return np.log(1 + (num_docs - df + 0.5) / (df + 0.5))

    def bm25_tf(self, tokenized_term, k1=1.2, b=0.75):
        tf = self.term_freq(tokenized_term)
        numer = (k1 + 1) * tf
        denom = k1 * (1 - b + b * (self.doc_lengths() / self.avg_doc_length))
        return numer / denom

    def bm25(self, tokenized_term, k1=1.2, b=0.75):
        """Score each doc using BM25."""
        return self.bm25_idf(tokenized_term) * self.bm25_tf(tokenized_term)
