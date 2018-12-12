# -*- coding: utf-8 -*-

# Copyright (c) 2016-2018 by University of Kassel and Fraunhofer Institute for Energy Economics
# and Energy System Technology (IEE), Kassel. All rights reserved.


import copy
import math

import numpy as np

from pandapower.idx_area import PRICE_REF_BUS
from pandapower.idx_brch import F_BUS, T_BUS, BR_STATUS, branch_cols, TAP, SHIFT, BR_R, BR_X, BR_B
from pandapower.idx_bus import NONE, BUS_I, BUS_TYPE, BASE_KV, GS, BS
from pandapower.idx_gen import GEN_BUS, GEN_STATUS

try:
    from pypower.run_userfcn import run_userfcn
except ImportError:
    # ToDo: Error only for OPF functions if PYPOWER is not installed
    pass

import pandapower.auxiliary as aux
from pandapower.build_branch import _build_branch_ppc, _switch_branches, _branches_with_oos_buses, \
    _update_trafo_trafo3w_ppc, _initialize_branch_lookup, _calc_tap_from_dataframe, _calc_nominal_ratio_from_dataframe, \
    _transformer_correction_factor
from pandapower.build_bus import _build_bus_ppc, _calc_pq_elements_and_add_on_ppc, \
    _calc_shunts_and_add_on_ppc, _add_gen_impedances_ppc, _add_motor_impedances_ppc
from pandapower.build_gen import _build_gen_ppc, _update_gen_ppc
from pandapower.opf.make_objective import _make_objective



def _pd2ppc(net, sequence=None):
    """
    Converter Flow:
        1. Create an empty pypower datatructure
        2. Calculate loads and write the bus matrix
        3. Build the gen (Infeeder)- Matrix
        4. Calculate the line parameter and the transformer parameter,
           and fill it in the branch matrix.
           Order: 1st: Line values, 2nd: Trafo values
        5. if opf: make opf objective (gencost)
        6. convert internal ppci format for pypower powerflow / opf without out of service elements and rearanged buses

    INPUT:
        **net** - The pandapower format network

    OUTPUT:
        **ppc** - The simple matpower format network. Which consists of:
                  ppc = {
                        "baseMVA": 1., *float*
                        "version": 2,  *int*
                        "bus": np.array([], dtype=float),
                        "branch": np.array([], dtype=np.complex128),
                        "gen": np.array([], dtype=float),
                        "gencost" =  np.array([], dtype=float), only for OPF
                        "internal": {
                              "Ybus": np.array([], dtype=np.complex128)
                              , "Yf": np.array([], dtype=np.complex128)
                              , "Yt": np.array([], dtype=np.complex128)
                              , "branch_is": np.array([], dtype=bool)
                              , "gen_is": np.array([], dtype=bool)
                              }
        **ppci** - The "internal" pypower format network for PF calculations
    """
    # select elements in service (time consuming, so we do it once)
    net["_is_elements"] = aux._select_is_elements_numba(net, sequence=sequence)

    # get options
    mode = net["_options"]["mode"]
    check_connectivity = net["_options"]["check_connectivity"]

    ppc = _init_ppc(net, sequence=sequence)

    if mode == "opf":
        # additional fields in ppc
        ppc["gencost"] = np.array([], dtype=float)

    # init empty ppci
    ppci = copy.deepcopy(ppc)
    # generate ppc['bus'] and the bus lookup
    _build_bus_ppc(net, ppc)
    # generate ppc['gen'] and fills ppc['bus'] with generator values (PV, REF nodes)
    _build_gen_ppc(net, ppc)
    if sequence==0:
    # Add external grid impedance to the zero system for 3ph and sc calculations
        _add_ext_grid_sc_impedance_zero(net, ppc)
    # generate ppc['branch'] and directly generates branch values
        _build_branch_ppc_zero(net, ppc)
    else:
        _build_branch_ppc(net, ppc)
        
    # adds P and Q for loads / sgens in ppc['bus'] (PQ nodes)
    if mode == "sc":
        _add_gen_impedances_ppc(net, ppc)
        _add_motor_impedances_ppc(net, ppc)
    else:
        _calc_pq_elements_and_add_on_ppc(net, ppc, sequence=sequence)
        # adds P and Q for shunts, wards and xwards (to PQ nodes)
        _calc_shunts_and_add_on_ppc(net, ppc)

    # adds auxilary buses for open switches at branches
    _switch_branches(net, ppc)

    # add auxilary buses for out of service buses at in service lines.
    # Also sets lines out of service if they are connected to two out of service buses
    _branches_with_oos_buses(net, ppc)

    if check_connectivity:
        if sequence in [None, 1]:
            # sets islands (multiple isolated nodes) out of service
            net["_isolated_buses"], _, _ = aux._check_connectivity(ppc)
            net["_is_elements_final"] = aux._select_is_elements_numba(net, net["_isolated_buses"],
                                                                      sequence)
        else:
            ppc["bus"][net._isolated_buses, 1] = NONE
        net["_is_elements"] = net["_is_elements_final"]
    else:
        aux._set_isolated_buses_out_of_service(net, ppc)

    # generates "internal" ppci format (for powerflow calc) from "external" ppc format and updates the bus lookup
    # Note: Also reorders buses and gens in ppc
    ppci = _ppc2ppci(ppc, ppci, net)

    if mode == "opf":
        # make opf objective
        ppci = _make_objective(ppci, net)

    return ppc, ppci


def _init_ppc(net, sequence=None):
    # init empty ppc
    ppc = {"baseMVA": net.sn_kva * 1e-3
        , "version": 2
        , "bus": np.array([], dtype=float)
        , "branch": np.array([], dtype=np.complex128)
        , "gen": np.array([], dtype=float)
        , "internal": {
            "Ybus": np.array([], dtype=np.complex128)
            , "Yf": np.array([], dtype=np.complex128)
            , "Yt": np.array([], dtype=np.complex128)
            , "branch_is": np.array([], dtype=bool)
            , "gen_is": np.array([], dtype=bool)

            , "DLF": np.array([], dtype=np.complex128)
            , "buses_ord_bfs_nets": np.array([], dtype=float)
        }
           }
    if sequence is None:
        net["_ppc"] = ppc
    else:
        ppc["sequence"] = int(sequence)
        net["_ppc%s"%sequence] = ppc
    return ppc


def _ppc2ppci(ppc, ppci, net):
    # BUS Sorting and lookups
    # get bus_lookup
    bus_lookup = net["_pd2ppc_lookups"]["bus"]
    # get OOS busses and place them at the end of the bus array (there are no OOS busses in the ppci)
    oos_busses = ppc['bus'][:, BUS_TYPE] == NONE
    ppci['bus'] = ppc['bus'][~oos_busses]
    # in ppc the OOS busses are included and at the end of the array
    ppc['bus'] = np.r_[ppc['bus'][~oos_busses], ppc['bus'][oos_busses]]

    # generate bus_lookup_ppc_ppci (ppc -> ppci lookup)
    ppc_former_order = (ppc['bus'][:, BUS_I]).astype(int)
    aranged_buses = np.arange(len(ppc["bus"]))

    # lookup ppc former order -> consecutive order
    e2i = np.zeros(len(ppc["bus"]), dtype=int)
    e2i[ppc_former_order] = aranged_buses

    # save consecutive indices in ppc and ppci
    ppc['bus'][:, BUS_I] = aranged_buses
    ppci['bus'][:, BUS_I] = ppc['bus'][:len(ppci['bus']), BUS_I]

    # update lookups (pandapower -> ppci internal)
    _update_lookup_entries(net, bus_lookup, e2i, "bus")

    if 'areas' in ppc:
        if len(ppc["areas"]) == 0:  # if areas field is empty
            del ppc['areas']  # delete it (so it's ignored)

    # bus types
    bt = ppc["bus"][:, BUS_TYPE]

    # update branch, gen and areas bus numbering
    ppc['gen'][:, GEN_BUS] = e2i[np.real(ppc["gen"][:, GEN_BUS]).astype(int)].copy()
    ppc["branch"][:, F_BUS] = e2i[np.real(ppc["branch"][:, F_BUS]).astype(int)].copy()
    ppc["branch"][:, T_BUS] = e2i[np.real(ppc["branch"][:, T_BUS]).astype(int)].copy()

    # Note: The "update branch, gen and areas bus numbering" does the same as this:
    # ppc['gen'][:, GEN_BUS] = get_indices(ppc['gen'][:, GEN_BUS], bus_lookup_ppc_ppci)
    # ppc["branch"][:, F_BUS] = get_indices(ppc["branch"][:, F_BUS], bus_lookup_ppc_ppci)
    # ppc["branch"][:, T_BUS] = get_indices( ppc["branch"][:, T_BUS], bus_lookup_ppc_ppci)
    # but faster...

    if 'areas' in ppc:
        ppc["areas"][:, PRICE_REF_BUS] = \
            e2i[np.real(ppc["areas"][:, PRICE_REF_BUS]).astype(int)].copy()

    # reorder gens (and gencosts) in order of increasing bus number
    sort_gens = ppc['gen'][:, GEN_BUS].argsort()
    new_gen_positions = np.arange(len(sort_gens))
    new_gen_positions[sort_gens] = np.arange(len(sort_gens))
    ppc['gen'] = ppc['gen'][sort_gens,]

    # update gen lookups
    _is_elements = net["_is_elements"]
    eg_end = np.sum(_is_elements['ext_grid'])
    gen_end = eg_end + np.sum(_is_elements['gen'])
    sgen_end = len(_is_elements["sgen_controllable"]) + gen_end if "sgen_controllable" in _is_elements else gen_end
    load_end = len(_is_elements["load_controllable"]) + sgen_end if "load_controllable" in _is_elements else sgen_end
    storage_end = len(_is_elements["storage_controllable"]) + load_end if "storage_controllable" in _is_elements else load_end

    if eg_end > 0:
        _build_gen_lookups(net, "ext_grid", 0, eg_end, new_gen_positions)
    if gen_end > eg_end:
        _build_gen_lookups(net, "gen", eg_end, gen_end, new_gen_positions)
    if sgen_end > gen_end:
        _build_gen_lookups(net, "sgen_controllable", gen_end, sgen_end, new_gen_positions)
    if load_end > sgen_end:
        _build_gen_lookups(net, "load_controllable", sgen_end, load_end, new_gen_positions)
    if storage_end > load_end:
        _build_gen_lookups(net, "storage_controllable", load_end, storage_end, new_gen_positions)

    # determine which buses, branches, gens are connected and
    # in-service
    n2i = ppc["bus"][:, BUS_I].astype(int)
    bs = (bt != NONE)  # bus status

    gs = ((ppc["gen"][:, GEN_STATUS] > 0) &  # gen status
          bs[n2i[np.real(ppc["gen"][:, GEN_BUS]).astype(int)]])
    ppci["internal"]["gen_is"] = gs

    brs = (np.real(ppc["branch"][:, BR_STATUS]).astype(int) &  # branch status
           bs[n2i[np.real(ppc["branch"][:, F_BUS]).astype(int)]] &
           bs[n2i[np.real(ppc["branch"][:, T_BUS]).astype(int)]]).astype(bool)
    ppci["internal"]["branch_is"] = brs

    if 'areas' in ppc:
        ar = bs[n2i[ppc["areas"][:, PRICE_REF_BUS].astype(int)]]
        # delete out of service areas
        ppci["areas"] = ppc["areas"][ar]

    # select in service elements from ppc and put them in ppci
    ppci["branch"] = ppc["branch"][brs]

    ppci["gen"] = ppc["gen"][gs]

    if 'dcline' in ppc:
        ppci['dcline'] = ppc['dcline']
    # execute userfcn callbacks for 'ext2int' stage
    if 'userfcn' in ppci:
        ppci = run_userfcn(ppci['userfcn'], 'ext2int', ppci)

    return ppci


def _update_lookup_entries(net, lookup, e2i, element):
    valid_bus_lookup_entries = lookup >= 0
    # update entries
    lookup[valid_bus_lookup_entries] = e2i[lookup[valid_bus_lookup_entries]]
    aux._write_lookup_to_net(net, element, lookup)


def _build_gen_lookups(net, element, ppc_start_index, ppc_end_index, sort_gens):
    # get buses from pandapower and ppc
    _is_elements = net["_is_elements"]
    if element in ["sgen_controllable", "load_controllable", "storage_controllable"]:
        pandapower_index = net["_is_elements"][element].index.values
    else:
        pandapower_index = net[element].index.values[_is_elements[element]]
    ppc_index = sort_gens[ppc_start_index: ppc_end_index]

    # init lookup
    lookup = -np.ones(max(pandapower_index) + 1, dtype=int)

    # update lookup
    lookup[pandapower_index] = ppc_index
    aux._write_lookup_to_net(net, element, lookup)


def _update_ppc(net, sequence=None):
    """
    Updates P, Q values of the ppc with changed values from net

    @param _is_elements:
    @return:
    """
    # select elements in service (time consuming, so we do it once)
    net["_is_elements"] = aux._select_is_elements_numba(net)

    recycle = net["_options"]["recycle"]
    # get the old ppc and lookup
    ppc = net["_ppc"] if sequence is None else net["_ppc%s"% sequence]
    ppci = copy.deepcopy(ppc)
    # adds P and Q for loads / sgens in ppc['bus'] (PQ nodes)
    _calc_pq_elements_and_add_on_ppc(net, ppc, sequence=sequence)
    # adds P and Q for shunts, wards and xwards (to PQ nodes)
    _calc_shunts_and_add_on_ppc(net, ppc)
    # updates values for gen
    _update_gen_ppc(net, ppc)
    if not recycle["Ybus"]:
        # updates trafo and trafo3w values
        _update_trafo_trafo3w_ppc(net, ppc)

    # get OOS busses and place them at the end of the bus array (so that: 3
    # (REF), 2 (PV), 1 (PQ), 4 (OOS))
    oos_busses = ppc['bus'][:, BUS_TYPE] == NONE
    # there are no OOS busses in the ppci
    ppci['bus'] = ppc['bus'][~oos_busses]
    # select in service elements from ppc and put them in ppci
    brs = ppc["internal"]["branch_is"]
    gs = ppc["internal"]["gen_is"]
    ppci["branch"] = ppc["branch"][brs]
    ppci["gen"] = ppc["gen"][gs]

    return ppc, ppci


def _build_branch_ppc_zero(net, ppc):
    """
    Takes the empty ppc network and fills it with the zero imepdance branch values. The branch
    datatype will be np.complex 128 afterwards.

    .. note:: The order of branches in the ppc is:
            1. Lines
            2. Transformers

    **INPUT**:
        **net** -The pandapower format network

        **ppc** - The PYPOWER format network to fill in values

    """
    length = _initialize_branch_lookup(net)
    lookup = net._pd2ppc_lookups["branch"]
    mode = net._options["mode"]
    ppc["branch"] = np.zeros(shape=(length, branch_cols), dtype=np.complex128)
    if mode == "sc":
        from pandapower.shortcircuit.idx_brch import branch_cols_sc
        branch_sc = np.empty(shape=(length, branch_cols_sc), dtype=float)
        branch_sc.fill(np.nan)
        ppc["branch"] = np.hstack((ppc["branch"], branch_sc))
    ppc["branch"][:, :13] = np.array([0, 0, 0, 0, 0, 250, 250, 250, 1, 0, 1, -360, 360])
    _add_line_sc_impedance_zero(net, ppc)
    _add_trafo_sc_impedance_zero(net, ppc)
    if "trafo3w" in lookup:
        raise NotImplemented("Three winding transformers are not implemented for unbalanced calculations")


def _add_trafo_sc_impedance_zero(net, ppc, trafo_df=None):
    if trafo_df is None:
        trafo_df = net["trafo"]
    branch_lookup = net["_pd2ppc_lookups"]["branch"]
    if not "trafo" in branch_lookup:
        return
    bus_lookup = net["_pd2ppc_lookups"]["bus"]
    mode = net["_options"]["mode"]
    f, t = branch_lookup["trafo"]
    trafo_df["_ppc_idx"] = range(f, t)
    bus_lookup = net["_pd2ppc_lookups"]["bus"]
    buses_all, gs_all, bs_all = np.array([], dtype=int), np.array([]), np.array([])
    for vector_group, trafos in trafo_df.groupby("vector_group"):
        ppc_idx = trafos["_ppc_idx"].values.astype(int)
        ppc["branch"][ppc_idx, BR_STATUS] = 0

        if vector_group in ["Yy", "Yd", "Dy", "Dd"]:
            continue

        vsc_percent = trafos["vsc_percent"].values.astype(float)
        vscr_percent = trafos["vscr_percent"].values.astype(float)
        trafo_kva = trafos["sn_kva"].values.astype(float)
        vsc0_percent = trafos["vsc0_percent"].values.astype(float)
        vscr0_percent = trafos["vscr0_percent"].values.astype(float)
        lv_buses = trafos["lv_bus"].values.astype(int)
        hv_buses = trafos["hv_bus"].values.astype(int)
        lv_buses_ppc = bus_lookup[lv_buses]
        hv_buses_ppc = bus_lookup[hv_buses]
        mag0_ratio = trafos.mag0_percent.values.astype(float)
        mag0_rx = trafos["mag0_rx"].values.astype(float)
        si0_hv_partial = trafos.si0_hv_partial.values.astype(float)
        parallel = trafos.parallel.values.astype(float)
        in_service = trafos["in_service"].astype(int)

        ppc["branch"][ppc_idx, F_BUS] = hv_buses_ppc
        ppc["branch"][ppc_idx, T_BUS] = lv_buses_ppc

        vn_trafo_hv, vn_trafo_lv, shift = _calc_tap_from_dataframe(net, trafos)
        #        if mode == 'pf3ph':
        #            vn_trafo_hv = vn_trafo_hv/np.sqrt(3)
        #            vn_trafo_lv = vn_trafo_lv/np.sqrt(3)
        vn_lv = ppc["bus"][lv_buses_ppc, BASE_KV]
        ratio = _calc_nominal_ratio_from_dataframe(ppc, trafos, vn_trafo_hv, vn_trafo_lv,
                                                   bus_lookup)
        ppc["branch"][ppc_idx, TAP] = ratio
        ppc["branch"][ppc_idx, SHIFT] = shift

        # zero seq. transformer impedance
        tap_lv = np.square(vn_trafo_lv / vn_lv)  # adjust for low voltage side voltage converter
        if mode == 'pf_3ph':
            tap_lv = np.square(vn_trafo_lv / (vn_lv / np.sqrt(3)))
        z_sc = vsc0_percent / 100. / trafo_kva * tap_lv * net.sn_kva
        r_sc = vscr0_percent / 100. / trafo_kva * tap_lv * net.sn_kva

        z_sc = z_sc.astype(float)
        r_sc = r_sc.astype(float)
        x_sc = np.sign(z_sc) * np.sqrt(z_sc ** 2 - r_sc ** 2)
        z0_k = (r_sc + x_sc * 1j) / parallel
        if mode == "sc":
            from pandapower.shortcircuit.idx_bus import C_MAX
            cmax = ppc["bus"][lv_buses_ppc, C_MAX]
            kt = _transformer_correction_factor(vsc_percent, vscr_percent, trafo_kva, cmax)
            z0_k *= kt
        y0_k = 1 / z0_k
        # zero sequence transformer magnetising impedance
        z_m = (z_sc * mag0_ratio) 
        x_m = z_m / np.sqrt(mag0_rx ** 2 + 1)
        r_m = x_m * mag0_rx
        r0_trafo_mag = r_m / parallel
        x0_trafo_mag = x_m / parallel
        z0_mag = r0_trafo_mag + x0_trafo_mag * 1j

        if vector_group == "Dyn":
            buses_all = np.hstack([buses_all, lv_buses_ppc])
            gs_all = np.hstack([gs_all, y0_k.real * in_service * int(ppc["baseMVA"])])
            bs_all = np.hstack([bs_all, y0_k.imag * in_service * int(ppc["baseMVA"])])

        elif vector_group == "YNd":
            buses_all = np.hstack([buses_all, hv_buses_ppc])
            gs_all = np.hstack([gs_all, y0_k.real * in_service * int(ppc["baseMVA"])])
            bs_all = np.hstack([bs_all, y0_k.imag * in_service * int(ppc["baseMVA"])])

        elif vector_group == "Yyn":
            buses_all = np.hstack([buses_all, lv_buses_ppc])
            y = 1 / (z0_mag + z0_k).astype(complex) * int(ppc["baseMVA"])
            gs_all = np.hstack([gs_all, y.real * in_service])
            bs_all = np.hstack([bs_all, y.imag * in_service])

        elif vector_group == "YNyn":
            ppc["branch"][ppc_idx, BR_STATUS] = in_service
            # convert the t model to pi model
            z1 = si0_hv_partial * z0_k
            z2 = (1 - si0_hv_partial) * z0_k
            z3 = z0_mag

            z_temp = z1 * z2 + z2 * z3 + z1 * z3
            za = z_temp / z2
            zb = z_temp / z1
            zc = z_temp / z3

            ppc["branch"][ppc_idx, BR_R] = zc.real
            ppc["branch"][ppc_idx, BR_X] = zc.imag
            # add a shunt element parallel to zb if the leakage impedance distribution is unequal
            # TODO: this only necessary if si0_hv_partial!=0.5 --> test
            for za_tr,zb_tr in zip(za,zb):
                if za_tr==zb_tr:
                    y = -1j / za_tr
                    ppc["branch"][ppc_idx, BR_B] = y
                    ys = 0
                    buses_all = np.hstack([buses_all, lv_buses_ppc])
                    gs_all = np.hstack([gs_all, ys.real * in_service * int(ppc["baseMVA"])])
                    bs_all = np.hstack([bs_all, ys.imag * in_service * int(ppc["baseMVA"])])
                elif za_tr > zb_tr :
                    y = -1j / za_tr
#                    ppc["branch"][ppc_idx, BR_B] = y.imag - y.real * 1j
                    ppc["branch"][ppc_idx, BR_B] = y
                    zs = (za_tr * zb_tr) / (za_tr - zb_tr)
                    ys = 1/ zs.astype(complex)
                    buses_all = np.hstack([buses_all, hv_buses_ppc])
                    gs_all = np.hstack([gs_all, ys.real * in_service * int(ppc["baseMVA"])])
                    bs_all = np.hstack([bs_all, ys.imag * in_service * int(ppc["baseMVA"])])
                elif za_tr < zb_tr :
                    y = -1j/ zb_tr
#                    ppc["branch"][ppc_idx, BR_B] = y.imag - y.real * 1j
                    ppc["branch"][ppc_idx, BR_B] = y
                    zs = (za_tr * zb_tr) / (zb_tr - za_tr)
                    ys = 1/ zs.astype(complex)
                    buses_all = np.hstack([buses_all, lv_buses_ppc])
                    gs_all = np.hstack([gs_all, ys.real * in_service * int(ppc["baseMVA"])])
                    bs_all = np.hstack([bs_all, ys.imag * in_service * int(ppc["baseMVA"])])
        elif vector_group == "YNy":
            buses_all = np.hstack([buses_all, hv_buses_ppc])
            y = 1 / (z0_mag + z0_k).astype(complex) * int(ppc["baseMVA"])
            gs_all = np.hstack([gs_all, y.real * in_service])
            bs_all = np.hstack([bs_all, y.imag * in_service])
        elif vector_group[-1].isdigit():
            raise ValueError(
                "Unknown transformer vector group %s - please specify vector group without phase shift number. Phase shift can be specified in net.trafo.shift_degree" % vector_group)
        else:
            raise ValueError("Transformer vector group %s is unknown / not implemented" % vector_group)

    buses, gs, bs = aux._sum_by_group(buses_all, gs_all, bs_all)
    ppc["bus"][buses, GS] += gs
    ppc["bus"][buses, BS] += bs
    del net.trafo["_ppc_idx"]


def _add_ext_grid_sc_impedance_zero(net, ppc):
    mode = net["_options"]["mode"]

    if mode == "sc":
        from pandapower.shortcircuit.idx_bus import C_MAX, C_MIN
        case = net._options["case"]
    else:
        case = "max"
    bus_lookup = net["_pd2ppc_lookups"]["bus"]
    eg = net["ext_grid"][net._is_elements["ext_grid"]]
    if len(eg) == 0:
        return
    eg_buses = eg.bus.values
    eg_buses_ppc = bus_lookup[eg_buses]

    if mode == "sc":
        c = ppc["bus"][eg_buses_ppc, C_MAX] if case == "max" else ppc["bus"][eg_buses_ppc, C_MIN]
    elif mode == 'pf_3ph':
        c = 1.1  # Todo: Where does that value come from?
    if not "s_sc_%s_mva" % case in eg:
        raise ValueError("short circuit apparent power s_sc_%s_mva needs to be specified for " % case +
                         "external grid")
    s_sc = eg["s_sc_%s_mva" % case].values
    if not "rx_%s" % case in eg:
        raise ValueError("short circuit R/X rate rx_%s needs to be specified for external grid" %
                         case)
    rx = eg["rx_%s" % case].values
    z_grid = c / s_sc
    if mode == 'pf_3ph':
        z_grid = c / (s_sc / 3)  # 3 phase power divided to get 1 ph power
    x_grid = z_grid / np.sqrt(rx ** 2 + 1)
    r_grid = rx * x_grid
    eg["r"] = r_grid
    eg["x"] = x_grid

    # ext_grid zero sequence impedance
    if case == "max":
        x0_grid = net.ext_grid["x0x_%s" % case] * x_grid
        r0_grid = net.ext_grid["r0x0_%s" % case] * x0_grid
    elif case == "min":
        x0_grid = net.ext_grid["x0x_%s" % case] * x_grid
        r0_grid = net.ext_grid["r0x0_%s" % case] * x0_grid
    y0_grid = 1 / (r0_grid + x0_grid * 1j)
    buses, gs, bs = aux._sum_by_group(eg_buses_ppc, y0_grid.real, y0_grid.imag)
    ppc["bus"][buses, GS] = gs
    ppc["bus"][buses, BS] = bs


def _add_line_sc_impedance_zero(net, ppc):
    branch_lookup = net["_pd2ppc_lookups"]["branch"]
    mode = net["_options"]["mode"]
    if not "line" in branch_lookup:
        return
    line = net["line"]
    bus_lookup = net["_pd2ppc_lookups"]["bus"]
    length = line["length_km"].values
    parallel = line["parallel"].values

    fb = bus_lookup[line["from_bus"].values]
    tb = bus_lookup[line["to_bus"].values]
    baseR = np.square(ppc["bus"][fb, BASE_KV]) / ppc["baseMVA"]
    if mode == 'pf_3ph':
        baseR = np.square(ppc["bus"][fb, BASE_KV] / np.sqrt(3)) / ppc["baseMVA"]
    f, t = branch_lookup["line"]
    # line zero sequence impedance
    ppc["branch"][f:t, F_BUS] = fb
    ppc["branch"][f:t, T_BUS] = tb
    ppc["branch"][f:t, BR_R] = line["r0_ohm_per_km"].values * length / baseR / parallel
    ppc["branch"][f:t, BR_X] = line["x0_ohm_per_km"].values * length / baseR / parallel
    ppc["branch"][f:t, BR_B] = (
                2 * net["f_hz"] * math.pi * line["c0_nf_per_km"].values * 1e-9 * baseR * length * parallel)
    ppc["branch"][f:t, BR_STATUS] = line["in_service"].astype(int)
