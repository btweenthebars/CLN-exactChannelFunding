#!/usr/bin/env python3
"""
CLN Plugin: exactmultifundchannel
Author: btweenthebars

Opens multiple Core Lightning channels simultaneously with zero change left back
to the user wallet. Any leftover change is absorbed into a designated destination
channel specified by `change_to` (index in destinations array).

Arguments match multifundchannel + change_to (mandatory). Any extra/future arguments
are passed straight through to multifundchannel without hardcoding.
"""

import os
import sys
import json
import socket

RPC_PATH = None
DUST_LIMIT_SAT = 546
P2WSH_OUTPUT_WEIGHT = 172  # (8 + 1 + 34) * 4 WU


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
            "params": params if params is not None else {}
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


def parse_sat_amount(amt):
    """Parses satoshi amount from integer or string (e.g. 1000, '1000sat', '1000000msat', '0.01btc')."""
    if isinstance(amt, int):
        return amt
    s = str(amt).strip().lower()
    if s.endswith("msat"):
        return int(s[:-4]) // 1000
    elif s.endswith("sat"):
        return int(s[:-3])
    elif s.endswith("btc"):
        return int(float(s[:-3]) * 100_000_000)
    return int(s)


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
    return 271


def calculate_exact_allocation(destinations, feerate_per_kw, utxos_info, change_to_idx):
    """
    Calculates exact zero-change funding allocation down to the single satoshi.
    Absorbs all remaining input funds into destinations[change_to_idx].
    """
    num_inputs = len(utxos_info)
    num_outputs = len(destinations)

    if num_inputs == 0:
        raise ValueError("Cannot calculate allocation with 0 inputs.")
    if num_outputs == 0:
        raise ValueError("Cannot calculate allocation with 0 destinations.")
    if change_to_idx < 0 or change_to_idx >= num_outputs:
        raise ValueError(
            f"'change_to' index {change_to_idx} is out of bounds for destinations list of length {num_outputs}."
        )

    # 1. Total transaction weight without change output
    core_weight = bitcoin_tx_core_weight(num_inputs, num_outputs)
    inputs_weight = sum(get_utxo_spend_weight(u.get("address", "")) for u in utxos_info)
    outputs_weight = num_outputs * P2WSH_OUTPUT_WEIGHT

    total_weight = core_weight + inputs_weight + outputs_weight

    # 2. Exact fee using CLN integer division: (feerate_per_kw * weight) // 1000
    exact_fee = (feerate_per_kw * total_weight) // 1000

    # 3. Sum total inputs
    total_inputs = sum(u["amount_sat"] for u in utxos_info)

    # 4. Sum amounts of all other destinations
    other_dest_sum = sum(
        parse_sat_amount(d["amount"])
        for idx, d in enumerate(destinations)
        if idx != change_to_idx
    )

    # 5. Exact amount for destinations[change_to_idx]
    target_orig_amount = parse_sat_amount(destinations[change_to_idx]["amount"])
    exact_target_amount = total_inputs - other_dest_sum - exact_fee

    if exact_target_amount < DUST_LIMIT_SAT:
        raise ValueError(
            f"Resulting channel amount for destination {change_to_idx} ({destinations[change_to_idx].get('id', '')}) "
            f"is {exact_target_amount} sats, which is below the dust limit ({DUST_LIMIT_SAT} sats). "
            f"Provide more input funds or reduce channel amounts."
        )

    change_absorbed = exact_target_amount - target_orig_amount

    # Sanity check: total inputs == total outputs + fee (strictly 0 change)
    total_outputs = other_dest_sum + exact_target_amount
    assert total_inputs - total_outputs == exact_fee, "Accounting invariant failed: Inputs - Outputs != Fee!"

    return {
        "exact_target_amount": exact_target_amount,
        "target_orig_amount": target_orig_amount,
        "change_absorbed": change_absorbed,
        "exact_fee": exact_fee,
        "total_weight": total_weight,
        "total_inputs": total_inputs,
        "other_dest_sum": other_dest_sum,
        "change_to_idx": change_to_idx,
        "change_to_id": destinations[change_to_idx].get("id", "")
    }


def get_mfc_parameter_names():
    """
    Dynamically fetches the positional parameter names of multifundchannel
    from CLN help command so that we don't hardcode them.
    """
    default_args = ["destinations", "feerate", "minconf", "utxos", "minchannels", "commitment_feerate"]
    try:
        help_res = rpc_call("help", {"command": "multifundchannel"})
        cmd_info = help_res.get("help", [{}])[0]
        cmd_str = cmd_info.get("command", "")
        parts = cmd_str.split()
        if parts and parts[0] == "multifundchannel":
            args = [p.strip("[]") for p in parts[1:] if p.strip("[]")]
            if args:
                return args
    except Exception:
        pass
    return default_args


def normalize_params(params):
    """
    Normalizes incoming params whether passed as a list (positional) or dict (keyword -k),
    converting to a dict without dropping any unknown or future arguments.
    """
    if isinstance(params, list):
        arg_names = get_mfc_parameter_names() + ["change_to"]
        dict_params = {}
        for i, val in enumerate(params):
            if i < len(arg_names):
                dict_params[arg_names[i]] = val
            else:
                dict_params[f"arg_{i}"] = val
        return dict_params
    elif isinstance(params, dict):
        return dict(params)
    else:
        raise ValueError("Invalid params type: expected list or dict.")


def parse_destinations(destinations_raw):
    """
    Parses destinations array, handling lists of dicts or lists of JSON strings
    (such as destinations=['{"id": "...", "amount": ...}', ...]).
    """
    if isinstance(destinations_raw, str):
        destinations_raw = json.loads(destinations_raw)

    if not isinstance(destinations_raw, list) or len(destinations_raw) == 0:
        raise ValueError("'destinations' must be a non-empty array.")

    parsed = []
    for item in destinations_raw:
        if isinstance(item, str):
            parsed.append(json.loads(item))
        elif isinstance(item, dict):
            parsed.append(dict(item))
        else:
            raise ValueError(f"Invalid destination entry: {item!r}")
    return parsed


def collect_utxos(specified_utxos, destinations, feerate_per_kw, minconf=1):
    """
    Collects confirmed UTXOs from wallet matching specified_utxos, or performs
    automatic coin selection via fundpsbt(reserve=0) for the base channel amounts.
    """
    funds = rpc_call("listfunds")["outputs"]
    wallet_utxos = {f"{u['txid']}:{u['output']}": u for u in funds if u.get("status") == "confirmed"}

    selected_utxos_info = []

    if specified_utxos:
        if isinstance(specified_utxos, str):
            specified_utxos = json.loads(specified_utxos)

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
        total_base = sum(parse_sat_amount(d["amount"]) for d in destinations)
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


def process_exact_funding(raw_params, dry_run=False):
    """
    Processes exact zero-change funding:
    1. Normalizes parameters dynamically.
    2. Parses destinations and change_to index (mandatory).
    3. Resolves feerate and UTXOs.
    4. Calculates exact non-change satoshi amount for destinations[change_to].
    5. Updates destinations and calls multifundchannel with ALL other arguments preserved.
    """
    params = normalize_params(raw_params)

    if "destinations" not in params:
        raise ValueError("Missing required parameter 'destinations'.")

    destinations = parse_destinations(params["destinations"])

    # 1. Parse change_to index (mandatory, no default)
    if "change_to" not in params or params["change_to"] is None:
        raise ValueError(
            "Missing required parameter 'change_to' (integer index of destination channel in 'destinations' to receive change)."
        )

    change_to_raw = params["change_to"]
    try:
        change_to_idx = int(change_to_raw)
    except (ValueError, TypeError):
        raise ValueError(f"'change_to' must be an integer index, got {change_to_raw!r}")

    if change_to_idx < 0 or change_to_idx >= len(destinations):
        raise ValueError(
            f"'change_to' index {change_to_idx} is out of bounds for destinations array of length {len(destinations)} "
            f"(valid indices: 0 to {len(destinations) - 1})."
        )

    # 2. Resolve feerate to perkw via CLN
    feerate_arg = params.get("feerate", "opening")
    feerate_res = rpc_call("parsefeerate", {"feerate": feerate_arg})
    feerate_per_kw = feerate_res["perkw"]

    # 3. Collect UTXOs (explicit or automatic)
    minconf = params.get("minconf", 1)
    specified_utxos = params.get("utxos")
    selected_utxos = collect_utxos(specified_utxos, destinations, feerate_per_kw, minconf)

    # 4. Calculate exact zero-change allocation
    calc = calculate_exact_allocation(destinations, feerate_per_kw, selected_utxos, change_to_idx)

    # 5. Update destinations: add the change into destinations[change_to_idx]
    final_destinations = []
    for idx, d in enumerate(destinations):
        new_d = dict(d)
        if idx == change_to_idx:
            new_d["amount"] = calc["exact_target_amount"]
        else:
            new_d["amount"] = parse_sat_amount(d["amount"])
        final_destinations.append(new_d)

    utxo_outpoints = [u["outpoint"] for u in selected_utxos]

    summary = {
        "change_to_index": change_to_idx,
        "change_to_node_id": calc["change_to_id"],
        "original_amount_sat": calc["target_orig_amount"],
        "change_absorbed_sat": calc["change_absorbed"],
        "final_channel_amount_sat": calc["exact_target_amount"],
        "exact_fee_sat": calc["exact_fee"],
        "total_inputs_sat": calc["total_inputs"],
        "tx_weight": calc["total_weight"],
        "feerate_per_kw": feerate_per_kw,
        "change_output_count": 0,
        "change_amount_sat": 0,
        "utxos_spent": utxo_outpoints
    }

    # 6. Prepare multifundchannel arguments:
    # Retain ALL original arguments without hardcoding, remove change_to, update destinations & utxos
    mfc_params = dict(params)
    mfc_params.pop("change_to", None)
    mfc_params["destinations"] = final_destinations
    if not mfc_params.get("utxos"):
        mfc_params["utxos"] = utxo_outpoints

    if dry_run:
        return {
            "dry_run": True,
            "summary": summary,
            "multifundchannel_params": mfc_params
        }

    # 7. Execute multifundchannel
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
                            "usage": "destinations [feerate] [minconf] [utxos] [minchannels] [commitment_feerate] change_to",
                            "description": "Opens channels with multifundchannel adding all leftover change into destination[change_to] with zero change outputs."
                        },
                        {
                            "name": "calculate_exact_funding",
                            "usage": "destinations [feerate] [minconf] [utxos] [minchannels] [commitment_feerate] change_to",
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
