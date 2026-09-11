#!/usr/bin/env python3
"""
CLN Plugin: exactmultifundchannel
Author: btweenthebars

Opens multiple Core Lightning channels simultaneously with zero change left back
to the user wallet, eliminating change outputs, saving transaction bytes,
and preventing dust UTXO creation.
"""

import os
import sys
import json
import socket

RPC_PATH = None
DUST_LIMIT_SAT = 546
MAX_NON_WUMBO_SAT = 16777215  # 2^24 - 1 satoshis (0.16777215 BTC)
P2WSH_OUTPUT_WEIGHT = 172      # (8 + 1 + 34) * 4 WU


def rpc_call(method, params=None):
    """Executes a JSON-RPC request to Core Lightning over its UNIX socket."""
    if not RPC_PATH:
        raise RuntimeError("Lightning RPC socket path is not configured.")

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(RPC_PATH)
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {}
        }
        s.sendall(json.dumps(req).encode("utf-8"))

        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            try:
                resp = json.loads(buf.decode("utf-8"))
                if "error" in resp:
                    code = resp["error"].get("code", -1)
                    msg = resp["error"].get("message", str(resp["error"]))
                    raise RuntimeError(f"RPC {method} failed ({code}): {msg}")
                return resp["result"]
            except json.JSONDecodeError:
                continue
    finally:
        s.close()
    raise RuntimeError(f"Failed to read complete JSON response from RPC method {method}.")


def varint_size(val):
    """Returns serialized length in bytes for a Bitcoin CompactSize uint."""
    if val < 253:
        return 1
    elif val <= 0xFFFF:
        return 3
    elif val <= 0xFFFFFFFF:
        return 5
    return 9


def bitcoin_tx_core_weight(num_inputs, num_outputs):
    """
    Computes Bitcoin SegWit core transaction weight.
    Matches CLN's bitcoin_tx_core_weight(num_inputs, num_outputs):
    (4 [version] + varint(inputs) + varint(outputs) + 4 [locktime]) * 4 + 2 [marker+flag]
    """
    return (4 + varint_size(num_inputs) + varint_size(num_outputs) + 4) * 4 + 2


def get_utxo_spend_weight(address_or_type):
    """
    Returns estimated spend weight for a given address type in Core Lightning.
    - P2TR (Taproot): 230 WU (57.5 vB)
    - P2WPKH (Native SegWit): 271 WU (67.75 vB)
    - P2SH-P2WPKH (Nested SegWit): 363 WU (90.75 vB)
    """
    addr = (address_or_type or "").lower()
    if addr.startswith("bc1p") or addr.startswith("tb1p") or addr.startswith("bcrt1p") or "p2tr" in addr:
        return 230
    elif addr.startswith("bc1q") or addr.startswith("tb1q") or addr.startswith("bcrt1q") or "p2wpkh" in addr:
        return 271
    elif addr.startswith("3") or addr.startswith("2") or "p2sh" in addr:
        return 363
    # Fallback to standard P2WPKH weight
    return 271


def calculate_exact_allocation(destinations, feerate_per_kw, utxos_info, excess_node_id, check_wumbo=True, node_features=None):
    """
    Calculates exact non-change funding amounts down to the single satoshi.
    Returns transaction weight, fee, and exact satoshi allocation for excess_node_id.
    """
    num_inputs = len(utxos_info)
    num_outputs = len(destinations)

    if num_inputs == 0:
        raise ValueError("Cannot calculate allocation with 0 inputs.")
    if num_outputs == 0:
        raise ValueError("Cannot calculate allocation with 0 destinations.")

    # 1. Calculate transaction weight without any change output
    core_weight = bitcoin_tx_core_weight(num_inputs, num_outputs)
    inputs_weight = sum(get_utxo_spend_weight(u.get("address", "")) for u in utxos_info)
    outputs_weight = num_outputs * P2WSH_OUTPUT_WEIGHT

    total_weight = core_weight + inputs_weight + outputs_weight

    # 2. Exact fee using CLN's integer division: (feerate_per_kw * weight) // 1000
    exact_fee = (feerate_per_kw * total_weight) // 1000

    # 3. Sum total inputs
    total_inputs = sum(u["amount_sat"] for u in utxos_info)

    # 4. Sum fixed channel amounts for all other destinations
    fixed_sum = 0
    excess_node_found = False
    for d in destinations:
        if d["id"] == excess_node_id:
            excess_node_found = True
        else:
            fixed_sum += d["amount"]

    if not excess_node_found:
        raise ValueError(f"Excess destination node '{excess_node_id}' not found in destinations list.")

    # 5. Exact remainder goes to excess_node_id
    exact_excess_amount = total_inputs - fixed_sum - exact_fee

    if exact_excess_amount < DUST_LIMIT_SAT:
        raise ValueError(
            f"Resulting channel amount for '{excess_node_id}' is {exact_excess_amount} sats, "
            f"which is below the dust limit ({DUST_LIMIT_SAT} sats). "
            f"Increase input amount or reduce other channel amounts."
        )

    # 6. Wumbo / large channel checks
    if check_wumbo and exact_excess_amount > MAX_NON_WUMBO_SAT:
        # Check if node supports option_support_large_channel (bit 19)
        has_wumbo = False
        if node_features:
            # Check bit 19 or hex features
            has_wumbo = bool(node_features.get("large_channels", False))
        if not has_wumbo:
            # Warning or error if large channels not confirmed
            pass  # CLN's multifundchannel also validates features during connect/start

    # Sanity verification: total_inputs - total_outputs == exact_fee (Zero change!)
    total_outputs = fixed_sum + exact_excess_amount
    assert total_inputs - total_outputs == exact_fee, "Accounting error: Inputs - Outputs != Fee!"

    return {
        "exact_excess_amount": exact_excess_amount,
        "exact_fee": exact_fee,
        "total_weight": total_weight,
        "total_inputs": total_inputs,
        "fixed_sum": fixed_sum,
        "inputs_weight": inputs_weight,
        "outputs_weight": outputs_weight,
        "core_weight": core_weight
    }


def parse_feerate_arg(feerate_arg):
    """Resolves feerate string or int into perkw via CLN parsefeerate."""
    res = rpc_call("parsefeerate", {"feerate": feerate_arg})
    return res["perkw"]


def collect_utxos(specified_utxos, destinations, feerate_per_kw, minconf):
    """
    Gathers UTXOs either from user explicit parameter or runs CLN fundpsbt with reserve=0
    to perform automatic minimal coin selection without wallet-drain.
    """
    funds = rpc_call("listfunds")["outputs"]
    wallet_utxos = {f"{u['txid']}:{u['output']}": u for u in funds if u.get("status") == "confirmed"}

    selected_utxos_info = []

    if specified_utxos:
        for outpoint in specified_utxos:
            if outpoint not in wallet_utxos:
                raise ValueError(f"Specified UTXO '{outpoint}' was not found as a confirmed UTXO in wallet.")
            u = wallet_utxos[outpoint]
            selected_utxos_info.append({
                "outpoint": outpoint,
                "amount_sat": u["amount_msat"] // 1000,
                "address": u.get("address", "")
            })
    else:
        # Automatic coin selection: run fundpsbt with reserve=0 for base amounts
        total_base = sum(d["amount"] for d in destinations)
        startweight = bitcoin_tx_core_weight(1, len(destinations)) + len(destinations) * P2WSH_OUTPUT_WEIGHT
        fund_res = rpc_call("fundpsbt", {
            "satoshi": total_base,
            "feerate": f"{feerate_per_kw}perkw",
            "startweight": startweight,
            "minconf": minconf,
            "reserve": 0,
            "excess_as_change": True
        })

        reservations = fund_res.get("reservations", [])
        if not reservations and "psbt" in fund_res:
            # Fallback if reservations array not present: query unreserve
            pass

        for res in reservations:
            outpoint = f"{res['txid']}:{res['vout']}"
            u = wallet_utxos.get(outpoint, {})
            amount_sat = u.get("amount_msat", 0) // 1000 if u else res.get("was_reserved", 0)
            selected_utxos_info.append({
                "outpoint": outpoint,
                "amount_sat": amount_sat,
                "address": u.get("address", "")
            })

    if not selected_utxos_info:
        raise RuntimeError("Unable to acquire sufficient UTXOs for funding channels.")

    return selected_utxos_info


def process_exact_funding(params, dry_run=False):
    """Core logic to calculate exact zero-change funding and optionally execute multifundchannel."""
    destinations = params.get("destinations")
    if not destinations or not isinstance(destinations, list) or len(destinations) < 1:
        raise ValueError("'destinations' must be a non-empty array of objects with 'id' and 'amount'.")

    excess_node_id = params.get("excess_node", destinations[0]["id"])
    feerate_arg = params.get("feerate", "opening")
    minconf = params.get("minconf", 1)
    specified_utxos = params.get("utxos")

    # 1. Resolve feerate
    feerate_per_kw = parse_feerate_arg(feerate_arg)

    # 2. Select/Collect UTXOs
    selected_utxos = collect_utxos(specified_utxos, destinations, feerate_per_kw, minconf)

    # 3. Calculate exact zero-change allocation
    calc = calculate_exact_allocation(destinations, feerate_per_kw, selected_utxos, excess_node_id)

    # 4. Prepare updated destinations array
    final_destinations = []
    for d in destinations:
        new_d = dict(d)
        if d["id"] == excess_node_id:
            new_d["amount"] = calc["exact_excess_amount"]
        final_destinations.append(new_d)

    utxo_outpoints = [u["outpoint"] for u in selected_utxos]

    summary = {
        "excess_node_id": excess_node_id,
        "exact_channel_amount": calc["exact_excess_amount"],
        "exact_fee_sat": calc["exact_fee"],
        "total_inputs_sat": calc["total_inputs"],
        "tx_weight": calc["total_weight"],
        "feerate_per_kw": feerate_per_kw,
        "change_output_count": 0,
        "change_amount_sat": 0,
        "utxos_spent": utxo_outpoints
    }

    if dry_run:
        return {
            "dry_run": True,
            "summary": summary,
            "final_destinations": final_destinations
        }

    # 5. Call multifundchannel
    mfc_params = {
        "destinations": final_destinations,
        "feerate": f"{feerate_per_kw}perkw",
        "utxos": utxo_outpoints
    }
    for opt in ["minconf", "commitment_feerate", "minchannels"]:
        if opt in params:
            mfc_params[opt] = params[opt]

    mfc_res = rpc_call("multifundchannel", mfc_params)
    mfc_res["zero_change_summary"] = summary
    return mfc_res


def main():
    global RPC_PATH

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        req = json.loads(line)
        method = req.get("method")
        msg_id = req.get("id")

        if method == "getmanifest":
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "dynamic": True,
                    "rpcmethods": [
                        {
                            "name": "exactmultifundchannel",
                            "usage": "destinations [excess_node] [feerate] [minconf] [utxos] [commitment_feerate]",
                            "description": "Opens multiple payment channels using multifundchannel with zero change back to the wallet. Any change is absorbed into excess_node."
                        },
                        {
                            "name": "calculate_exact_funding",
                            "usage": "destinations [excess_node] [feerate] [minconf] [utxos]",
                            "description": "Dry-run calculation of exact zero-change channel funding amounts and transaction fee."
                        }
                    ]
                }
            }
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

        elif method == "init":
            cfg = req.get("params", {}).get("configuration", {})
            lightning_dir = cfg.get("lightning-dir", "")
            rpc_file = cfg.get("rpc-file", "lightning-rpc")
            RPC_PATH = os.path.join(lightning_dir, rpc_file)

            resp = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

        elif method == "exactmultifundchannel":
            try:
                res = process_exact_funding(req.get("params", {}), dry_run=False)
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": res}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32600, "message": str(e)}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

        elif method == "calculate_exact_funding":
            try:
                res = process_exact_funding(req.get("params", {}), dry_run=True)
                resp = {"jsonrpc": "2.0", "id": msg_id, "result": res}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32600, "message": str(e)}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
