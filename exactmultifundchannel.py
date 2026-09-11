#!/usr/bin/env python3
"""
CLN Plugin: exactmultifundchannel
Author: btweenthebars

Opens multiple Core Lightning channels simultaneously with zero change left back
to the user wallet. Any leftover change is absorbed into a designated destination
channel specified by `change_to` (index in destinations array).

If the change cannot be added to `destinations[change_to]` due to:
- Case 1: Non-Wumbo channel capacity limit (16,777,215 sats / 0.16777215 BTC)
- Case 2: Peer-configured maximum channel limits or user 'max_amount'
- Case 3: Output below dust threshold (546 sats)
The command aborts with a descriptive error message explaining the exact cause
so the user can decide what to do, ensuring no funds are lost or leaked to miner fees.
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
    if s == "all":
        raise ValueError(
            "Destination amount cannot be 'all'. In exactmultifundchannel, specify the base numerical "
            "amount for each channel, and use 'change_to=<index>' to designate which channel absorbs "
            "all leftover change."
        )
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


def check_wumbo_support(features_hex):
    """
    Checks if a peer's feature bitfield advertises option_support_large_channel (wumbo).
    BOLT #9: Bit 18 is compulsory, Bit 19 is optional option_support_large_channel.
    """
    if not features_hex or not isinstance(features_hex, str):
        return False
    try:
        raw_bytes = bytes.fromhex(features_hex.strip())
    except ValueError:
        return False

    def is_bit_set(b, bit):
        byte_offset = bit // 8
        if byte_offset >= len(b):
            return False
        # Big-endian bitfield
        return bool(b[len(b) - 1 - byte_offset] & (1 << (bit % 8)))

    return is_bit_set(raw_bytes, 18) or is_bit_set(raw_bytes, 19)


def get_peer_features(node_id_with_host):
    """
    Queries peer features via listpeers or listnodes to check capability flags.
    """
    node_id = node_id_with_host.split("@")[0].strip()
    try:
        peers_res = rpc_call("listpeers", {"id": node_id})
        peers = peers_res.get("peers", [])
        if peers and "features" in peers[0]:
            return peers[0]["features"]
    except Exception:
        pass

    try:
        nodes_res = rpc_call("listnodes", {"id": node_id})
        nodes = nodes_res.get("nodes", [])
        if nodes and "features" in nodes[0]:
            return nodes[0]["features"]
    except Exception:
        pass

    return None


def calculate_exact_allocation(destinations, feerate_per_kw, utxos_info, change_to_idx, peer_features_hex=None):
    """
    Calculates exact zero-change funding allocation down to the single satoshi.
    Absorbs all remaining input funds into destinations[change_to_idx].
    Validates against:
    - Dust limit (< 546 sats)
    - Wumbo capacity limit (16,777,215 sats) if peer does not support large channels
    - User-specified max_amount limit if configured
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

    target_dest = destinations[change_to_idx]
    target_node_id = target_dest.get("id", f"index_{change_to_idx}")

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
    target_orig_amount = parse_sat_amount(target_dest["amount"])
    exact_target_amount = total_inputs - other_dest_sum - exact_fee

    # Validation Case 3: Dust check
    if exact_target_amount < DUST_LIMIT_SAT:
        raise ValueError(
            f"Cannot add change to destination {change_to_idx} ('{target_node_id}'): "
            f"resulting channel amount ({exact_target_amount} sats) is below the Bitcoin dust threshold ({DUST_LIMIT_SAT} sats). "
            f"Total inputs ({total_inputs:,} sats) are insufficient to cover other channels ({other_dest_sum:,} sats) and fee ({exact_fee:,} sats). "
            f"Please supply more input funds or reduce channel amounts."
        )

    # Validation Case 2 (User Cap): Optional user-configured max_amount
    if "max_amount" in target_dest and target_dest["max_amount"] is not None:
        user_max = parse_sat_amount(target_dest["max_amount"])
        if exact_target_amount > user_max:
            excess_above_max = exact_target_amount - user_max
            raise ValueError(
                f"Cannot add change to destination {change_to_idx} ('{target_node_id}'): "
                f"resulting channel amount ({exact_target_amount:,} sats) exceeds the configured 'max_amount' of {user_max:,} sats "
                f"by {excess_above_max:,} sats. Aborting so you can select an alternative destination for change_to, "
                f"adjust inputs, or increase max_amount."
            )

    # Validation Case 1: Wumbo check (large channels)
    if exact_target_amount > MAX_NON_WUMBO_SAT:
        is_wumbo = check_wumbo_support(peer_features_hex)
        if not is_wumbo:
            excess_above_wumbo = exact_target_amount - MAX_NON_WUMBO_SAT
            raise ValueError(
                f"Cannot add change to destination {change_to_idx} ('{target_node_id}'): "
                f"resulting channel amount ({exact_target_amount:,} sats) exceeds the non-Wumbo limit of {MAX_NON_WUMBO_SAT:,} sats "
                f"(0.16777215 BTC) by {excess_above_wumbo:,} sats, and peer '{target_node_id}' does not support "
                f"'option_support_large_channel' (BOLT #9 wumbo feature bit 19). "
                f"Aborting so you can select a wumbo-enabled destination for change_to or provide fewer input funds."
            )

    change_absorbed = exact_target_amount - target_orig_amount

    # Invariant verification: strictly 0 satoshis unallocated to outputs or fee
    total_outputs = other_dest_sum + exact_target_amount
    assert total_inputs - total_outputs == exact_fee, "Accounting error: Inputs - Outputs != Fee!"

    return {
        "exact_target_amount": exact_target_amount,
        "target_orig_amount": target_orig_amount,
        "change_absorbed": change_absorbed,
        "exact_fee": exact_fee,
        "total_weight": total_weight,
        "total_inputs": total_inputs,
        "other_dest_sum": other_dest_sum,
        "change_to_idx": change_to_idx,
        "change_to_id": target_node_id
    }


def get_mfc_parameter_names():
    """
    Dynamically fetches positional parameter names of multifundchannel from CLN help.
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
    Parses destinations array, handling lists of dicts or lists of JSON strings.
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
    automatic coin selection via fundpsbt(reserve=0) for base channel amounts.
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
            "reserve": 100,
            "excess_as_change": True
        })

        reservations = fund_res.get("reservations", [])
        # Immediately release the reservation so multifundchannel can spend them cleanly
        if "psbt" in fund_res:
            try:
                rpc_call("unreserveinputs", {"psbt": fund_res["psbt"]})
            except Exception:
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


def process_exact_funding(raw_params, dry_run=False):
    """
    Processes exact zero-change funding:
    1. Normalizes parameters dynamically.
    2. Validates mandatory change_to index.
    3. Resolves feerate and UTXOs.
    4. Validates wumbo and peer limits.
    5. Calculates exact non-change satoshi amount for destinations[change_to].
    6. Updates destinations and calls multifundchannel with ALL other arguments preserved.
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

    target_peer_id = destinations[change_to_idx].get("id", "")

    # 2. Resolve feerate to perkw via CLN
    feerate_arg = params.get("feerate", "opening")
    feerate_res = rpc_call("parsefeerate", {"feerate": feerate_arg})
    feerate_per_kw = feerate_res["perkw"]

    # 3. Collect UTXOs (explicit or automatic)
    minconf = params.get("minconf", 1)
    specified_utxos = params.get("utxos")
    selected_utxos = collect_utxos(specified_utxos, destinations, feerate_per_kw, minconf)

    # 4. Check peer features (Wumbo support) if RPC available
    peer_features = get_peer_features(target_peer_id)

    # 5. Calculate exact zero-change allocation and validate limits
    calc = calculate_exact_allocation(
        destinations,
        feerate_per_kw,
        selected_utxos,
        change_to_idx,
        peer_features_hex=peer_features
    )

    # 6. Update destinations: add the change into destinations[change_to_idx]
    final_destinations = []
    for idx, d in enumerate(destinations):
        new_d = dict(d)
        new_d.pop("max_amount", None)  # internal plugin parameter
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

    # 7. Prepare multifundchannel arguments:
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

    # 8. Execute multifundchannel with explicit error trapping for peer limit rejections
    try:
        mfc_res = rpc_call("multifundchannel", mfc_params)
        mfc_res["zero_change_summary"] = summary
        return mfc_res
    except RuntimeError as e:
        err_msg = str(e)
        # Catch peer rejections and provide a clear actionable error
        if "max" in err_msg.lower() or "large" in err_msg.lower() or "capacity" in err_msg.lower() or "reject" in err_msg.lower():
            raise RuntimeError(
                f"Peer '{target_peer_id}' at destination {change_to_idx} rejected the funding amount of "
                f"{calc['exact_target_amount']:,} satoshis ({err_msg}). "
                f"Aborting to prevent excess funds from going to miner fees. "
                f"Please choose a different destination for 'change_to' or reduce funding inputs."
            )
        raise


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
