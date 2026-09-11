# CLN Exact Channel Funding Plugin (`exactmultifundchannel`)

A lightweight, zero-dependency Core Lightning (CLN) plugin that opens multiple Lightning channels using `multifundchannel` without leaving any change output back to your wallet.

---

## The Problem

When opening multiple Lightning channels with `multifundchannel`:
1. **Unwanted Change Outputs**: If standard amounts are provided (e.g., node1: 100k, node2: 200k, node3: 300k), CLN's coin selector selects UTXOs that exceed the total needed. The remainder is sent back to your wallet as a change output.
   - Adds **31–43 vbytes (124–172 weight units)** to the transaction.
   - Creates fragmented micro-UTXOs or dust in your on-chain wallet.
   - Requires you to pay another input fee later when spending that change UTXO.
2. **The Danger of Native `"amount": "all"`**: While `multifundchannel` allows specifying `"amount": "all"` for one destination, if you do not specify `utxos`, CLN's internal coin selector sweeps **every single available UTXO in your entire wallet** into that channel.

---

## The Solution

`exactmultifundchannel` solves both problems:
1. **Targeted Coin Selection**: Uses CLN's `fundpsbt` (with `reserve=0`) to select only the minimal set of UTXOs needed to cover your channels (or accepts explicit `utxos`).
2. **Exact Non-Change Allocation**: Mathematically computes the exact fee for a transaction with **zero change outputs**, allocating 100% of the remaining input satoshis into your designated channel (`node1`).
3. **Double Fee Savings**: You not only eliminate the change output, but the fee savings from removing that change output (approx. 43 vB $\times$ feerate) are also redirected directly into your channel capacity.

$$\text{Inputs} - \sum \text{Outputs} = \text{Fee}_{\text{exact}}$$
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
Executes `multifundchannel` with zero change on the funding transaction.

#### Parameters:
- `destinations` *(array, required)*: List of destination objects:
  - `id` *(string, required)*: Peer node pubkey (optional `@host:port`).
  - `amount` *(integer, required)*: Base satoshis to fund.
  - `announce`, `push_msat`, `close_to`, `mindepth`, `reserve` *(optional)*.
- `excess_node` *(string, optional)*: Pubkey of the node to receive all leftover change. Defaults to the first node in `destinations`.
- `feerate` *(string/number, optional)*: Feerate (`"normal"`, `"urgent"`, `"slow"`, or `"2500perkw"`). Defaults to `"opening"`.
- `minconf` *(integer, optional)*: Minimum confirmations for UTXOs (default: `1`).
- `utxos` *(array of strings, optional)*: Specific `txid:vout` outpoints to use.
- `commitment_feerate` *(string/number, optional)*: Initial commitment tx feerate.
- `minchannels` *(integer, optional)*: Minimum successful channels required.

#### Example:
```bash
lightning-cli exactmultifundchannel \
  destinations='[{"id":"02abc...","amount":100000},{"id":"03def...","amount":200000},{"id":"02123...","amount":300000}]' \
  excess_node="02abc..." \
  feerate="normal"
```

---

### 2. `calculate_exact_funding` (Dry Run)
Inspects the exact weights, fees, and satoshi allocations without committing any transaction.

#### Example:
```bash
lightning-cli calculate_exact_funding \
  destinations='[{"id":"02abc...","amount":100000},{"id":"03def...","amount":200000}]' \
  feerate="2500perkw"
```

#### Output:
```json
{
  "dry_run": true,
  "summary": {
    "excess_node_id": "02abc...",
    "exact_channel_amount": 497928,
    "exact_fee_sat": 2072,
    "total_inputs_sat": 1000000,
    "tx_weight": 829,
    "feerate_per_kw": 2500,
    "change_output_count": 0,
    "change_amount_sat": 0,
    "utxos_spent": ["a1b2c3...:0"]
  },
  "final_destinations": [
    {"id": "02abc...", "amount": 497928},
    {"id": "03def...", "amount": 200000}
  ]
}
```

---

## Technical Details

CLN funding transactions use SegWit:
- **Core Weight**: $W_{\text{core}} = (4 + \text{varint}(M) + \text{varint}(N) + 4) \times 4 + 2 = 42 \text{ WU}$.
- **Channel Outputs**: Each P2WSH/P2TR funding output is 34 bytes scriptPubKey $\rightarrow (8 + 1 + 34) \times 4 = 172 \text{ WU}$ ($43 \text{ vbytes}$).
- **Input Spend Weights**:
  - P2TR (Taproot): $230 \text{ WU}$ ($57.5 \text{ vbytes}$)
  - P2WPKH (Native SegWit): $271 \text{ WU}$ ($67.75 \text{ vbytes}$)
  - P2SH-P2WPKH (Nested SegWit): $363 \text{ WU}$ ($90.75 \text{ vbytes}$)

The exact fee is computed matching CLN's integer division:
$$\text{Fee} = \lfloor (\text{feerate\_per\_kw} \times W_{\text{total}}) / 1000 \rfloor$$

---

## Running Unit Tests

```bash
python3 -m unittest discover tests
```

---

## License

MIT License. Copyright (c) 2026 btweenthebars.
