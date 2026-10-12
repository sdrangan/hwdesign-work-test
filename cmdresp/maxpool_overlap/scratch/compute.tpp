// One COMPUTE whose header is valid: the response header, then Y.  Returns the error code.
//
// Every counter is as narrow as its range, and every loop-carried value is updated by one
// flat select per iteration: the pixel loop must close its timing at II = 1.
template <int in_bw, int out_bw>
ap_uint<8> compute(const MaxpoolCmdHdr& h,
                   hls::stream<streamutils::axi4s_word<in_bw>>& s_in,
                   hls::stream<streamutils::axi4s_word<out_bw>>& m_out) {
#pragma HLS INLINE
    typedef ap_uint<10> ix_t;        // rows, columns, window indices: < 2 * N_MAX
    typedef ap_uint<17> cnt_t;       // pixels, loop trips: <= 264 * 256

    MaxpoolRespHdr resp_hdr;
    resp_hdr.tx_id = h.tx_id;
    resp_hdr.write_axi4_stream<out_bw>(m_out, true);

    const ix_t n0 = h.n0, n1 = h.n1;
    const ap_uint<4> s0 = h.s0, s1 = h.s1, k0 = h.k0, k1 = h.k1;
    const ix_t m0 = n0 / s0, m1 = n1 / s1;
    const cnt_t npix = n0 * n1;
    ix_t rows = n0;                                  // + the zero rows below the image
    if (m0 > 0 && s0 * (m0 - 1) + k0 > rows) {
        rows = s0 * (m0 - 1) + k0;
    }
    const cnt_t total = rows * n1;
    const cnt_t npix_m1 = npix - 1;
    const ix_t n1_m1 = n1 - 1;
    const ix_t k0_m1 = k0 - 1, k1_m1 = k1 - 1;
    const bool m0_pos = (m0 > 0), m1_pos = (m1 > 0);

    static pix_t vacc[NSLOT][N_MAX];
#pragma HLS ARRAY_PARTITION variable=vacc complete dim=1
    pix_t hacc[NSLOT], pend[NSLOT];
    bool vact[NSLOT], hact[NSLOT];
    // The last two writes to each vacc bank, forwarded to a read of the same column: a
    // narrow image (n1 = 1, 2) revisits a column before the bank's write has landed.
    ix_t fa1[NSLOT], fa2[NSLOT];
    pix_t fv1[NSLOT], fv2[NSLOT];
#pragma HLS ARRAY_PARTITION variable=hacc complete dim=1
#pragma HLS ARRAY_PARTITION variable=pend complete dim=1
#pragma HLS ARRAY_PARTITION variable=vact complete dim=1
#pragma HLS ARRAY_PARTITION variable=hact complete dim=1
#pragma HLS ARRAY_PARTITION variable=fa1 complete dim=1
#pragma HLS ARRAY_PARTITION variable=fa2 complete dim=1
#pragma HLS ARRAY_PARTITION variable=fv1 complete dim=1
#pragma HLS ARRAY_PARTITION variable=fv2 complete dim=1
    for (int q = 0; q < NSLOT; ++q) {
#pragma HLS UNROLL
        hacc[q] = 0;
        pend[q] = 0;
        vact[q] = false;
        hact[q] = false;
        fa1[q] = 1023;                               // no column: never matches
        fa2[q] = 1023;
        fv1[q] = 0;
        fv2[q] = 0;
    }

    pix_t xw[2];                     // the current input word's two pixels
#pragma HLS ARRAY_PARTITION variable=xw complete dim=1
    xw[0] = 0;
    xw[1] = 0;
    bool in_last = false;            // ...and its TLAST
    pix_t lo = 0;                    // an output value waiting for its pair
    bool have_lo = false;
    pix_t held[2];                   // a full output word, held back for its TLAST
#pragma HLS ARRAY_PARTITION variable=held complete dim=1
    held[0] = 0;
    held[1] = 0;
    bool held_valid = false;

    ix_t c = 0, r = 0;                               // column, row
    cnt_t p = 0;                                     // pixels read
    // Vertical windows: the next output row to open (and its first row), the next to close
    // (and its last row), and whether any is left to open / close.
    ap_uint<3> qs = 0, qd = 0;
    ix_t v_next = 0, v_next_row = 0, v_out = 0, v_out_row = k0_m1;
    bool v_open = m0_pos, v_close = m0_pos;
    // Horizontal windows, the same on the columns of the row being closed.
    ap_uint<3> hs = 0, ho = 0;
    ix_t h_next = 0, h_next_col = 0, h_out = 0, h_out_col = k1_m1;
    bool h_open = m1_pos, h_close = m1_pos;
    ix_t p_rd = 0, p_end = 0;                        // parked outputs: columns p_rd .. p_end-1
    ap_uint<8> err = code(MaxpoolError::NO_ERROR);

PIXELS:
    for (cnt_t t = 0; t < total; ++t) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=1 max=67584
#pragma HLS DEPENDENCE variable=vacc inter false
        // The pixel: from the stream inside the image, zero in the rows below it.
        pix_t x = 0;
        bool word_end = false;
        if (r < n0) {
            if (!p[0]) {
                streamutils::tlast_status tl = streamutils::tlast_status::no_tlast;
                int16_array_utils::read_axi4_stream_lane<in_bw>(s_in, xw, 2, tl);
                in_last = (tl == streamutils::tlast_status::tlast_at_end);
            }
            x = p[0] ? xw[1] : xw[0];
            word_end = p[0] || (p == npix_m1);
            ++p;
        }
        const bool row_end = (c == n1_m1);

        // Vertical: every open output row takes the pixel at this column.
        const bool vstart = v_open && (r == v_next_row);
        const bool vdone = v_close && (r == v_out_row);
        pix_t v = 0;
        for (int q = 0; q < NSLOT; ++q) {
#pragma HLS UNROLL
            pix_t old = vacc[q][c];
            if (fa2[q] == c) old = fv2[q];
            if (fa1[q] == c) old = fv1[q];
            const bool st = vstart && (qs == q);
            const pix_t nv = st ? x : pmax(old, x);
            if (st || vact[q]) {
                vacc[q][c] = nv;
                fa2[q] = fa1[q];
                fv2[q] = fv1[q];
                fa1[q] = c;
                fv1[q] = nv;
            }
            if (qd == q) v = nv;
        }

        // Horizontal, on the row that closes an output row.
        const bool hstart = vdone && h_open && (c == h_next_col);
        const bool hdone = vdone && h_close && (c == h_out_col);
        pix_t y = 0;
        for (int q = 0; q < NSLOT; ++q) {
#pragma HLS UNROLL
            const bool st = hstart && (hs == q);
            const pix_t nh = st ? v : pmax(hacc[q], v);
            if (st || hact[q]) hacc[q] = nh;
            if (ho == q) y = nh;
        }
        const bool pend_go = !hdone && (p_rd < p_end);    // a free slot: a parked output
        if (pend_go) {
            y = pend[p_rd(2, 0)];
        }
        const bool emit = hdone || pend_go;
        if (emit) {
            if (!have_lo) {
                lo = y;
                have_lo = true;
            } else {
                if (held_valid) {
                    int16_array_utils::write_axi4_stream_lane<out_bw>(held, m_out, false, 2);
                }
                held[0] = lo;
                held[1] = y;
                held_valid = true;
                have_lo = false;
            }
        }

        // Next state: one select per register.  At the end of a row that closed an output
        // row, the windows that ran past the right edge are parked (zero padded).
        const ix_t h_out_n = hdone ? ix_t(h_out + 1) : h_out;
        if (row_end && vdone) {
            for (int q = 0; q < NSLOT; ++q) {
#pragma HLS UNROLL
                pend[q] = pmax(hacc[q], 0);
            }
            p_rd = h_out_n;
            p_end = m1;
        } else if (pend_go) {
            p_rd = p_rd + 1;
        }
        for (int q = 0; q < NSLOT; ++q) {
#pragma HLS UNROLL
            hact[q] = !row_end && (hact[q] || (hstart && hs == q)) && !(hdone && ho == q);
            if (row_end) {
                vact[q] = (vact[q] || (vstart && qs == q)) && !(vdone && qd == q);
            }
        }
        h_open = row_end ? m1_pos : (hstart ? bool(h_next + 1 < m1) : h_open);
        h_close = row_end ? m1_pos : (hdone ? bool(h_out + 1 < m1) : h_close);
        h_next_col = row_end ? ix_t(0) : (hstart ? ix_t(h_next_col + s1) : h_next_col);
        h_out_col = row_end ? k1_m1 : (hdone ? ix_t(h_out_col + s1) : h_out_col);
        h_next = row_end ? ix_t(0) : (hstart ? ix_t(h_next + 1) : h_next);
        h_out = row_end ? ix_t(0) : h_out_n;
        hs = row_end ? ap_uint<3>(0) : (hstart ? ap_uint<3>(hs + 1) : hs);
        ho = row_end ? ap_uint<3>(0) : (hdone ? ap_uint<3>(ho + 1) : ho);
        const bool vs_go = row_end && vstart, vd_go = row_end && vdone;
        v_open = vs_go ? bool(v_next + 1 < m0) : v_open;
        v_close = vd_go ? bool(v_out + 1 < m0) : v_close;
        v_next_row = vs_go ? ix_t(v_next_row + s0) : v_next_row;
        v_out_row = vd_go ? ix_t(v_out_row + s0) : v_out_row;
        v_next = vs_go ? ix_t(v_next + 1) : v_next;
        v_out = vd_go ? ix_t(v_out + 1) : v_out;
        qs = vs_go ? ap_uint<3>(qs + 1) : qs;
        qd = vd_go ? ap_uint<3>(qd + 1) : qd;
        c = row_end ? ix_t(0) : ix_t(c + 1);
        r = row_end ? ix_t(r + 1) : r;

        // TLAST, checked on the image's words.
        if (word_end) {
            if (in_last && p != npix) {              // early: stop reading at once
                err = code(MaxpoolError::TLAST_EARLY_SAMP_IN);
                break;
            }
            if (!in_last && p == npix) {             // missing: finish Y, then halt
                err = code(MaxpoolError::NO_TLAST_SAMP_IN);
            }
        }
    }

    // The parked outputs of the last row that closed (at most 7).
FLUSH:
    for (int f = 0; f < NSLOT; ++f) {
#pragma HLS PIPELINE II=1
        if (p_rd < p_end) {
            const pix_t y = pend[p_rd(2, 0)];
            p_rd = p_rd + 1;
            if (!have_lo) {
                lo = y;
                have_lo = true;
            } else {
                if (held_valid) {
                    int16_array_utils::write_axi4_stream_lane<out_bw>(held, m_out, false, 2);
                }
                held[0] = lo;
                held[1] = y;
                held_valid = true;
                have_lo = false;
            }
        }
    }

    // Close the burst: TLAST on the last word written (none if nothing was sent).
    if (have_lo) {
        if (held_valid) {
            int16_array_utils::write_axi4_stream_lane<out_bw>(held, m_out, false, 2);
        }
        pix_t last[2];
#pragma HLS ARRAY_PARTITION variable=last complete dim=1
        last[0] = lo;
        last[1] = 0;
        int16_array_utils::write_axi4_stream_lane<out_bw>(last, m_out, true, 1);
    } else if (held_valid) {
        int16_array_utils::write_axi4_stream_lane<out_bw>(held, m_out, true, 2);
    }
    return err;
}

