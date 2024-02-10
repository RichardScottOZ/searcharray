import numpy as np
import math
import gc
from searcharray.phrase.middle_out import MAX_POSN, PosnBitArrayFromFlatBuilder, PosnBitArrayBuilder, PosnBitArrayAlreadyEncBuilder
from searcharray.term_dict import TermDict
from searcharray.utils.mat_set import SparseMatSetBuilder
from searcharray.utils.row_viewable_matrix import RowViewableMatrix

import logging
logger = logging.getLogger(__name__)


def _compute_doc_lens(posns: np.ndarray, doc_ids: np.ndarray, num_docs: int) -> np.ndarray:
    """Given an array of positions, compute the length of each document."""
    doc_lens = np.zeros(num_docs, dtype=np.uint32)

    # Find were we ave posns for each doc
    non_empty_doc_lens = -np.diff(posns) + 1

    non_empty_idxs = np.argwhere(non_empty_doc_lens > 0).flatten()
    non_empty_doc_ids = doc_ids[non_empty_idxs]
    non_empty_doc_lens = non_empty_doc_lens[non_empty_idxs]
    doc_lens[non_empty_doc_ids] = non_empty_doc_lens
    if doc_ids[-1] not in non_empty_doc_ids:
        doc_lens[doc_ids[-1]] = posns[-1] + 1
    return doc_lens


def convert_size(size_bytes):
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return "%s %s" % (s, size_name[i])


def _gather_tokens(array, tokenizer, term_dict, term_doc, start_doc_id=0):
    all_terms = []
    all_docs = []
    all_posns = []

    logger.info(f"Tokenizing {len(array)} documents")

    for idx, doc in enumerate(array):
        doc_id = start_doc_id + idx

        terms = np.asarray([term_dict.add_term(token)
                            for token in tokenizer(doc)], dtype=np.uint32)
        doc_ids = np.full(len(terms), doc_id, dtype=np.uint32)
        all_terms.append(terms)
        all_docs.append(doc_ids)
        all_posns.append(np.arange(len(terms)))

        term_doc.append(np.unique(terms))

        if idx % 10000 == 0 and idx > 0:
            logger.info(f"Tokenized {doc_id} ({100.0 * (idx/len(array))}%)")

    # Flatten each
    all_terms = np.concatenate(all_terms)
    all_docs = np.concatenate(all_docs)
    all_posns = np.concatenate(all_posns)

    logger.info("Tokenization -- vstacking")
    terms_w_posns = np.vstack([all_terms, all_docs, all_posns])
    del all_terms, all_docs, all_posns
    gc.collect()
    logger.info("Tokenization -- DONE")
    return terms_w_posns, term_dict, term_doc


def _tokenize_batch(array, tokenizer, term_dict, term_doc, batch_size, batch_beg):
    terms_w_posns, term_dict, term_doc = _gather_tokens(array, tokenizer,
                                                        term_dict, term_doc, start_doc_id=batch_beg)

    # Use posns to compute doc lens
    doc_lens = _compute_doc_lens(posns=terms_w_posns[2, :],
                                 doc_ids=(terms_w_posns[1, :] - batch_beg),
                                 num_docs=len(array))
    logger.info("Inverting docs->terms")
    # Sort on terms, then doc_id, then posn with lexsort
    terms_w_posns = terms_w_posns[:, np.lexsort(terms_w_posns[::-1, :])]

    # Encode posns to bit array
    logger.info("Encoding positions to bit array")
    posns = PosnBitArrayFromFlatBuilder(terms_w_posns)
    bit_posns = posns.build()

    if np.any(doc_lens > MAX_POSN):
        raise ValueError(f"Document length exceeds maximum of {MAX_POSN}")

    return term_doc, bit_posns, term_dict, doc_lens


def build_index_from_tokenizer(array, tokenizer, batch_size=10000):
    """Build index directly from tokenizing docs (array of string)."""
    term_dict = TermDict()
    term_doc = SparseMatSetBuilder()
    doc_lens = []
    bit_posns = None

    for batch_beg in range(0, len(array), batch_size):
        batch_end = (batch_beg + batch_size) if (batch_beg + batch_size) < len(array) else len(array)
        batch = array[batch_beg:batch_end]
        term_doc, batch_bit_posns, term_dict, batch_doc_lens = _tokenize_batch(batch, tokenizer,
                                                                               term_dict, term_doc,
                                                                               batch_size, batch_beg)
        if bit_posns is None:
            bit_posns = batch_bit_posns
        else:
            bit_posns.concat(batch_bit_posns)

        doc_lens.append(batch_doc_lens)

        logger.info(f"{batch_end // batch_size} - Complete, remaining batches: {len(array) / batch_size}")
        logger.info(f"{batch_end // batch_size} - Roaringish NBytes -- {convert_size(bit_posns.nbytes)}")
        logger.info(f"{batch_end // batch_size} - Term Dict Size -- {len(term_dict)}")

    doc_lens = np.concatenate(doc_lens)

    avg_doc_length = np.mean(doc_lens)

    return RowViewableMatrix(term_doc.build()), bit_posns, term_dict, avg_doc_length, np.array(doc_lens)


def build_index_from_terms_list(postings, Terms):
    """Bulid an index from postings that are already tokenized and point at their term frequencies/posns."""
    term_dict = TermDict()
    term_doc = SparseMatSetBuilder()
    doc_lens = []
    avg_doc_length = 0
    num_postings = 0
    posns = PosnBitArrayBuilder()
    posns_enc = PosnBitArrayAlreadyEncBuilder()

    # COPY 1
    # Consume generator (tokenized postings) into list
    # its faster this way?
    postings = list(postings)

    # COPY 2
    for doc_id, tokenized in enumerate(postings):
        if isinstance(tokenized, dict):
            tokenized = Terms(tokenized, doc_len=len(tokenized))
        elif not isinstance(tokenized, Terms):
            raise TypeError("Expected a Terms or a dict")

        if tokenized.encoded:
            posns = posns_enc

        doc_lens.append(tokenized.doc_len)
        avg_doc_length += doc_lens[-1]
        terms = []
        for token, term_freq in tokenized.terms():
            term_id = term_dict.add_term(token)
            terms.append(term_id)
            positions = tokenized.positions(token)
            if positions is not None:
                posns.add_posns(doc_id, term_id, positions)

        term_doc.append(terms)

        posns.ensure_capacity(doc_id)
        num_postings += 1

    if num_postings > 0:
        avg_doc_length /= num_postings

    bit_posns = posns.build()
    return RowViewableMatrix(term_doc.build()), bit_posns, term_dict, avg_doc_length, np.array(doc_lens)
