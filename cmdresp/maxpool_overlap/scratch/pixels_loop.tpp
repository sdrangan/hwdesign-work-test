    // The loop is skewed into four stages, each one iteration behind the one before, so
    // that no clock cycle holds more than one of them:
    //   A  read the pixel, update the vertical banks           (pixel t)
    //   S  select the closing bank's value v                    (pixel t - 1)
    //   B  horizontal windows, parking, the next parked output  (pixel t - 2)
    //   C  the output value, packed two to a word, written      (pixel t - 3)
    // The body runs them in reverse order, so each stage reads what the stage before it
    // left on the previous iteration.  After the last pixel the loop drains for three more.
    bool a_on = true, s_ok = false, b_ok = false, c_ok = false;
    ap_uint<2> drain = 0;
    cnt_t t = 0;
    pix_t nv_s[NSLOT];                               // A -> S
#pragma HLS ARRAY_PARTITION variable=nv_s complete dim=1
    for (int q = 0; q < NSLOT; ++q) {
#pragma HLS UNROLL
        nv_s[q] = 0;
    }
    ap_uint<3> qd_s = 0;
    bool vdone_s = false, row_end_s = false;
    ix_t c_s = 0;
    pix_t v_b = 0;                                   // S -> B
    bool vdone_b = false, row_end_b = false;
    ix_t c_b = 0;
    bool hdone_c = false, hst_c = false, pend_c = false;   // B -> C
    pix_t v_c = 0, hacc_c = 0, pend_val_c = 0;

PIXELS:
    while (true) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min=4 max=67587
#pragma HLS DEPENDENCE variable=vacc inter false
        // ---- C: the output value, packed ----
        if (c_ok && (hdone_c || pend_c)) {
            const pix_t y = hdone_c ? (hst_c ? v_c : pmax(hacc_c, v_c)) : pmax(pend_val_c, 0);
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

        // ---- B: horizontal windows, on the row that closes an output row ----
        c_ok = b_ok;
        hdone_c = false;
        pend_c = false;
        if (b_ok) {
            const bool hstart = vdone_b && h_open && (c_b == h_next_col);
            const bool hdone = vdone_b && h_close && (c_b == h_out_col);
            hdone_c = hdone;
            hst_c = hstart && (hs == ho);            // a window opened and closed here (k1 = 1)
            hacc_c = hacc[ho];
            v_c = v_b;
            for (int q = 0; q < NSLOT; ++q) {
#pragma HLS UNROLL
                const bool st = hstart && (hs == q);
                if (st || hact[q]) hacc[q] = st ? v_b : pmax(hacc[q], v_b);
            }
            const bool pend_go = !hdone && (p_rd < p_end);    // a free slot: a parked output
            pend_c = pend_go;
            pend_val_c = pend[p_rd(2, 0)];
            // End of a row that closed an output row: park the windows that ran past the
            // right edge (the zero pad is applied on the way out, in C).
            const ix_t h_out_n = hdone ? ix_t(h_out + 1) : h_out;
            if (row_end_b && vdone_b) {
                for (int q = 0; q < NSLOT; ++q) {
#pragma HLS UNROLL
                    pend[q] = hacc[q];
                }
                p_rd = h_out_n;
                p_end = m1;
            } else if (pend_go) {
                p_rd = p_rd + 1;
            }
            for (int q = 0; q < NSLOT; ++q) {
#pragma HLS UNROLL
                hact[q] = !row_end_b && (hact[q] || (hstart && hs == q)) && !(hdone && ho == q);
            }
            h_open = row_end_b ? m1_pos : (hstart ? bool(h_next + 1 < m1) : h_open);
            h_close = row_end_b ? m1_pos : (hdone ? bool(h_out + 1 < m1) : h_close);
            h_next_col = row_end_b ? ix_t(0) : (hstart ? ix_t(h_next_col + s1) : h_next_col);
            h_out_col = row_end_b ? k1_m1 : (hdone ? ix_t(h_out_col + s1) : h_out_col);
            h_next = row_end_b ? ix_t(0) : (hstart ? ix_t(h_next + 1) : h_next);
            h_out = row_end_b ? ix_t(0) : h_out_n;
            hs = row_end_b ? ap_uint<3>(0) : (hstart ? ap_uint<3>(hs + 1) : hs);
            ho = row_end_b ? ap_uint<3>(0) : (hdone ? ap_uint<3>(ho + 1) : ho);
        }

        // ---- S: the closing bank's value ----
        b_ok = s_ok;
        v_b = nv_s[qd_s];
        vdone_b = vdone_s;
        row_end_b = row_end_s;
        c_b = c_s;

        // ---- A: the pixel, and the vertical banks ----
        s_ok = a_on;
        if (a_on) {
            // From the stream inside the image, zero in the rows below it.
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
            const bool vstart = v_open && (r == v_next_row);
            const bool vdone = v_close && (r == v_out_row);
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
                nv_s[q] = nv;
                if (row_end) {
                    vact[q] = (vact[q] || st) && !(vdone && qd == q);
                }
            }
            qd_s = qd;
            vdone_s = vdone;
            row_end_s = row_end;
            c_s = c;
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
            if (word_end && in_last && p != npix) {           // early: stop reading at once
                err = code(MaxpoolError::TLAST_EARLY_SAMP_IN);
                a_on = false;
            } else if (word_end && !in_last && p == npix) {   // missing: finish Y, then halt
                err = code(MaxpoolError::NO_TLAST_SAMP_IN);
            }
            if (t == total_m1) {
                a_on = false;
            }
            ++t;
        } else {
            if (drain == 2) break;
            ++drain;
        }
    }
