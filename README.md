# CLN Exact Channel Funding Plugin (`exactmultifundchannel`)

A lightweight, zero-dependency Core Lightning (CLN) plugin that opens multiple Lightning channels using `multifundchannel` without leaving any change output back to your wallet.

---

## Overview & Interface

`exactmultifundchannel` mirrors the exact interface of CLN's `multifundchannel`, adding a **mandatory** `change_to` argument to specify which destination channel absorbs all leftover change satoshis.

Any existing, optional, or future arguments supported by `multifundchannel` are automatically forwarded without hardcoding.

```bash
exactmultifundchannel destinations [feerate] [minconf] [utxos] [minchannels] [commitment_feerate] ... change_to=index
```

### Example

```bash
lightning-cli -k exactmultifundchannel \
  feerate=700 \
  destinations='[{"id":"aaaaa", "amount":1000000},{"id":"bbbb", "amount":2000000}]' \
  utxos='["xxxx:0","yyyy:1"]' \
  change_to=1
```

In this example:
- `change_to=1` (mandatory) selects the channel at index `1` (node `"bbbb"`).
- Node `"aaaaa"` receives its exact requested `1,000,000` sats.
- The exact non-change fee is calculated for the transaction.
- All remaining satoshis from the inputs are added into node `"bbbb"`:
  $$\text{Amount}(\text{"bbbb"}) = 2,000,000 + \text{Change}$$
- `multifundchannel` is called with the adjusted destinations and all other arguments passed through.
- **Change outputs on the funding transaction: 0.**

---

## Why Zero-Change Funding?

When opening multiple Lightning channels normally:
1. **Unwanted Change Outputs**: CLN's coin selection selects UTXOs that exceed the total required. The remainder is sent back to your wallet as a change output:
   - Adds **31–43 vbytes (124–172 weight units)** to the transaction.
   - Creates fragmented micro-UTXOs or dust in your on-chain wallet.
   - Requires another input fee when you spend that change UTXO in the future.
2. **The Danger of Native `"amount": "all"`**: While native `multifundchannel` allows specifying `"amount": "all"` for one destination, if you do not explicitly pass `utxos`, CLN's internal coin selector sweeps **every single available UTXO in your entire wallet** into that channel.

`exactmultifundchannel` fixes this:
- If `utxos` is provided, it uses only those UTXOs.
- If `utxos` is omitted, it performs minimal coin selection using `fundpsbt(reserve=0)` for just the base amounts, avoiding whole-wallet sweeps.
- It calculates the exact fee with **zero change outputs**, allocating all remainder directly into `destinations[change_to]`.

$$\sum \text{Inputs} - \sum \text{Outputs} = \text{Fee}_{\text{exact}}$$
$$\text{Change} = 0 \text{ satoshis}$$

---

## Installation

### Prerequisites
- Python 3.8+ (standard library only; **no pip dependencies required**).
- Core Lightning (CLN) v0.10.0 or later.

### Load Dynamically
```bash
lightning-cli plugin start /path/to/exactmultifundchannel.py
```

### Or Load Automatically at Startup
Add to your `~/.lightning/config`:
```text
plugin=/path/to/exactmultifundchannel.py
```

---

## RPC Commands

### 1. `exactmultifundchannel`
Executes `multifundchannel` with zero change outputs.

#### Parameters:
- `destinations` *(array, required)*: List of destination objects (or JSON strings).
- `change_to` *(integer, required)*: Zero-based index in `destinations` of the channel to absorb all change satoshis. **Mandatory with no default**.
- `feerate` *(string/number, optional)*: Fee rate (e.g. `"normal"`, `"urgent"`, `700`, `"2500perkw"`). Defaults to `"opening"`.
- `utxos` *(array of strings, optional)*: Specific `txid:vout` outpoints to spend.
- `minconf`, `minchannels`, `commitment_feerate`, etc.: Any standard or future `multifundchannel` argument.

---

### 2. `calculate_exact_funding` (Dry Run)
Previews the exact transaction weights, fees, and satoshi allocations without broadcasting or opening channels.

```bash
lightning-cli -k calculate_exact_funding \
  feerate=700 \
  destinations='[{"id":"aaaaa", "amount":1000000},{"id":"bbbb", "amount":2000000}]' \
  utxos='["xxxx:0","yyyy:1"]' \
  change_to=1
```

#### Output:
```json
{
  "dry_run": true,
  "summary": {
    "change_to_index": 1,
    "change_to_node_id": "bbbb",
    "original_amount_sat": 2000000,
    "change_absorbed_sat": 999351,
    "final_channel_amount_sat": 2999351,
    "exact_fee_sat": 649,
    "total_inputs_sat": 4000000,
    "tx_weight": 928,
    "feerate_per_kw": 700,
    "change_output_count": 0,
    "change_amount_sat": 0,
    "utxos_spent": ["xxxx:0", "yyyy:1"]
  },
  "multifundchannel_params": {
    "destinations": [
      {"id": "aaaaa", "amount": 1000000},
      {"id": "bbbb", "amount": 2999351}
    ],
    "feerate": "700perkw",
    "utxos": ["xxxx:0", "yyyy:1"]
  }
}
```

---

## Technical Details

CLN SegWit funding transaction weight:
- **Core Weight**: $W_{\text{core}} = (4 + \text{varint}(M) + \text{varint}(N) + 4) \times 4 + 2 = 42 \text{ WU}$.
- **Channel Outputs**: Each funding output is 34 bytes scriptPubKey $\rightarrow 172 \text{ WU}$ ($43 \text{ vbytes}$).
- **Input Spend Weights**:
  - P2TR (Taproot): $230 \text{ WU}$ ($57.5 \text{ vbytes}$)
  - P2WPKH (Native SegWit): $271 \text{ WU}$ ($67.75 \text{ vbytes}$)
  - P2SH-P2WPKH (Nested SegWit): $363 \text{ WU}$ ($90.75 \text{ vbytes}$)

The exact fee matches CLN's integer calculation:
$$\text{Fee} = \lfloor (\text{feerate\_per\_kw} \times W_{\text{total}}) / 1000 \rfloor$$

---

## Running Unit Tests

```bash
python3 -m unittest discover tests
```

---

## License

MIT License. Copyright (c) 2026 btweenthebars.
